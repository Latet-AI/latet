"""The dashboard: REST API and live feed under ``/_latet/api/v1``, the UI under ``/_latet/ui``.

Access: ``latet dashboard`` asks the daemon (with the local token) for a one-time code and opens
``/_latet/ui/login?code=...``, which sets an HttpOnly, SameSite=Strict session cookie. Every API
call needs that cookie; calls that change something also need the ``X-Latet-UI: 1`` header, which
a page from another origin cannot send without a CORS preflight that is never answered.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import hmac
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from statistics import quantiles
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeGuard

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route
from starlette.types import Receive, Scope, Send

from latet import __version__
from latet.config import (
    ID_REGIONS,
    MAX_GUIDANCE,
    REVIEW_DETAILS,
    TERM_WITHOUT_LETTERS,
    Config,
    Destination,
    LlmReview,
    Profile,
    ProjectEntry,
    ProjectSettings,
    TermConfig,
    in_container,
    join_guidance,
    problems_of,
    term_errors,
)
from latet.engine.build import build_masker, recognizers_for, reserved_types
from latet.engine.detectors import checksums, ids
from latet.engine.detectors.pii import ALL_PII, BASE_PII, effective_pii
from latet.engine.detectors.terms import has_letter_or_digit
from latet.engine.rehydrate import restore_text, restores_for
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import MaskingError, Vault
from latet.gateway.approvals import Change, MaskError
from latet.gateway.recorder import original_range, reconstruct, replacement_dicts, restored_source
from latet.http_inputs import bounded_body, same_etag
from latet.logline import Paced, described
from latet.projects import (
    IN_CLEAR_REASON,
    PROJECT_FILE,
    REPO_KEYS,
    UNCHECKED,
    UNCHECKED_REASON,
    Project,
    ProjectFileUnreadable,
    RepositoriesUnchecked,
    RepositoryScan,
    effective_config,
    file_rules,
    file_tag,
    personal_for,
    render_project_file,
    repo_settings,
    repository_states,
    with_personal,
    write_project_file,
)
from latet.secret_text import KEY_LONGEST, KEY_REQUIREMENT, key_problem
from latet.server import egress
from latet.server.shared import (
    REVIEWER_KEY,
    SAVED_CHANGE_PENDING,
    ChangedMeanwhile,
    ChangeWaits,
    OwnSettings,
    SettingsRefused,
    SettingsSavedChangePending,
    SettingsStale,
    SettingsTooLarge,
    SharedItemsError,
    TermsTooLong,
    TooManyProjects,
    TooManyRules,
    check_rules,
    own_destinations_refused,
    personal_destination_problem,
    pinned_reviewer,
    reviewer_key_named,
)
from latet.state.local import CODE_TTL, SESSION_TTL, LocalSignIns  # noqa: F401 - CODE_TTL: its old name here

if TYPE_CHECKING:
    from latet.columns import Column
    from latet.engine.masker import Masker
    from latet.runtime import Runtime
    from latet.state.protocols import SignIns
    from latet.tutorial.api import TutorialApi

log = logging.getLogger(__name__)

API = "/_latet/api/v1"
UI = "/_latet/ui"
# Media types the dashboard's files need that Python's guess may not know (Windows knows no .woff2).
_UI_MEDIA = {".woff2": "font/woff2", ".woff": "font/woff"}
COOKIE = "latet_session"
_MAX_PLAYGROUND = 1_000_000
# The largest body a dashboard request may have (a rules file, a playground text): read no further.
_MAX_BODY = 8_000_000
# The bytes of a body parsed where it is read, at most: a larger one is parsed in a worker thread (``_json``), as the
# bridge and the administration parse theirs (A-04). Under it the parse costs less than a thread's hop.
PARSED_ON_LOOP = 4096
_DASHBOARD_POOL: ThreadPoolExecutor | None = None
_DASHBOARD_POOL_LOCK = threading.Lock()
# The exchanges one call of ``stats`` reads at most, the latest, a part at a time (``HistoryStore.summaries_parts``):
# about three seconds of a worker thread's work (opening and aggregating them took 2.6 s on a desktop, PostgreSQL's
# fetch aside), and what one part holds of memory, whatever an identity's history holds (on a server, up to some
# millions). A busy person's 90 days stay under it.
STATS_MOST = 100_000
_KINDS = {k.value: k for k in SlotKind}
_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}
_NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Latet</title>
<body style="font-family:system-ui;margin:3rem;max-width:40rem">
<h1>Latet dashboard</h1><p>The dashboard files are not part of this install.
Build them with <code>npm run build</code> in <code>dashboard/</code>, or install a release.</p>
<p>The API is available under <code>/_latet/api/v1</code>.</p></body>"""
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # the built dashboard
# The documentation of this version (`npm run build:bundle` in docs/, then scripts/check_docs_bundle.py), served at DOCS
# by the daemon and by a Latet server, where every link of the dashboard opens it.
DOCS = "/_latet/docs"
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
# What a file of the documentation is sent as, by its extension, never as the system guesses (Windows can say text/plain
# for .js, which nosniff then refuses to run); anything else as bytes, Pagefind's index and fragments among them, which
# it reads as they are (no content-encoding).
_DOCS_MEDIA = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".wasm": "application/wasm",
}
# A path in the documentation as its build names its files: letters, digits, '.', '_', '-' and '/'. Nothing a file
# system reads another way (a backslash, a colon, a percent sign, a NUL) is ever looked up.
_DOCS_PATH = re.compile(r"[A-Za-z0-9._/-]*")
# A Windows device name as a path's segment (COM1, aux.html): looked up, it holds the event loop for half a second, and
# any page may ask for it in a loop. Never a file of the dashboard or of the docs.
_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?")


def _names_a_device(rel: str) -> bool:
    return any(_DEVICE.fullmatch(part) for part in rel.replace("\\", "/").split("/"))


def served_docs_path(rel: str) -> bool:
    """Whether ``rel`` is a path the docs route looks up at all: a path the build writes (its characters, no empty or
    hidden segment, no device name). ``scripts/check_docs_bundle.py`` holds the same rule for the bundle's files."""
    parts = rel.split("/")
    return (
        _DOCS_PATH.fullmatch(rel) is not None
        and "" not in parts[:-1]
        and not any(part.startswith(".") for part in parts)
        and not _names_a_device(rel)
    )


_SCRIPT_HASH = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")

Handler = Callable[[Request], Awaitable[Response]]


@functools.lru_cache(maxsize=8)
def _docs_policy(csp_file: str, stamp: tuple[int, int]) -> str | None:
    """The documentation's content security policy, from the hashes of its inline scripts that its check wrote; None
    when the file holds anything but a list of sha256 hashes, which is never put in a policy. ``stamp`` (the file's
    time and size) reads a new build's file again."""
    try:
        written = json.loads(Path(csp_file).read_bytes())
    except (OSError, ValueError):
        return None
    hashes = written.get("script_hashes") if isinstance(written, dict) else None
    if not isinstance(hashes, list) or not all(isinstance(h, str) and _SCRIPT_HASH.fullmatch(h) for h in hashes):
        return None
    # As the dashboard's own policy, but for the inline scripts of the static pages (their theme and their menus), by
    # hash, and the WebAssembly of their search (Pagefind): no other host, nothing inline that was not built.
    scripts = " ".join(["'self'", *(f"'{h}'" for h in hashes), "'wasm-unsafe-eval'"])
    return (
        f"default-src 'self'; script-src {scripts}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def docs_headers(docs: Path) -> dict[str, str] | None:
    """The headers of every answer of the documentation bundled at ``docs``, its own policy among them; None when there
    is none to serve: no index.html, or no .csp.json its check wrote (a build that failed the check, or never had it).
    """
    csp = docs / ".csp.json"
    try:
        if not (docs / "index.html").is_file():
            return None
        found = csp.stat()
    except OSError:
        return None
    policy = _docs_policy(str(csp), (found.st_mtime_ns, found.st_size))
    return None if policy is None else {**_SECURITY_HEADERS, "content-security-policy": policy}


def _inside(root: Path, path: Path) -> Path | None:
    """``path`` with its links followed, when it is still in ``root``; None when it leads out, or cannot be read."""
    try:
        found = path.resolve()
    except (OSError, ValueError):
        return None
    return found if found.is_relative_to(root) else None


def docs_file(docs: Path, rel: str) -> Response:
    """A file of the documentation bundled at ``docs``, public (no session): a folder's index.html for the folder's own
    path, the path without its last slash sent to it (its pages' relative links work from there); the bundle's
    404.html, with 404, for what it does not have, a folder without its page among them (nothing is listed). A path
    that is not one the build writes (a character it never uses, an empty segment, a ``..``, a hidden file) answers 404
    with nothing looked up, and one that leads out of the bundle by a link 404 with nothing read."""
    headers = docs_headers(docs)
    if headers is None:
        return Response(status_code=404, headers=_SECURITY_HEADERS)
    if not served_docs_path(rel):
        return Response(status_code=404, headers=headers)
    root = docs.resolve()
    target = _inside(root, docs / rel)
    if target is not None and target.is_dir():
        if rel and not rel.endswith("/"):
            return RedirectResponse(f"{DOCS}/{rel}/", status_code=308, headers=headers)
        target = _inside(root, target / "index.html")
    if target is None:
        return Response(status_code=404, headers=headers)
    if target.is_file():
        media = _DOCS_MEDIA.get(target.suffix.lower(), "application/octet-stream")
        cache = "public, max-age=31536000, immutable" if rel.startswith("_astro/") else "no-cache"
        return FileResponse(target, media_type=media, headers={**headers, "cache-control": cache})
    missing = _inside(root, docs / "404.html")
    if missing is None or not missing.is_file():
        return Response(status_code=404, headers=headers)
    return FileResponse(
        missing, status_code=404, media_type=_DOCS_MEDIA[".html"], headers={**headers, "cache-control": "no-cache"}
    )


def ui_file(web: Path, rel: str) -> Response:
    """A file of the built dashboard; ``index.html`` for a page's own path."""
    index = web / "index.html"
    if not index.is_file():
        return HTMLResponse(_NOT_BUILT, headers=_SECURITY_HEADERS)
    if rel:
        if _names_a_device(rel):
            return Response(status_code=404, headers=_SECURITY_HEADERS)
        target = (web / rel).resolve()
        inside = target.is_relative_to(web.resolve())
        if inside and target.is_file():
            cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
            headers = {**_SECURITY_HEADERS, "cache-control": cache}
            return FileResponse(target, media_type=_UI_MEDIA.get(target.suffix), headers=headers)
        if not inside or Path(rel).suffix:
            return Response(status_code=404, headers=_SECURITY_HEADERS)
    return FileResponse(index, headers={**_SECURITY_HEADERS, "cache-control": "no-cache"})


class _SseResponse(StreamingResponse):
    """Close the event iterator even when a send fails before it starts, and release its subscription once."""

    def __init__(self, events: AsyncGenerator[bytes, None], done: Callable[[], None]) -> None:
        self.events, self.done = events, done
        super().__init__(
            events, media_type="text/event-stream", headers={"cache-control": "no-store", "x-accel-buffering": "no"}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.events.aclose()
            finally:
                self.done()


def _sse(queue: asyncio.Queue[Any], done: Callable[[], None]) -> StreamingResponse:
    """Server-sent events from ``queue``: an exchange summary, or an ``(event, payload)`` pair."""
    closed = False

    def finish() -> None:
        nonlocal closed
        if not closed:
            closed = True
            done()

    async def events() -> AsyncGenerator[bytes, None]:
        try:
            yield b"retry: 3000\n\n"
            while True:
                try:
                    summary = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                event, payload = summary if isinstance(summary, tuple) else ("exchange", summary)
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
        finally:
            finish()

    return _SseResponse(events(), finish)


def _read(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


_FIELD_RULES = ("strict:", "field-secret:")  # rules that name the field of the value they masked


def _fields_restored(spans: list[dict[str, Any]], vault: Vault) -> list[dict[str, Any]]:
    """``spans`` with each rule that names a field (strict data mode's, a secret field's) naming it as the original
    does: a key of the path masked is named by its value, as the dashboard shows the text, so that keeping the field in
    clear makes a rule that matches it. What the history keeps names the field as it was sent."""
    entities = [e for s in spans if s.get("entity") and (e := vault.get(s["entity"])) is not None]
    if not entities:
        return spans
    restores = restores_for(entities, json_mode=False)
    for span in spans:
        rule = span.get("rule") or ""
        for prefix in _FIELD_RULES:
            if rule.startswith(prefix):
                span["rule"] = prefix + restore_text(rule[len(prefix) :], restores)[0]
    return spans


def restore_detail(detail: dict[str, Any], vault: Vault) -> dict[str, Any]:
    """A stored exchange as the dashboard shows it: the originals come from the vault as it is now (a forgotten
    value stays masked), with offsets on both sides."""
    for slot in detail["request"]["slots"]:
        original, spans = reconstruct(slot["masked"], slot["replacements"], vault)
        slot["original"] = original
        slot["replacements"] = _fields_restored(spans, vault)
        slot["source"] = restored_source(slot.get("source"), vault)  # where a tool's output came from, likewise
    # A value that stopped the request is in clear in its block, as sent: its place in the original
    # moves with the values masked before it. A block that is gone leaves it with no place there.
    replaced = {slot["block"]: slot["replacements"] for slot in detail["request"]["slots"]}
    for leak in detail["leaks"]:
        if (spans := replaced.get(leak.get("block"))) is not None:
            leak["start"], leak["end"] = original_range(spans, leak["masked_start"], leak["masked_end"])
    response = detail.get("response")
    if response is not None:
        text, text_spans = reconstruct(response.get("text", ""), response.get("text_spans", []), vault)
        calls = []
        for call in response.get("tool_calls", []):
            args, spans = reconstruct(call.get("args", ""), call.get("spans", []), vault)
            calls.append(
                {
                    "name": call.get("name", ""),
                    "args_masked": call.get("args", ""),
                    "args_restored": args,
                    "spans": spans,
                }
            )
        detail["response"] = {
            "headers": response.get("headers", []),
            "text_masked": response.get("text", ""),
            "text_restored": text,
            "text_spans": text_spans,  # offsets on both sides, like a slot's replacements
            "tool_calls": calls,
        }
    return detail


def sandbox_refusal(current: Config, config: Config) -> str | None:
    """What the tutorial's sandbox refuses of a change from ``current`` to ``config`` made on the Rules page: a
    destination, as its only model is the pretend one; the local reviewer turned on, as its server is outside the
    sandbox, and, while the Tutorial Reviewer is the local reviewer, any of its settings but your guidance; your
    identity, as the sandbox has nobody's. None: nothing. A change only: the Rules page saves its whole draft, what it
    leaves as it is included. (The tutorial agent's `latet config set` is refused every setting the sandbox keeps for
    itself.)"""
    if config.upstreams != current.upstreams:
        return "a destination (its only model is the pretend one)"
    if config.llm_review.enabled and not current.llm_review.enabled:
        return "the local reviewer"
    kept = config.llm_review.model_copy(update={"guidance": current.llm_review.guidance})
    if current.llm_review.enabled and kept != current.llm_review:
        return "the local reviewer's settings but your guidance (its server is the Tutorial Reviewer)"
    if config.identity != current.identity:
        return "your identity (the sandbox has nobody's)"
    return None


def _column_view(column: Column) -> dict[str, Any]:
    """What the dashboard shows of a column: a few of its values, never all of them."""
    return {
        "name": column.name,
        "masked": column.masked,
        "proposal": column.proposal,
        "reason": column.reason,
        "detail": column.detail,
        "kept_now": column.kept_now,
        "everywhere": column.everywhere,
        "held_by_file": column.held_by_file,
        "examples": column.examples,
    }


class _ToolOutput(NamedTuple):
    """One tool's output of an exchange, as the history keeps it, and what decides strict data mode for it."""

    slot: dict[str, Any]  # the block, as the exchange's detail gives it
    recorded: dict[str, Any] | None  # the project the exchange was recorded in, as recorded
    project: Project | None  # that project, when Latet has it loaded
    config: Config  # the rules in effect there
    strict: bool  # strict data mode is on there
    applies: bool  # strict data mode covers the tool


# A computer's two calls to its identity's space on a server (the user name's fake, a destination its agent names): the
# bytes of a body read at most (past them 413, nothing parsed), the characters of a user name and of an address.
USER_NAME_BYTES = 16 * 1024
DESTINATIONS_BYTES = 64 * 1024
USER_NAME_CHARS = 256
ADDRESS_CHARS = 2048
# What a user name a computer sends may not hold (Unicode's categories): controls, format characters (the bidirectional
# and the zero-width ones among them), lone surrogates, line and paragraph separators. It is interned as it is sent.
_NOT_IN_A_NAME = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
USER_NAME_SHAPE = (
    f"name: the computer's user name, a text of 1 to {USER_NAME_CHARS} characters, not only white space, with no "
    "control, format or separator character, and nothing beside it"
)
DESTINATIONS_SHAPE = "destinations: a list of {url, agent}, agent claude or codex"
_ADDRESS_SHAPE = f"destinations: each url {ADDRESS_CHARS} characters at most, printable ASCII alone"
NO_COMPUTER = "an API key has no computer"
_UNANSWERED = "the server cannot answer now: try again in a moment"
_WORK_UNANSWERED = Paced()


def _a_user_name(name: object) -> TypeGuard[str]:
    """Whether ``name`` is a user name a computer may send (``USER_NAME_SHAPE``)."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= USER_NAME_CHARS
        and bool(name.strip())
        and not any(unicodedata.category(c) in _NOT_IN_A_NAME for c in name)
    )


def _wanted(data: dict[str, Any] | None) -> list[tuple[str, Literal["claude", "codex"] | None]] | JSONResponse:
    """What a computer's ``{destinations: [{url, agent}]}`` names (1 to 20 of them, nothing else in it), each address
    written as a saved one is (``Destination``: what ``add_destination`` compares); a refusal (400) naming the field,
    never what it holds, for anything else."""
    items = data.get("destinations") if data is not None and set(data) == {"destinations"} else None
    if not isinstance(items, list) or not 0 < len(items) <= 20:
        return _error(DESTINATIONS_SHAPE, 400)
    wanted: list[tuple[str, Literal["claude", "codex"] | None]] = []
    for item in items:
        if not isinstance(item, dict) or not set(item) <= {"url", "agent"} or not isinstance(item.get("url"), str):
            return _error(DESTINATIONS_SHAPE, 400)
        url, agent = item["url"], item.get("agent")
        if agent not in (None, "claude", "codex"):
            return _error(DESTINATIONS_SHAPE, 400)
        if len(url) > ADDRESS_CHARS or any(not 0x21 <= ord(c) <= 0x7E for c in url):
            return _error(_ADDRESS_SHAPE, 400)
        try:
            made = Destination(name="new", url=url).url
        except ValidationError as exc:
            reason = exc.errors(include_url=False, include_context=False, include_input=False)[0]["msg"]
            return _error(f"not an address Latet can send to: {reason}", 400)
        wanted.append((made, agent))
    return wanted


async def _within(request: Request, limit: int) -> bytearray | None:
    """The request's body, ``limit`` bytes at most, read into one buffer and no further: None past it (a
    ``content-length`` over it says so before anything is read)."""
    return await bounded_body(request, limit)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# A save of the settings their store did not keep, on a server (``Dashboard._unkept``), or made from a page older than
# the settings in use (its etag: in another tab, a change applied since, on a computer too): its words, and its reason,
# which the dashboard words in the reader's language. Said in the log once a minute at most when the store does not
# answer.
STALE_SETTINGS = (
    "your settings changed since they were read, here or on another instance of the server: make the change again"
)
UNNAMED_SETTINGS = (
    "on a server a save names the settings it was made from (etag, as GET /config answered it): read them again, then "
    "make the change"
)
TOO_LARGE_SETTINGS = "these settings weigh more than a server keeps for one identity: they were not kept"
UNKEPT_SETTINGS = "your settings could not be kept now: try again in a moment"
_UNKEPT = Paced()


def _refused_save(message: str, status: int, reason: str) -> JSONResponse:
    return JSONResponse({"error": message, "reason": reason}, status_code=status)


def _unchecked() -> JSONResponse:
    """A change of the settings (a reload, an import) weighed on repositories that could not all be read now
    (``RepositoriesUnchecked``): nothing applied, opened for you or kept as approved; tried again once they can be."""
    return JSONResponse({"error": UNCHECKED, "reason": UNCHECKED_REASON}, status_code=503, headers={"Retry-After": "1"})


def _added_by(key: str, saved: ProjectSettings, held: ProjectSettings | None) -> bool:
    """Whether the entry of a repository file's notice (``recognizers.<name>``, ``terms.<place>``, ``fields.<name>``)
    is one ``saved`` adds or changes, not one the file ``held`` already as it is."""
    kind, _, which = key.partition(".")
    before = held if held is not None else ProjectSettings()
    if kind == "terms":
        place = int(which) - 1 if which.isdigit() else -1
        return not 0 <= place < len(saved.terms) or saved.terms[place] not in before.terms
    if kind == "fields":
        return any(f not in before.fields for f in saved.fields if f.name == which)
    if kind == "recognizers":
        return any(r not in before.recognizers for r in saved.recognizers if str(r.get("name")) == which)
    return True


# A project's latet.yaml there that was not read as a project's file (``ProjectFileUnreadable``): nothing is saved over
# it, whatever tag the page sends. Not a regular file, or too large: fixed in the folder first (409). Not readable now
# (a refused permission, a sharing violation): tried again (503).
REPO_FILE_UNREADABLE = (
    "the project's latet.yaml is not a regular file, or larger than a project's file may be: nothing is saved over it "
    "until it is fixed in the folder"
)
REPO_FILE_UNAVAILABLE = "the project's latet.yaml cannot be read now: nothing is saved over it; try again"


def _repo_file_unread(exc: ProjectFileUnreadable) -> JSONResponse:
    if exc.reason == "unavailable":
        return JSONResponse(
            {"error": REPO_FILE_UNAVAILABLE, "reason": "repo_file_unavailable"},
            status_code=503,
            headers={"Retry-After": "1"},
        )
    return _refused_save(REPO_FILE_UNREADABLE, 409, "repo_file_unreadable")


def _saved_change_pending() -> JSONResponse:
    """A kept save whose proposal refresh failed: a partial conflict, with no instruction to retry the mutation."""
    return JSONResponse(
        {"error": SAVED_CHANGE_PENDING, "reason": "settings_saved_change_pending", "saved": True}, status_code=409
    )


def _refused_items(exc: SharedItemsError) -> JSONResponse:
    """On a server, settings refused for what they hold (409), each with a reason the dashboard words in its reader's
    language: more recognizers, field rules or patterns of one's own than it keeps for one identity, more projects, a
    save made while a change waits that cannot be composed with what was approved (``ChangeWaits``), and a change that
    would remove items set for everyone (``dropped``: how many of each list, never which), or a term too long. Other
    refusals keep their own words alone, which name a setting and a count, never an item."""
    if isinstance(exc, TooManyRules):
        return _refused_save(str(exc), 409, "settings_too_many_rules")
    if isinstance(exc, ChangeWaits):
        return _refused_save(str(exc), 409, "settings_change_waits")
    if isinstance(exc, TooManyProjects):
        return _refused_save(str(exc), 409, "settings_too_many_projects")
    if isinstance(exc, TermsTooLong):
        return _refused_save(str(exc), 409, "settings_terms_too_long")
    if not isinstance(exc, SettingsRefused):  # a change that drops items set for everyone, counted by list
        dropped = {"error": str(exc), "reason": "settings_drops_shared", "dropped": dict(exc.missing)}
        return JSONResponse(dropped, status_code=409)
    return _error(str(exc), 409)


def _same_etag(given: str, expected: str) -> bool:
    """Whether caller text is the generated etag, using the shared byte comparison."""
    return same_etag(given, expected)


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None}
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0]}
    cuts = quantiles(values, n=100, method="inclusive")
    return {"p50": round(cuts[49], 1), "p95": round(cuts[94], 1)}


def _parsed(body: bytes | bytearray) -> dict[str, Any] | None:
    """``body`` as a JSON object (an empty one: ``{}``); None for anything else: not JSON, nested deeper than Python
    reads, not an object."""
    try:
        data = json.loads(body or b"{}")
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _stats_of(parts: Iterable[Iterable[dict[str, Any]]], most: int | None = None) -> dict[str, Any]:
    """In a worker thread: what ``stats`` answers of the exchanges' summaries ``parts`` give, read a part at a time.
    ``most``: what the history was asked for at most (``STATS_MOST``); a read that reached it counts the latest of the
    period alone, and says so (``partial``, ``latest``)."""
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"exchanges": 0, "masked": 0, "blocked": 0})
    by_type: Counter[str] = Counter()
    by_agent: Counter[str] = Counter()
    latency, overhead = [], []
    cache_read = cache_total = 0
    read = 0
    for part in parts:
        for s in part:
            read += 1
            day = datetime.fromtimestamp(s["ts"]).strftime("%Y-%m-%d")
            by_day[day]["exchanges"] += 1
            by_day[day]["masked"] += s.get("new_masked", 0)
            by_day[day]["blocked"] += 1 if "blocked" in s.get("flags", []) else 0
            by_type.update(s.get("new_by_type") or {})
            by_agent[s.get("agent", "other")] += 1
            if s.get("latency_ms") is not None:
                latency.append(float(s["latency_ms"]))
            if s.get("overhead_ms") is not None:
                overhead.append(float(s["overhead_ms"]))
            tokens = s.get("tokens") or {}
            if tokens:
                cache_read += tokens.get("cache_read", 0)
                cache_total += tokens.get("input", 0) + tokens.get("cache_read", 0) + tokens.get("cache_write", 0)
    partial = {"partial": True, "latest": most} if most is not None and read >= most else {}
    return {
        "by_day": [{"day": d, **v} for d, v in sorted(by_day.items())],
        "by_type": dict(by_type),
        "by_agent": dict(by_agent),
        "latency_ms": _percentiles(latency),
        "overhead_ms": _percentiles(overhead),
        "cache_read_ratio": round(cache_read / cache_total, 4) if cache_total else None,
        **partial,
    }


_COUNTRY_OF = {spec.etype: spec.country for spec in ids.SPECS.values()}


class Dashboard:
    def __init__(
        self,
        runtime: Runtime,
        web_dir: Path | None = None,
        tutorial: TutorialApi | None = None,
        sign_ins: SignIns | None = None,
        docs_dir: Path | None = None,
    ) -> None:
        self.rt = runtime
        self.web_dir = web_dir or WEB_DIR
        self.docs_dir = docs_dir or DOCS_DIR  # the documentation of this version, when the package has it
        # The tutorial's sandbox: its routes, and what it refuses. Its cookie has a name of its own (a cookie of
        # 127.0.0.1 goes to every port): its sign-in and the daemon's never take each other's place.
        self.tutorial = tutorial
        self.cookie = f"latet_tutorial_{runtime.gateway.port}" if tutorial is not None else COOKIE
        # One-time codes and sessions: this process's memory on a computer, Valkey on a Latet server.
        if sign_ins is None:
            # The runtime's, when it has backends (none: a runtime made by hand, or a dashboard made for its files).
            backends = runtime.backends if runtime is not None else None
            sign_ins = backends.sign_ins if backends is not None else LocalSignIns()
        self.sign_ins: SignIns = sign_ins
        self._playground: dict[Profile | None, tuple[int, Masker]] = {}  # yours, and each profile tried
        self._agents: tuple[float, list[dict[str, Any]]] | None = None
        self._turns: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}  # one call at a time (``_turn``)
        self._repo_writes = threading.Lock()  # a project's latet.yaml compared and written by one save at a time

    # --- routes -----------------------------------------------------------------------------------

    def routes(self) -> list[BaseRoute]:
        guarded = self._guarded
        return [
            Route("/_latet/admin/login-code", self.login_code, methods=["POST"]),
            Route("/_latet/admin/reload", self.reload, methods=["POST"]),
            Route("/_latet/admin/changes/{id}", self.admin_change, methods=["GET", "POST"]),
            Route("/_latet/admin/repositories", self.repositories, methods=["GET"]),
            Route("/_latet/admin/repositories/forget", self.forget_repository, methods=["POST"]),
            Route("/_latet/admin/user-name", self.user_name_surrogate, methods=["POST"]),
            Route("/_latet/admin/destinations", self.add_destinations, methods=["POST"]),
            Route("/_latet/admin/known", self.known_values, methods=["POST"]),
            Route(f"{UI}/login", self.login, methods=["GET"]),
            Route(UI, self.ui_root, methods=["GET", "HEAD"]),
            Route(f"{UI}/{{path:path}}", self.ui, methods=["GET", "HEAD"]),
            Route(DOCS, self.docs_root, methods=["GET", "HEAD"]),
            Route(f"{DOCS}/{{path:path}}", self.docs, methods=["GET", "HEAD"]),
            Route(f"{API}/me", guarded(self.me), methods=["GET"]),
            Route(f"{API}/logout", guarded(self.logout), methods=["POST"]),
            Route(f"{API}/exchanges", guarded(self.exchanges), methods=["GET"]),
            Route(f"{API}/exchanges", guarded(self.clear_history), methods=["DELETE"]),
            Route(f"{API}/exchanges/{{id}}", guarded(self.exchange), methods=["GET"]),
            Route(f"{API}/images/{{id}}", guarded(self.image), methods=["GET"]),
            Route(f"{API}/exchanges/{{id}}/columns", guarded(self.columns), methods=["GET"]),
            Route(f"{API}/exchanges/{{id}}/columns/review", guarded(self.review_columns), methods=["POST"]),
            Route(f"{API}/sessions", guarded(self.sessions), methods=["GET"]),
            Route(f"{API}/reviews", guarded(self.reviews), methods=["GET"]),
            Route(f"{API}/vault", guarded(self.vault), methods=["GET"]),
            Route(f"{API}/vault/{{id}}/reveal", guarded(self.reveal), methods=["POST"]),
            Route(f"{API}/vault/{{id}}", guarded(self.forget), methods=["DELETE"]),
            Route(f"{API}/config", guarded(self.get_config), methods=["GET"]),
            Route(f"{API}/config", guarded(self.put_config), methods=["PUT"]),
            Route(f"{API}/rules/export", guarded(self.rules_export), methods=["GET"]),
            Route(f"{API}/rules/import", guarded(self.rules_import), methods=["POST"]),
            Route(f"{API}/allow", guarded(self.allow), methods=["POST"]),
            Route(f"{API}/terms", guarded(self.terms), methods=["POST"]),
            Route(f"{API}/playground", guarded(self.playground), methods=["POST"]),
            Route(f"{API}/recognizers/test", guarded(self.test_recognizer), methods=["POST"]),
            Route(f"{API}/recognizers/draft", guarded(self.draft_recognizer), methods=["POST"]),
            Route(f"{API}/reviewer/test", guarded(self.test_reviewer), methods=["POST"]),
            Route(f"{API}/reviewer/prompt", guarded(self.reviewer_prompt), methods=["GET"]),
            Route(f"{API}/reviewer/key", guarded(self.reviewer_key), methods=["GET", "PUT", "DELETE"]),
            Route(f"{API}/reviewer/certificate", guarded(self.reviewer_certificate), methods=["POST", "PUT"]),
            Route(f"{API}/projects", guarded(self.projects_list), methods=["GET"]),
            Route(f"{API}/projects", guarded(self.add_project), methods=["POST"]),
            Route(f"{API}/projects/{{id}}", guarded(self.project), methods=["GET"]),
            Route(f"{API}/projects/{{id}}/personal", guarded(self.put_personal), methods=["PUT"]),
            Route(f"{API}/projects/{{id}}/personal", guarded(self.delete_personal), methods=["DELETE"]),
            Route(f"{API}/projects/{{id}}/repo", guarded(self.put_repo), methods=["PUT"]),
            Route(f"{API}/projects/{{id}}/suggest", guarded(self.suggest_project), methods=["POST"]),
            Route(f"{API}/stats", guarded(self.stats), methods=["GET"]),
            Route(f"{API}/integrations", guarded(self.integrations), methods=["GET"]),
            Route(f"{API}/destinations/{{name}}", guarded(self.remove_destination), methods=["DELETE"]),
            Route(f"{API}/stream", guarded(self.stream), methods=["GET"]),
            Route(f"{API}/approvals", guarded(self.approvals), methods=["GET"]),
            Route(f"{API}/approvals/stream", guarded(self.approvals_stream), methods=["GET"]),
            Route(f"{API}/approvals/approve-all", guarded(self.approve_all), methods=["POST"]),
            Route(f"{API}/approvals/{{id}}", guarded(self.decide), methods=["POST"]),
            Route(f"{API}/approvals/{{id}}/images/{{item}}", guarded(self.approval_image), methods=["GET"]),
            Route(f"{API}/changes", guarded(self.changes), methods=["GET"]),
            Route(f"{API}/changes/{{id}}", guarded(self.decide_change), methods=["POST"]),
            *(self.tutorial.routes(guarded) if self.tutorial is not None else []),
            Route(f"{API}/{{rest:path}}", guarded(self.not_found), methods=["GET", "POST", "PUT", "DELETE"]),
        ]

    # --- access -----------------------------------------------------------------------------------

    def _origins(self) -> set[str]:
        if (space := self.rt.gateway.on_server) is not None:  # its server's address, as browsers name their origin
            return {space.url}
        port = self.rt.gateway.port
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}

    def _subject(self) -> str:
        """Whose dashboard this is, as the sign-ins name them: the identity whose space the runtime is on a Latet
        server, "" on a computer (you)."""
        identity = self.rt.identity
        return identity.id if identity is not None else ""

    def issue_code(self) -> str:
        """A one-time code that signs in here: for this dashboard's own subject."""
        return self.sign_ins.issue_code(self._subject())

    def _session_valid(self, request: Request) -> bool:
        if (space := self.rt.gateway.on_server) is not None:  # whom the server signed in, as it hands the request on
            return request.scope.get("latet.identity") == space.identity
        # The session of this dashboard's own subject, not any session: in a store of sign-ins that every identity of
        # a server shares, another identity's opens nothing here. ("" is you on a computer; no session answers None.)
        return self.sign_ins.subject_of(request.cookies.get(self.cookie) or "") == self._subject()

    def _guarded(self, handler: Handler) -> Handler:
        async def endpoint(request: Request) -> Response:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return _error("cross-site request", 403)
            origin = request.headers.get("origin")
            if origin is not None and origin not in self._origins():
                return _error("origin not allowed", 403)
            if not self._session_valid(request):
                return _error("unauthorized", 401)
            if request.method not in ("GET", "HEAD") and request.headers.get("x-latet-ui") != "1":
                return _error("missing X-Latet-UI header", 403)
            try:
                response = await handler(request)
            except Exception as exc:
                # By its handler's name, its type and its frames (where it failed), never its words: they can quote
                # what was sent (a setting refused quotes its value), and the path can hold a project's id, a hash of
                # its folder. The frames are shown as a server's log shows them, on a computer's daemon.log too
                # (``logs.frames_only``).
                name = getattr(handler, "__name__", "a handler")
                log.error("dashboard API error in %s: %s", name, type(exc).__name__, exc_info=exc)
                return _error("internal error", 500)
            response.headers.setdefault("cache-control", "no-store")
            return response

        return endpoint

    def _local_token(self, request: Request) -> bool:
        """The CLI's own calls: the local token, and never from a web page. In an identity's space on a server, the
        gate's word that its computer's token is the identity's: the gate hands on the CLI's two calls alone (the user
        name's fake, a destination), and the CLI's other calls refuse by themselves there (``_on_server``)."""
        if "origin" in request.headers:
            return False
        if (space := self.rt.gateway.on_server) is not None:
            return request.scope.get("latet.identity") == space.identity
        token = self.rt.gateway.bridge_token or ""
        return bool(token) and hmac.compare_digest(request.headers.get("x-latet-token", ""), token)

    def _on_server(self, what: str) -> Response | None:
        """In an identity's space on a Latet server, what stays the server's is refused: 409, saying what."""
        if self.rt.gateway.on_server is None:
            return None
        return _error(f"not on a Latet server: {what}", 409)

    def _shared(self) -> Config | None:
        """The settings the server shares, under this identity's; None on a computer."""
        settings = self.rt.settings
        return settings.shared() if isinstance(settings, OwnSettings) else None

    async def reload(self, request: Request) -> Response:
        """``latet reload`` / ``latet ner install``: rebuild the rules from the configuration file. Read,
        weighed and applied in one step (``Runtime.update_config``): a save made at the same moment is not undone."""
        if (refused := self._on_server("reading the settings again, which a space applies itself")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        # What lowers protection waits for you; the agent that asked is named.
        agent = request.headers.get("x-latet-agent") or None
        source: Literal["command", "file"] = "command" if request.headers.get("x-latet-command") else "file"
        unreadable: list[str] = []  # why the file cannot be read: nothing is then applied
        waiting: list[Change] = []
        notices: list[str] = []  # what the file holds that was left out, each by its place

        def read_first() -> RepositoryScan | None:
            """The repositories the file's settings are weighed in, read before the settings' lock (a share that does
            not answer must not hold it; the helper's deadlines bound the read, ``repository_io``); none when the
            settings the file holds could lower nothing in a repository (R24 M4). Read again under the lock: one that
            changed meanwhile and could lower something finds this snapshot incomplete, and is refused."""
            try:
                candidate = self.rt.settings.load()
            except (ValueError, OSError):  # said under the lock, where the file is read again
                candidate = None
            return self.rt.repositories_for(candidate)

        repositories = await asyncio.to_thread(read_first)

        def reloaded(current: Config) -> Config | None:
            settings = self.rt.settings
            try:
                config = settings.load()
            except ValidationError as exc:  # a store that did not word it: by its places, never what the file holds
                unreadable.append(f"cannot read the configuration: {'; '.join(problems_of(exc))}")
                return None
            except (ValueError, OSError) as exc:  # a ConfigError's words say where, never what
                unreadable.append(f"cannot read the configuration: {exc}")
                return None
            notices.extend(getattr(settings, "notices", ()))
            change = self.rt.propose(config, source, agent, repositories=repositories)
            if change is not None:
                waiting.append(change)
                return None
            return config

        try:
            await asyncio.to_thread(self.rt.update_config, reloaded, save=False)
        except RepositoriesUnchecked:
            return _unchecked()
        if unreadable:
            return _error(unreadable[0], 400)
        said = {"notices": notices} if notices else {}
        if waiting:
            return JSONResponse({"ok": True, "applied": False, "pending": waiting[0].summary(), **said})
        version = self.rt.built.engine.rules_version
        problems = [s.as_dict() for s in self.rt.built.recognizers if s.errors or s.warnings]
        return JSONResponse({"ok": True, "applied": True, "rules_version": version, "recognizers": problems, **said})

    async def repositories(self, request: Request) -> Response:
        """``latet repositories``: the repositories this computer remembers, each with its state as the helper reads it
        now and why, in fixed categories (``projects.repository_states``), off the event loop. The CLI's own call."""
        if (refused := self._on_server("the repositories a computer remembers")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        seen = self.rt.projects.seen if self.rt.projects is not None else None
        if seen is None:
            return JSONResponse({"repositories": [], "list": "readable"})
        return JSONResponse(await asyncio.to_thread(repository_states, seen))

    async def forget_repository(self, request: Request) -> Response:
        """``latet repositories forget``: ``{folder}`` taken out of the list and of the daemon's memory, under the
        list's lock (``SeenRepositories.forget``); the command refuses an agent before it asks. 404 for a folder not
        remembered; 503 while the list cannot be read (nothing is written over it)."""
        if (refused := self._on_server("the repositories a computer remembers")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        data = await self._json(request)
        folder = data.get("folder") if data is not None else None
        if not isinstance(folder, str) or not folder.strip() or len(folder) > 4096:
            return _error("folder: a folder as the list shows it", 400)
        seen = self.rt.projects.seen if self.rt.projects is not None else None
        outcome = await asyncio.to_thread(seen.forget, folder) if seen is not None else "unknown"
        if outcome == "forgotten":
            return JSONResponse({"forgotten": True})
        if outcome == "unknown":
            return _error("not one Latet remembers", 404)
        return JSONResponse(
            {"error": "the list of the repositories Latet has seen cannot be read or written now: nothing changed"},
            status_code=503,
            headers={"Retry-After": "1"},
        )

    async def admin_change(self, request: Request) -> Response:
        """The CLI's view of a change waiting (GET), and your answer to it at your own terminal (POST
        ``{action}``), where the CLI asked you first."""
        if (refused := self._on_server("a change decided at a computer: it is decided in the dashboard")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        change_id = request.path_params["id"]
        if request.method == "GET":
            approvals = self.rt.mediator.approvals
            waiting = change_id in approvals.changes
            return JSONResponse({"waiting": waiting, "state": None if waiting else approvals.settled.get(change_id)})
        return await self._decide_change(change_id, request)

    async def changes(self, request: Request) -> Response:
        return JSONResponse({"changes": self.rt.mediator.approvals.pending_changes()})

    async def decide_change(self, request: Request) -> Response:
        """``{action: "approve" | "reject"}`` for a change to your settings that lowers protection."""
        return await self._decide_change(request.path_params["id"], request)

    async def _decide_change(self, change_id: str, request: Request) -> Response:
        action = (await self._json(request) or {}).get("action")
        if action not in ("approve", "reject"):
            return _error("action is approve or reject", 400)

        def decided() -> Response:
            try:
                state = self.rt.decide_change(change_id, action == "approve")
            except SharedItemsError as exc:  # on a server, more than it keeps for one identity: none of it runs
                return _refused_items(exc)
            except OSError as exc:  # on a server, settings their store did not keep: none of them run
                return self._unkept(exc)
            if state == ("approved" if action == "approve" else "rejected"):
                return JSONResponse({"ok": True, "state": state})
            if state == "stale":
                return _error("your settings changed since: it was dropped, ask for it again", 409)
            return _error("it is not waiting any more", 409)

        if self.rt.gateway.work is not None:
            return await self._saved(decided)
        return await asyncio.to_thread(decided)

    async def user_name_surrogate(self, request: Request) -> Response:
        """``auto_mode_review.user_name: surrogate``: the OS user name's surrogate, which
        ``latet agents apply claude`` gives Claude Code as its user name. In a space on a Latet server, the user name of
        the computer that asks, which it sends, and only when the space's settings want a surrogate
        (``_computer_user_name``)."""
        if not self._local_token(request):
            return _error("unauthorized", 401)
        if self.rt.gateway.on_server is not None:
            return await self._computer_user_name(request)
        from latet.config import os_user_name

        name = os_user_name()
        if not name:
            return _error("the OS user name is unknown", 404)
        vault = self.rt.built.vault
        entity = await asyncio.to_thread(vault.intern, "USERNAME", EntityClass.IDENTITY, name)
        return JSONResponse({"surrogate": entity.surrogate})

    async def add_destinations(self, request: Request) -> Response:
        """``latet claude``, ``latet codex``, ``global-install``: destinations for the addresses an
        agent's settings name, in use at once (adding one lowers nothing). Answers their names. In a space on a Latet
        server, a computer's agent names them, and one only the server would reach is refused there
        (``_computer_destinations``)."""
        if not self._local_token(request):
            return _error("unauthorized", 401)
        if self.rt.gateway.on_server is not None:
            return await self._computer_destinations(request)
        if (refused := self._sandboxed("a destination (its only model is the pretend one)")) is not None:
            return refused
        items = (await self._json(request) or {}).get("destinations")
        shape = DESTINATIONS_SHAPE
        if not isinstance(items, list) or not 0 < len(items) <= 20:
            return _error(shape, 400)
        wanted: list[tuple[str, Literal["claude", "codex"] | None]] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                return _error(shape, 400)
            agent = item.get("agent")
            if agent not in (None, "claude", "codex"):
                return _error(shape, 400)
            wanted.append((item["url"], agent))
        try:
            names = await asyncio.to_thread(self.rt.add_destinations, wanted)
        except ValidationError as exc:
            reason = exc.errors(include_url=False, include_context=False, include_input=False)[0]["msg"]
            return _error(f"not an address Latet can send to: {reason}", 400)
        return JSONResponse({"names": names})

    def _no_computer(self) -> Response | None:
        """In a space on a server, an API key's: it has no computer, and its settings are the administration's (403),
        before anything of the call is read. None for a person's."""
        identity = self.rt.identity
        return None if identity is not None and identity.kind == "person" else _error(NO_COMPUTER, 403)

    async def _in_work(self, job: Callable[[], Response]) -> Response:
        """``job``, the work of a computer's call or of a save of the settings, which answers what it raises itself: on
        a server in its work threads (``GatewayConfig.work``), never the event loop nor the default executor, which the
        spaces' builds hold. Escaped failures answer 503 with ``Retry-After: 1``: controlled worker failures were
        logged already; unexpected failures are logged here by type, paced. This boundary cannot say whether earlier
        work was kept. On a computer, and in a space made without them (a test's), a worker thread, what it raises
        raised again."""
        work = self.rt.gateway.work
        if work is None:
            return await asyncio.to_thread(job)
        from latet.server.machines import StoresUnavailable  # only a server supplies these work threads

        try:
            return await work(job)
        except StoresUnavailable:  # the normal worker wrapper, including CredentialsBusy, has already logged it
            pass
        except Exception as exc:
            if count := _WORK_UNANSWERED.due():
                log.warning(
                    "server dashboard work did not finish (%s)%s",
                    described(exc),
                    f" ({count} times since the line before)" if count > 1 else "",
                )
        return JSONResponse({"error": _UNANSWERED}, status_code=503, headers={"retry-after": "1"})

    async def _in_pool[T](
        self, work: Callable[..., T], *args: Any, _held_turn: asyncio.Lock | None = None, **kwargs: Any
    ) -> T:
        """A server dashboard's reads in its process's two threads, with one outstanding job per identity.
        Cancellation leaves the turn held until the submitted job finishes; callers waiting for it hold no thread.
        A computer retains its default executor and both paths carry the caller's context."""
        if self.rt.gateway.work is None:
            return await asyncio.to_thread(work, *args, **kwargs)
        global _DASHBOARD_POOL
        turn = self._turn("pool")
        acquired = False
        try:
            await turn.acquire()
            acquired = True
            with _DASHBOARD_POOL_LOCK:
                if _DASHBOARD_POOL is None:
                    _DASHBOARD_POOL = ThreadPoolExecutor(2, thread_name_prefix="latet-dashboard")
                pool = _DASHBOARD_POOL
            job = functools.partial(work, *args, **kwargs)
            running = asyncio.get_running_loop().run_in_executor(pool, contextvars.copy_context().run, job)
        except BaseException:
            if acquired:
                turn.release()
            if _held_turn is not None:
                _held_turn.release()
            raise

        def finished(done: asyncio.Future[T]) -> None:
            turn.release()
            if _held_turn is not None:
                _held_turn.release()
            if not done.cancelled():
                done.exception()  # a cancelled requester may no longer await a failed job

        running.add_done_callback(finished)
        return await asyncio.shield(running)

    async def _computer_user_name(self, request: Request) -> Response:
        """On a server: ``{name}``, the user name of the computer that asks, read no further than ``USER_NAME_BYTES``
        (413), then in the server's work threads parsed, checked (``USER_NAME_SHAPE``: anything else 400, naming the
        field, nothing interned) and, when the space's settings want a surrogate, interned as it was sent (never
        stripped, folded or normalized); ``{surrogate: null}`` when they keep the user name. The server's own user name
        is never read. A vault that takes no new value (past its share, no free fake) answers 409 in its own words,
        which name no value."""
        if (refused := self._no_computer()) is not None:
            return refused
        raw = await _within(request, USER_NAME_BYTES)
        if raw is None:
            return _error(f"a user name takes {USER_NAME_BYTES} bytes at most", 413)

        def interned() -> Response:
            data = _parsed(raw)
            name = data.get("name") if data is not None and set(data) == {"name"} else None
            if not _a_user_name(name):
                return _error(USER_NAME_SHAPE, 400)
            if self.rt.config.auto_mode_review.user_name != "surrogate":
                return JSONResponse({"surrogate": None})  # the space's settings keep the user name
            try:
                entity = self.rt.built.vault.intern("USERNAME", EntityClass.IDENTITY, name)
            except MaskingError as exc:
                return _error(str(exc), 409)
            return JSONResponse({"surrogate": entity.surrogate})

        return await self._in_work(interned)

    async def _computer_destinations(self, request: Request) -> Response:
        """On a server: the destinations a computer's agent names (``_wanted``: 20 at most, each address written as a
        saved one is, ``ADDRESS_CHARS`` of printable ASCII at most), read no further than ``DESTINATIONS_BYTES`` (413),
        then in the server's work threads each address the settings in use do not have judged as the identity's own
        destination is (``personal_destination_problem``, the server's own address and the resolver bounded and
        counted for the identity: a shared destination's address is named, never judged), the first refused answered
        400, and the destinations added (``Runtime.add_destinations``): refused whole (409) when an address that was not
        judged is new to the settings by then. A refusal of the save is answered as the dashboard's other saves are."""
        if (refused := self._no_computer()) is not None:
            return refused
        raw = await _within(request, DESTINATIONS_BYTES)
        if raw is None:
            return _error(f"destinations take {DESTINATIONS_BYTES} bytes at most", 413)
        space, me = self.rt.gateway.on_server, self.rt.identity

        def added() -> Response:
            wanted = _wanted(_parsed(raw))
            if isinstance(wanted, Response):
                return wanted
            known = {d.url for d in self.rt.config.upstreams}
            resolve = egress.bounded(me.id if me is not None else None)
            server = space.url if space is not None else None
            judged: set[str] = set()
            for url, _agent in wanted:
                if url in known or url in judged:
                    continue
                if (problem := personal_destination_problem(url, resolve=resolve, server=server)) is not None:
                    return _error(f"{url}: {problem}", 400)
                judged.add(url)
            try:
                names = self.rt.add_destinations(wanted, judged=judged)
            except SettingsSavedChangePending:
                return _saved_change_pending()
            except ValidationError as exc:
                reason = exc.errors(include_url=False, include_context=False, include_input=False)[0]["msg"]
                return _error(f"not an address Latet can send to: {reason}", 400)
            except ChangedMeanwhile as exc:
                return _error(str(exc), 409)
            except SharedItemsError as exc:
                return _refused_items(exc)
            except OSError as exc:
                return self._unkept(exc)
            return JSONResponse({"names": names})

        return await self._in_work(added)

    async def known_values(self, request: Request) -> Response:
        """``latet rules add``: whether the examples of a rule are real values (already in the vault).
        Answers yes or no for each value, never anything about it."""
        if (refused := self._on_server("the CLI's look into the vault")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        values = (await self._json(request) or {}).get("values")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values) or len(values) > 1000:
            return _error("values: a list of strings", 400)
        vault = self.rt.built.vault
        return JSONResponse({"known": [vault.by_original(v) is not None for v in values]})

    async def login_code(self, request: Request) -> Response:
        """``latet dashboard`` asks for a one-time code with the local token."""
        if (refused := self._on_server("the CLI's sign-in code: a machines' call gives one")) is not None:
            return refused
        if not self._local_token(request):
            return _error("unauthorized", 401)
        code = self.issue_code()
        port = self.rt.gateway.port
        return JSONResponse({"code": code, "url": f"http://127.0.0.1:{port}{UI}/login?code={code}"})

    async def login(self, request: Request) -> Response:
        own = self._subject()
        # A code unknown, used or late answers None; one made for another dashboard's subject opens no session here.
        if self.sign_ins.redeem(request.query_params.get("code", "")) != own:
            return RedirectResponse(f"{UI}/?login=expired", status_code=303, headers=_SECURITY_HEADERS)
        sid = self.sign_ins.open(own)
        response = RedirectResponse(f"{UI}/", status_code=303, headers=_SECURITY_HEADERS)
        response.set_cookie(
            self.cookie, sid, max_age=int(SESSION_TTL), path="/_latet", httponly=True, samesite="strict"
        )
        return response

    async def logout(self, request: Request) -> Response:
        """You sign out: this browser's session ends wherever it is checked (on a server, on every instance: the
        sign-ins are Valkey's), and its cookie goes. Behind ``_guarded``: no page of another origin signs anyone out."""
        sid = request.cookies.get(self.cookie) or ""
        if sid:
            await self._in_pool(self.sign_ins.end, sid=sid)  # on a server a round trip to Valkey: off the loop
        space = self.rt.gateway.on_server
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            self.cookie, path="/_latet", httponly=True, samesite="strict",
            secure=space is not None and space.url.startswith("https://"),
        )  # fmt: skip
        return response

    # --- static UI --------------------------------------------------------------------------------

    async def ui_root(self, request: Request) -> Response:
        return RedirectResponse(f"{UI}/", status_code=308)

    async def ui(self, request: Request) -> Response:
        return ui_file(self.web_dir, request.path_params.get("path", ""))

    # --- the documentation of this version: public, no session ------------------------------------------------------

    async def docs_root(self, request: Request) -> Response:
        return RedirectResponse(f"{DOCS}/", status_code=308)

    async def docs(self, request: Request) -> Response:
        return docs_file(self.docs_dir, request.path_params.get("path", ""))

    # --- API ----------------------------------------------------------------------------------------

    async def not_found(self, request: Request) -> Response:
        return _error("not found", 404)

    def _sandboxed(self, what: str) -> Response | None:
        """In the tutorial's sandbox, what would reach outside it (a reviewer's server, a real address, your
        projects' files) is refused: 409, saying what. None anywhere else."""
        if self.tutorial is None:
            return None
        return _error(f"not in the tutorial's sandbox: {what}", 409)

    async def me(self, request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "profile": self.rt.config.profile.value,
                "port": self.rt.gateway.port,
                "data_dir": str(self.rt.data) if self.rt.gateway.on_server is None else "",  # nothing of a server's
                "recording": self.rt.exchanges is not None,
                # The tutorial's sandbox: the chapter in progress (null on a real daemon).
                "tutorial": self.tutorial.session.me() if self.tutorial is not None else None,
                "server": self._server_me(),
                # Whether this install serves the documentation of its version at /_latet/docs/: the dashboard's links
                # open it there, else on docs.latet.ai.
                "docs": self.has_docs(),
            }
        )

    def has_docs(self) -> bool:
        """Whether this install serves the documentation of its version (built and checked) at /_latet/docs/."""
        return docs_headers(self.docs_dir) is not None

    def _server_me(self) -> dict[str, Any] | None:
        """On a Latet server: its address, and whose space this is, a person's or an API key's. None on a computer."""
        space, ref = self.rt.gateway.on_server, self.rt.identity
        if space is None:
            return None
        who = {"id": ref.id, "name": ref.name, "kind": ref.kind} if ref is not None else None
        return {"url": space.url, "identity": who}

    async def exchanges(self, request: Request) -> Response:
        store = self.rt.exchanges
        if store is None:
            return JSONResponse({"items": [], "next": None})
        q = request.query_params
        try:
            limit = max(1, min(500, int(q.get("limit", "100"))))
        except ValueError:
            return _error("invalid limit", 400)
        items, next_id = await self._in_pool(
            store.list, limit, q.get("before"), q.get("session"), q.get("agent"), q.get("flag"), q.get("project")
        )
        return JSONResponse({"items": items, "next": next_id})

    async def reviews(self, request: Request) -> Response:
        """What the local reviewer said of each block it read: categories and where, never values."""
        store = self.rt.exchanges
        if store is None:
            return JSONResponse({"items": []})
        try:
            limit = max(1, min(1000, int(request.query_params.get("limit", "100"))))
        except ValueError:
            return _error("invalid limit", 400)
        items = await self._in_pool(store.reviews, limit, request.query_params.get("session"))
        return JSONResponse({"items": items})

    async def clear_history(self, request: Request) -> Response:
        """Delete every recorded exchange (the vault is kept: it is what restores the originals)."""

        def cleared() -> Response:
            if self.rt.exchanges is not None:
                self.rt.exchanges.clear()
            return JSONResponse({"ok": True})

        if self.rt.gateway.work is not None:
            return await self._saved(cleared)
        if self.rt.exchanges is not None:
            await asyncio.to_thread(self.rt.exchanges.clear)
        return JSONResponse({"ok": True})

    async def exchange(self, request: Request) -> Response:
        store = self.rt.exchanges
        if store is None:
            return _error("not found", 404)
        detail = await self._in_pool(store.detail, request.path_params["id"])
        if detail is None:
            return _error("not found", 404)
        return JSONResponse(await self._in_pool(restore_detail, detail, self.rt.built.vault))

    async def image(self, request: Request) -> Response:
        """An image of a request as it was sent (masked, or with nothing to mask)."""
        store = self.rt.exchanges
        found = await self._in_pool(store.image, request.path_params["id"]) if store is not None else None
        if found is None:
            return _error("not found", 404)
        media, data = found
        headers = {"cache-control": "private, max-age=86400, immutable", "x-content-type-options": "nosniff"}
        return Response(
            data, media_type=media if media.startswith("image/") else "application/octet-stream", headers=headers
        )

    # --- strict data mode, guided -------------------------------------------------------------------

    def _tool_output(self, request: Request) -> _ToolOutput | Response:
        """One tool's output in an exchange (``?slot=``), as the history keeps it, with the project it was recorded
        in and the rules in effect there."""
        from latet.engine.detectors.fields import named_source, strict_covers

        history = self.rt.config.history
        if history != "full":
            return _error(f"history: {history} keeps no texts, and the columns are read from them", 409)
        try:
            index = int(request.query_params.get("slot", ""))
        except ValueError:
            return _error("invalid slot", 400)
        store = self.rt.exchanges
        detail = store.detail(request.path_params["id"]) if store is not None else None
        if detail is None:
            return _error("not found", 404)
        slots = detail["request"]["slots"]
        if not 0 <= index < len(slots) or slots[index]["kind"] != SlotKind.TOOL_RESULT.value:
            return _error("not found: no tool output there", 404)
        slot = slots[index]
        recorded = detail["summary"].get("project") or None
        project = self._find_project(recorded["id"]) if recorded else None
        config = project.config if project is not None else self.rt.config
        # Strict data mode covers this tool's output, by the rule the masking follows (files and web pages left alone
        # unless `data_files` or `data_hosts` names them, `data_tools`), with the file or the address it came from. A
        # request that did not name the tool counts as one of a tool with no name, as the masking counted it. An
        # exchange recorded before Latet kept the names of tools has none either: it is answered as for a tool with no
        # name (covered unless `data_tools` is set), whatever the tool was, so a file read of then can be said to be
        # covered although the masking left it alone; one recorded before Latet kept the sources of outputs is answered
        # as for a file or an address no rule names. Such exchanges leave with the history's retention.
        source = restored_source(slot.get("source"), self.rt.built.vault)
        applies = strict_covers(slot.get("tool"), config.data_tools, source, config.data_files, config.data_hosts)
        # A file or an address a rule names was masked as data with strict data mode off too: nothing to turn on.
        named = named_source(slot.get("tool"), source, config.data_files, config.data_hosts)
        return _ToolOutput(slot, recorded, project, config, config.data_mode() == "strict" or named, applies)

    def _output_columns(self, request: Request) -> tuple[list[Column], dict[str, Any]] | Response:
        """The columns of one tool's output in an exchange (``?slot=``), each with its proposal, read from
        the original the history keeps; and what the dashboard needs around them."""
        from latet.columns import columns_of, propose, strict_values

        found = self._tool_output(request)
        if isinstance(found, Response):
            return found
        slot, project = found.slot, found.project
        original, spans = reconstruct(slot["masked"], slot["replacements"], self.rt.built.vault)
        masker = project.scope.masker if project is not None and project.scope is not None else self.rt.built.masker
        # What a rule for everywhere would not change there: your rules for the project, which come before the global
        # ones (a rule for the project does change it); and what no choice of yours changes there: the latet.yaml's
        # rules in effect, which apply when stricter than yours (Ruling 098-R2), not the ones set aside.
        nearer = project.personal.fields if project is not None and project.personal is not None else []
        held = file_rules(found.config) if project is not None else []
        columns = propose(
            columns_of(original, strict_values(original, spans)), masker, found.config.fields, nearer, held
        )
        around = {
            "strict": found.strict,
            "applies": found.applies,
            "project": {**found.recorded, "loaded": project is not None} if found.recorded else None,
            "reviewer": self.rt.config.llm_review.enabled,
        }
        return columns, around

    def _output_shape(self, request: Request) -> dict[str, Any] | Response:
        """What the exchange's notice needs of a tool's output (``?shape=1``): whether strict data mode is on where
        the exchange was, whether it covers the tool, and, only when it does, the names of the output's columns. No
        proposal is worked out, so no detector runs (name detection included); an output strict data mode does not
        cover is not even read for columns."""
        from latet.columns import columns_of

        found = self._tool_output(request)
        if isinstance(found, Response):
            return found
        names: list[str] = []
        if found.applies:
            original, _ = reconstruct(found.slot["masked"], found.slot["replacements"], self.rt.built.vault)
            names = [column.name for column in columns_of(original)]
        return {"columns": names, "strict": found.strict, "applies": found.applies}

    async def columns(self, request: Request) -> Response:
        """Strict data mode, guided: which columns of a tool's output could go to the model in clear.
        Worked out when asked: nothing is stored, and nothing applies by itself. ``?shape=1``: the light answer the
        exchange's notice asks, the columns' names alone (``_output_shape``)."""
        if request.query_params.get("shape") == "1":
            shape = await self._in_pool(self._output_shape, request)
            return shape if isinstance(shape, Response) else JSONResponse(shape)
        found = await self._in_pool(self._output_columns, request)
        if isinstance(found, Response):
            return found
        columns, around = found
        return JSONResponse({"columns": [_column_view(c) for c in columns], **around})

    async def review_columns(self, request: Request) -> Response:
        return await self._reviewer_call(request, self._review_columns)

    async def _review_columns(self, request: Request) -> Response:
        """The local reviewer's opinion on each column: shown beside the proposals, never applied."""
        from latet.engine.reviewer.client import ReviewError
        from latet.engine.reviewer.columns import opinions
        from latet.engine.reviewer.reviewer import client_for

        if (refused := self._sandboxed("the local reviewer")) is not None:
            return refused
        settings = self.rt.config.llm_review
        if not settings.enabled:
            return _error("the local reviewer is off: turn it on in Rules first", 409)
        found = await self._in_pool(self._output_columns, request)
        if isinstance(found, Response):
            return found
        columns, _ = found
        client = await self._in_pool(client_for, settings, 120, key_of=self.rt.reviewer_key)
        try:
            said = await opinions(client, columns) if columns else {}
        except ReviewError as exc:
            return _error(str(exc), 502)
        finally:
            await client.aclose()
        return JSONResponse({"opinions": said})

    async def sessions(self, request: Request) -> Response:
        store = self.rt.exchanges
        if store is None:
            return JSONResponse({"items": []})
        try:
            limit = max(1, min(500, int(request.query_params.get("limit", "50"))))
        except ValueError:
            return _error("invalid limit", 400)
        return JSONResponse({"items": await self._in_pool(store.sessions, limit)})

    async def vault(self, request: Request) -> Response:
        q = request.query_params.get("q", "").strip().lower()
        etype = request.query_params.get("type", "").strip().upper()
        eclass = request.query_params.get("class", "").strip().upper()
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            per_page = max(10, min(200, int(request.query_params.get("per_page", "50"))))
        except ValueError:
            return _error("invalid page", 400)
        # In a thread: thousands of conversations of hundreds of values take seconds to count, a vault of 200,000 values
        # to sort, and on a server the conversations are read from Valkey; the event loop serves everyone meanwhile.
        return JSONResponse(await self._in_pool(self._vault_page, q, etype, eclass, page, per_page))

    def _vault_page(self, q: str, etype: str, eclass: str, page: int, per_page: int) -> dict[str, Any]:
        """One page of the vault's values, the newest first (by id among values of the same instant, so a value is on
        one page only), with how many match, the page (a page past the last is the last) and every type the vault holds
        (the menu of types does not shrink as you filter)."""
        types: Counter[str] = Counter()
        matched = []
        for e in self.rt.built.vault.entities():
            types[e.etype] += 1
            if (etype and e.etype != etype) or (eclass and e.eclass.name != eclass):
                continue
            if q and not (q in e.surrogate.lower() or q in e.etype.lower() or q == e.id or q in e.original.lower()):
                continue
            matched.append(e)
        matched.sort(key=lambda e: (-e.created, e.id))
        pages = max(1, -(-len(matched) // per_page))
        page = min(page, pages)
        shown = matched[(page - 1) * per_page : page * per_page]
        ids = {e.id for e in shown}
        # Counted for the page's values only. tuple(): a copy taken at once, while a request may be showing the
        # conversation one more entity.
        used = Counter(i for s in self.rt.built.engine.sessions() for i in tuple(s.entity_ids) if i in ids)
        items = [
            {
                "id": e.id,
                "type": e.etype,
                "class": e.eclass.name,
                "surrogate": e.surrogate,
                "created": e.created,
                "sessions": used[e.id],
            }
            for e in shown
        ]
        return {"items": items, "total": len(matched), "page": page, "pages": pages, "per_page": per_page,
                "types": dict(types)}  # fmt: skip

    async def reveal(self, request: Request) -> Response:
        entity = self.rt.built.vault.get(request.path_params["id"])
        if entity is None:
            return _error("not found", 404)
        return JSONResponse({"original": entity.original})

    async def forget(self, request: Request) -> Response:
        # In a thread, as its twins: the runtime's lock is held for seconds by a save or a catch-up of the vault, and on
        # a server the forgetting is a transaction of PostgreSQL; the event loop serves every identity meanwhile.
        def forgotten() -> Response:
            if self.rt.forget(request.path_params["id"]) is None:
                return _error("not found", 404)
            return JSONResponse({"ok": True})

        if self.rt.gateway.work is not None:
            return await self._saved(forgotten)
        return await asyncio.to_thread(forgotten)

    # --- rules --------------------------------------------------------------------------------------

    def _config_view(self, c: Config) -> dict[str, Any]:
        """``c``, the settings in use as the caller read them once, as the Rules page shows them."""
        from latet.runtime import resolve_identity

        identity = resolve_identity(c, detect=self.rt.identity is None) or c.identity
        usernames = list(dict.fromkeys([*identity.usernames, *([identity.username] if identity.username else [])]))
        return {
            "profile": c.profile.value,
            "pii": c.pii,
            "pii_available": sorted(ALL_PII),
            # Every type with its country (null: not tied to one), in the order the Rules page lists them.
            "pii_catalog": [{"type": t, "country": _COUNTRY_OF.get(t)} for t in ALL_PII],
            "pii_base": list(BASE_PII),
            "pii_effective": []
            if c.profile == Profile.SECRETS
            else effective_pii(c.pii, c.id_regions, every_region=c.profile == Profile.MAX),
            "id_regions": c.id_regions,
            "id_regions_available": list(ID_REGIONS),
            "phone_regions": c.phone_regions,
            "include_private_ips": c.include_private_ips,
            "terms": [t.model_dump() for t in c.terms],
            "recognizers": c.recognizers,
            "recognizer_status": [status.as_dict() for status in self.rt.built.recognizers],
            "checksums": [{"name": k.name, "description": k.description} for k in checksums.CHECKSUMS.values()],
            "allow_values": c.allow_values,
            "allow_patterns": c.allow_patterns,
            "policies": {k: str(v) for k, v in c.policies.items()},
            "history": c.history,
            "retention_days": c.retention_days,
            "ner": c.ner,
            "auto_mode_review": {d: getattr(c.auto_mode_review, d) for d in REVIEW_DETAILS},
            "data": c.data,  # null: the profile decides
            "data_effective": c.data_mode(),
            "data_tools": c.data_tools,
            "data_files": c.data_files,
            "data_hosts": c.data_hosts,
            "fields": [f.model_dump(exclude_defaults=True) for f in c.fields],
            "dump_guard": c.dump_guard,
            "images": c.images,
            "ocr": c.ocr,
            "ocr_status": self._ocr_status(),
            "llm_review": c.llm_review.model_dump(mode="json"),
            "live_review": c.live_review.model_dump(mode="json"),
            "upstreams": [d.model_dump(exclude_none=True) for d in c.upstreams],
            "ner_status": self._ner_status(),
            "identity": {
                "enabled": c.identity.enabled,
                "usernames": usernames,
                "hostname": identity.hostname,
                # More host names, masked as `hostname` is: on a Latet server, those of the identity's other computers.
                "hostnames": identity.hostnames,
                "full_name": identity.full_name,
                "emails": identity.emails,
            },
        }

    def _ocr_status(self) -> dict[str, Any]:
        from latet.vision import models

        vision = self.rt.mediator.vision
        reads = vision is not None and vision.available
        # The models are in this data folder, or the daemon holds a reader that works: the tutorial's sandbox has a
        # folder of its own, with none, and reads with the models of yours.
        return {
            "runtime": models.runtime_available(),
            "model": models.installed(self.rt.data) or reads,
            "active": reads and self.rt.mediator.ocr,
        }

    def _ner_status(self) -> dict[str, Any]:
        from latet.engine.detectors import ner

        active = any(d.id == "ner" for d in self.rt.built.masker.detectors)
        return {"runtime": ner.runtime_available(), "model": ner.is_installed(self.rt.data), "active": active}

    async def _json(self, request: Request) -> dict[str, Any] | None:
        """The request's body as a JSON object; None for none that is one, or past ``_MAX_BODY``. Read into one buffer
        (no copy of what was read at each part), and parsed in a worker thread past ``PARSED_ON_LOOP`` bytes: every
        handler's, the settings' and the rules' included."""

        turn = None
        if self.rt.gateway.work is not None:
            try:
                small = 0 <= int(request.headers.get("content-length", "")) <= PARSED_ON_LOOP
            except ValueError:
                small = False
            if not small:
                turn = self._turn("body")
                await turn.acquire()
        try:
            body = await _within(request, _MAX_BODY)
            if body is None:
                return None
            if len(body) > PARSED_ON_LOOP:
                held, turn = turn, None  # the pool releases this body turn when the actual parse ends
                return await self._in_pool(_parsed, body, _held_turn=held)
            return _parsed(body)
        finally:
            if turn is not None:
                turn.release()

    async def rules_export(self, request: Request) -> Response:
        """Your rules as a file, for a colleague; ``?project=``: your own settings for that project."""
        from latet.transfer import export_rules

        root, name = None, "latet-rules"
        if pid := request.query_params.get("project"):
            project = await self._in_pool(self._find_project, pid)
            if project is None:
                return _error("not found", 404)
            root, name = project.root, "latet-rules-" + re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-")
        vault = self.rt.built.vault

        def known(values: list[str]) -> list[bool]:
            return [vault.by_original(v) is not None for v in values]

        text, warnings = await self._in_pool(export_rules, self.rt.config, root, known)
        return JSONResponse({"name": f"{name}-{time.strftime('%Y-%m-%d')}.yaml", "file": text, "warnings": warnings})

    async def rules_import(self, request: Request) -> Response:
        """A rules file: what it would do (``apply`` false), or done. With ``all`` also what lowers
        protection, which you chose here, in the dashboard; ``project``: into your settings for it."""
        from latet.transfer import RulesError, merge_rules, read_rules

        data = await self._json(request)
        if data is None or not isinstance(data.get("file"), str):
            return _error("file: the text of a rules file", 400)
        root = None
        if pid := data.get("project"):
            # What adds protection there is measured against the project's own file, which a server has none of: it
            # would read the folder of that name on the server's own disk.
            if (refused := self._on_server("your projects' files: the server has none of them")) is not None:
                return refused
            project = await self._in_pool(self._find_project, str(pid))
            if project is None:
                return _error("not found", 404)
            root = project.root
        projects = self.rt.projects

        def seen() -> RepositoryScan:
            """The repositories seen with a latet.yaml of their own, before the daemon's start too: what the import is
            weighed in, the same snapshot a change of config.yaml is weighed in (``Runtime.propose``). In a thread,
            outside every lock: the files of those seen before are read again, by the helper within its deadlines. One
            not complete classifies nothing: the import is refused, as a reload is (``_unchecked``)."""
            scan = projects.repositories() if projects is not None else RepositoryScan()
            if not scan.complete:
                raise RepositoriesUnchecked()
            return scan

        try:
            settings, notices = await self._in_pool(read_rules, data["file"])
            merged = await self._in_pool(lambda: merge_rules(self.rt.config, settings, root, seen()))
        except RulesError as exc:
            return _error(str(exc), 422)
        except RepositoriesUnchecked:
            return _unchecked()
        merged.report.notices = notices
        out: dict[str, Any] = {"report": merged.report.as_dict(), "applied": False}
        everything = bool(data.get("all"))
        if not data.get("apply") or not (merged.report.added or (everything and merged.report.held)):
            return JSONResponse(out)

        def imported() -> Response:
            try:
                snapshot = seen()  # read again for the apply, before the settings' lock
            except RepositoriesUnchecked:
                return _unchecked()

            def import_rules(current: Config) -> Config:
                # Over the settings as they are when it applies, and the repositories as read a moment ago with the
                # projects of this process as they are now (``with_active``: one reached since is weighed too).
                weighed = projects.with_active(snapshot) if projects is not None else snapshot
                again = merge_rules(current, settings, root, weighed)
                return again.full if everything else again.protective

            try:
                self.rt.update_config(import_rules)
            except SettingsSavedChangePending:
                return _saved_change_pending()
            except RulesError as exc:
                return _error(str(exc), 422)
            except SharedItemsError as exc:
                return _refused_items(exc)
            except OSError as exc:
                return self._unkept(exc)
            return JSONResponse({**out, "applied": True, "rules_version": self.rt.built.engine.rules_version})

        return await self._saved(imported)

    async def _apply(self, change: Callable[[Config], Config | None], etag: str | None = None) -> Response:
        """Switch to what ``change`` makes of the settings in use (``Runtime.update_config``): read and applied
        under one lock, so a save made at the same moment is not undone. ``change`` answers None to leave them; one that
        would remove items set for everyone on a server is refused (409), with how many, never which; on a server, one
        its store does not keep is answered as the administration answers it (``_unkept``). ``etag``: the one the page
        read the settings with (``GET /config``), compared under that lock before anything else: when the settings in
        use are others now (another tab saved, a change applied since), nothing is applied (409, ``settings_stale``).
        Made as a save is (``_saved``: one at a time, in the work threads on a server)."""
        stale: list[bool] = []

        def checked(current: Config) -> Config | None:
            from latet.runtime import digest

            if etag is not None and not _same_etag(etag, digest(current)):
                stale.append(True)
                return None
            return change(current)

        def applied() -> Response:
            try:
                self.rt.update_config(checked)
            except SettingsSavedChangePending:
                return _saved_change_pending()
            except SharedItemsError as exc:
                return _refused_items(exc)
            except OSError as exc:
                return self._unkept(exc)
            if stale:
                return _refused_save(STALE_SETTINGS, 409, "settings_stale")
            return JSONResponse({"ok": True, "rules_version": self.rt.built.engine.rules_version})

        return await self._saved(applied)

    async def _saved(self, job: Callable[[], Response]) -> Response:
        """A save of the settings, ``job``: one at a time for this dashboard (the others wait their turn on the event
        loop, holding no thread), made in the work threads on a server (``_in_work``), where what it reads and writes of
        the settings' store waits behind no space's build, and a flood of one person's saves holds one thread."""
        async with self._turn("save"):
            return await self._in_work(job)

    def _unkept(self, exc: OSError) -> Response:
        """On a server, a save of the settings their store did not keep, answered as the administration answers one,
        none of it running (``Runtime.apply_config``): 409 when another instance wrote them since (they are read again:
        make the change anew), 413 when they weigh more than a server keeps for one identity, 503 with
        ``Retry-After: 1`` when the store does not answer. ``reason`` says which, for the dashboard's words; the store's
        own words, which name the identity and the driver's error, are said in the log by the error's type alone. A
        computer's raises it again (an error of the dashboard, 500), as before."""
        if self.rt.gateway.on_server is None:
            raise exc
        if isinstance(exc, SettingsStale):
            return _refused_save(STALE_SETTINGS, 409, "settings_stale")
        if isinstance(exc, SettingsTooLarge):
            return _refused_save(TOO_LARGE_SETTINGS, 413, "settings_too_large")
        if count := _UNKEPT.due():
            log.warning(
                "the settings of %s could not be kept (%s)%s",
                self.rt.identity.id if self.rt.identity is not None else "an identity",
                described(exc),
                f" ({count} times since the line before)" if count > 1 else "",
            )
        unkept = _refused_save(UNKEPT_SETTINGS, 503, "settings_unavailable")
        unkept.headers["retry-after"] = "1"  # nothing was kept: the same save may be made again
        return unkept

    async def get_config(self, request: Request) -> Response:
        # Made and rendered in a worker thread: on a server the settings in use hold every shared term and recognizer
        # (tens of thousands), and every read of the Rules page would hold the event loop every identity shares.
        return await self._in_pool(self._config_answer)

    def _config_answer(self) -> Response:
        """In a worker thread: the settings in use, read once, as the Rules page shows them, with their etag (the one a
        save of the page sends back), both made from that one reading."""
        from latet.runtime import digest

        config = self.rt.config
        view = self._config_view(config)
        view["etag"] = digest(config)
        return JSONResponse(view)

    def _etag(self, data: dict[str, Any]) -> str | Response | None:
        """The etag a save carries (``etag``, as ``GET /config`` answered it): None when it carries none (or null), a
        refusal (400) when it is not a text. On a Latet server, where the dashboard and the server ship together, a save
        that carries none is refused as one from a page older than the settings in use (409, ``settings_stale``): a page
        opened before an upgrade reads them again rather than saving what it read over them. On a computer, as
        before."""
        etag = data.get("etag")
        if etag is not None and not isinstance(etag, str):
            return _error("etag: the text GET /config answered", 400)
        if etag is None and self.rt.gateway.on_server is not None:
            return _refused_save(UNNAMED_SETTINGS, 409, "settings_stale")
        return etag

    async def put_config(self, request: Request) -> Response:
        data = await self._json(request)
        if data is None:
            return _error("invalid body", 400)
        if isinstance(etag := self._etag(data), Response):
            return etag
        editable = ("profile", "pii", "id_regions", "phone_regions", "include_private_ips", "terms", "allow_values")
        editable += ("recognizers",)
        editable += ("allow_patterns", "policies", "history", "retention_days", "ner", "auto_mode_review")
        editable += ("data", "data_tools", "data_files", "data_hosts", "fields", "dump_guard", "images", "ocr")
        editable += ("llm_review", "live_review")
        editable += ("upstreams",)
        details: list[Any] = []  # why it cannot be saved: nothing is then applied
        refused: list[Response] = []  # what the tutorial's sandbox does not do: nothing is then applied either

        def merge(current: Config) -> Config | None:
            merged = current.model_dump(mode="json")
            for key in editable:
                if key in data:
                    merged[key] = data[key]
            if isinstance(data.get("identity"), dict):
                identity = {**merged.get("identity", {}), **data["identity"]}
                # The older single user name, folded into the list it stands for (the page shows both as one list, and
                # sends neither): dropped, its home folder's name would leave in clear.
                names, name = identity.get("usernames"), identity.get("username")
                if isinstance(names, list) and isinstance(name, str):
                    identity["usernames"] = names if name in names else [*names, name]
                    del identity["username"]
                merged["identity"] = identity
            try:
                config = Config.model_validate(merged)
            except ValidationError as exc:
                details.extend(exc.errors(include_url=False, include_context=False, include_input=False))
                return None
            shared = self._shared()
            if shared is not None:  # on a server: more rules than it keeps for one identity, refused before compiled
                check_rules(shared, config)
            if "recognizers" in data:  # the dashboard saves no broken recognizer (a file edited by hand may hold one)
                _, statuses = recognizers_for(config)
                details.extend(
                    {"loc": ["recognizers", i], "msg": f"{status.name}: {error}", "type": "value_error"}
                    for i, status in enumerate(statuses)
                    for error in status.errors
                )
            if "terms" in data:  # nor a new term it cannot mask (one written by hand stays as it is)
                details.extend(term_errors((t.value for t in config.terms), kept=[t.value for t in current.terms]))
            if shared is not None:
                # No file or variable of the server's to read the reviewer's key from: the server gives it. Each with a
                # reason the dashboard words in its reader's language.
                details.extend(
                    {
                        "loc": name.split("."),
                        "msg": REVIEWER_KEY,
                        "type": "value_error",
                        "reason": "reviewer_key_server",
                    }
                    for name in reviewer_key_named(config)
                )
                # An own destination reaches nothing only the server does: judged by where it leads, a name by what it
                # resolves to now, in the egress threads, its lookups counted for the identity and waited for a bounded
                # time (the server judges it again when it connects). Its address, the identity's own, is said back.
                space, me = self.rt.gateway.on_server, self.rt.identity
                resolve = egress.bounded(me.id if me is not None else None)
                server = space.url if space is not None else None
                for i, problem in own_destinations_refused(shared, config, resolve=resolve, server=server):
                    url = config.upstreams[i].url
                    details.append(
                        {"loc": ["upstreams", i], "msg": f"{url}: {problem}", "type": "value_error",
                         "reason": "destination_refused", "url": url}
                    )  # fmt: skip
            why = sandbox_refusal(current, config)
            if why is not None and (refusal := self._sandboxed(why)) is not None:
                refused.append(refusal)
            return None if details or refused else config

        answer = await self._apply(merge, etag)
        if details:
            return JSONResponse({"error": "invalid configuration", "details": details}, 400)
        return refused[0] if refused else answer

    async def allow(self, request: Request) -> Response:
        data = await self._json(request)
        value = (data or {}).get("value")
        if not isinstance(value, str) or not value.strip():
            return _error("value required", 400)

        def allowed(current: Config) -> Config:
            config = current.model_copy(deep=True)
            if value not in config.allow_values:
                config.allow_values.append(value)
            return config

        return await self._apply(allowed)

    async def terms(self, request: Request) -> Response:
        data = await self._json(request) or {}
        value, etype = data.get("value"), data.get("type") or "CUSTOM"
        if not isinstance(value, str) or not value.strip() or not isinstance(etype, str):
            return _error("value required", 400)
        if not has_letter_or_digit(value):
            return _error(TERM_WITHOUT_LETTERS, 400)

        def masked(current: Config) -> Config:
            config = current.model_copy(deep=True)
            if not any(t.value == value for t in config.terms):
                config.terms.append(TermConfig(value=value, type=etype.upper()))
            if value in config.allow_values:
                config.allow_values.remove(value)  # "always mask" wins over an earlier "not sensitive"
            return config

        return await self._apply(masked)

    def _playground_masker(self, profile: Profile | None = None) -> Masker:
        """Current rules over a throwaway in-memory vault: nothing typed here is kept. With ``profile``, the rules as
        that profile makes them, to try a text with it: the settings are copied, never changed."""
        from latet.runtime import resolve_identity

        version = self.rt.built.engine.rules_version
        cached = self._playground.get(profile)
        if cached is None or cached[0] != version or len(cached[1].vault.entities()) > 10_000:
            config = self.rt.config if profile is None else self.rt.config.model_copy(update={"profile": profile})
            identity = resolve_identity(config, detect=self.rt.identity is None)
            masker, _ = build_masker(config, Vault(os.urandom(32)), identity)
            cached = self._playground[profile] = (version, masker)
        return cached[1]

    async def playground(self, request: Request) -> Response:
        data = await self._json(request) or {}
        text = data.get("text")
        if not isinstance(text, str):
            return _error("text required", 400)
        if len(text) > _MAX_PLAYGROUND:
            return _error("text too long", 413)
        kind = _KINDS.get(str(data.get("kind") or "user_text"), SlotKind.USER_TEXT)
        try:
            profile = Profile(data["profile"]) if data.get("profile") is not None else None
        except (ValueError, TypeError):
            return _error("unknown profile: balanced, max or secrets", 400)

        def run() -> dict[str, Any]:
            masker = self._playground_masker(profile)
            result = masker.mask(text, kind)
            spans = replacement_dicts(result.replacements, masker.vault)
            for span, r in zip(spans, result.replacements, strict=True):
                span["start"], span["end"] = r.start, r.end
            return {"masked": result.text, "replacements": spans}

        return JSONResponse(await self._in_pool(run))

    # --- projects -------------------------------------------------------------------------------------

    def _repo_tag(self, project: Project) -> tuple[Project, str | None]:
        """The project as its file is now, and the tag of the bytes its settings were read from: what the editor shows
        and saves over, never a tag read apart from them. A file changed since it was worked out is read again once
        (the project worked out anew); one there that cannot be read has no tag (None). Off the event loop."""
        for attempt in range(2):
            if project.repo_file is None:
                return project, ""
            try:
                now = file_tag(project.repo_file)
            except ProjectFileUnreadable:
                return project, None
            if now == project.tag or attempt or self.rt.projects is None:
                return project, project.tag if now == project.tag else None
            self.rt.projects.touch(project.id)
            project = self._find_project(project.id) or project
        return project, None

    def _project_view(self, project: Project, detail: bool = False) -> dict[str, Any]:
        view = project.summary()
        if not detail:
            return view
        local = self.rt.gateway.on_server is None and Path(project.root).is_dir()  # a server looks at no folder
        tag: str | None = ""
        if local:
            project, tag = self._repo_tag(project)
            view = project.summary()
        c = project.config
        repo_path = project.repo_file or (Path(project.root) / PROJECT_FILE if local else None)
        secrets_only = c.profile == Profile.SECRETS
        every_region = c.profile == Profile.MAX
        return view | {
            "repo": {
                "path": str(repo_path) if repo_path else None,
                "exists": project.repo_file is not None,
                "settings": project.repo.model_dump(mode="json", exclude_defaults=True) if project.repo else {},
                "tag": tag or "",
                # Whether its latet.yaml was read whole (or is not there): one that cannot be read is never written.
                "readable": tag is not None,
                "writable": local and not in_container() and tag is not None,
            },
            "personal": project.personal.model_dump(mode="json", exclude={"path"}) if project.personal else None,
            "notices": [n.as_dict() for n in project.notices],
            "effective": {
                "profile": c.profile.value,
                "pii": [] if secrets_only else effective_pii(c.pii, c.id_regions, every_region=every_region),
                "id_regions": c.id_regions,
                "phone_regions": c.phone_regions,
                "include_private_ips": c.include_private_ips,
                "ner": c.ner,
                "terms": len(c.terms),
                "recognizers": len(c.recognizers),
                "allow_values": len(c.allow_values),
                "allow_patterns": len(c.allow_patterns),
                "policies": {k: str(v) for k, v in c.policies.items()},
                "data": c.data_mode(),
                "data_tools": c.data_tools,
                "data_files": c.data_files,
                "data_hosts": c.data_hosts,
                "fields": [f.model_dump(exclude_defaults=True) for f in c.fields],
                "dump_guard": c.dump_guard,
                "images": c.images,
                "llm_review": c.llm_review.mode(),
            },
            "recognizer_status": [s.as_dict() for s in project.statuses],
        }

    def _find_project(self, pid: str) -> Project | None:
        return self.rt.projects.get(pid) if self.rt.projects is not None else None

    async def projects_list(self, request: Request) -> Response:
        projects = self.rt.projects
        if projects is None:
            return JSONResponse({"items": []})
        items = await self._in_pool(lambda: [self._project_view(p) for p in projects.known()])
        return JSONResponse({"items": items})

    async def project(self, request: Request) -> Response:
        project = await self._in_pool(self._find_project, request.path_params["id"])
        if project is None:
            return _error("not found", 404)
        return JSONResponse(await self._in_pool(self._project_view, project, detail=True))

    async def add_project(self, request: Request) -> Response:
        """A project by its folder, before any session there (to give it settings first)."""
        path = (await self._json(request) or {}).get("path")
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            return _error("path required", 400)
        if self.rt.projects is None:
            return _error("not found", 404)
        project = await self._in_pool(self.rt.projects.resolve, path.strip())
        return JSONResponse(await self._in_pool(self._project_view, project, detail=True))

    async def put_personal(self, request: Request) -> Response:
        """Your own settings for a project, kept in config.yaml: they may also loosen it or turn it off. With the etag
        of ``GET /config`` (``etag``), refused (409) when the settings in use changed since the page read them."""
        project = await self._in_pool(self._find_project, request.path_params["id"])
        if project is None:
            return _error("not found", 404)
        data = await self._json(request)
        if data is None:
            return _error("invalid body", 400)
        if isinstance(etag := self._etag(data), Response):
            return etag
        data = {key: value for key, value in data.items() if key != "etag"}
        try:
            entry = ProjectEntry.model_validate({**data, "path": project.root})
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_context=False, include_input=False)
            return JSONResponse({"error": "invalid settings", "details": details}, 400)
        kept = [t.value for t in project.personal.terms] if project.personal is not None else []
        if unmaskable := term_errors((t.value for t in entry.terms), kept=kept):
            return JSONResponse({"error": "invalid settings", "details": unmaskable}, 400)

        def counted() -> None:
            """On a server, more rules of her own than it keeps for one identity: refused before any is compiled."""
            if (shared := self._shared()) is not None:
                check_rules(shared, with_personal(self.rt.config, project.root, entry))

        try:
            await self._in_pool(counted)
        except SharedItemsError as exc:
            return _refused_items(exc)
        except OSError as exc:
            return self._unkept(exc)
        if entry.recognizers:
            from latet.engine.detectors.recognizers import compile_all

            _, statuses = await self._in_pool(compile_all, entry.recognizers, reserved_types())
            details = [
                {"loc": ["recognizers", i], "msg": f"{status.name}: {error}", "type": "value_error"}
                for i, status in enumerate(statuses)
                for error in status.errors
            ]
            if details:
                return JSONResponse({"error": "invalid settings", "details": details}, 400)
        return await self._apply(lambda current: with_personal(current, project.root, entry), etag)

    async def delete_personal(self, request: Request) -> Response:
        project = await self._in_pool(self._find_project, request.path_params["id"])
        if project is None:
            return _error("not found", 404)
        return await self._apply(lambda current: with_personal(current, project.root, None))

    async def put_repo(self, request: Request) -> Response:
        """Write the project's latet.yaml: only settings that add protection, and never over a file
        changed since the dashboard read it."""
        no_files = "your projects' files: the server has none of them"
        if (refused := self._sandboxed("your projects' files") or self._on_server(no_files)) is not None:
            return refused
        project = await asyncio.to_thread(self._find_project, request.path_params["id"])
        if project is None:
            return _error("not found", 404)
        if in_container():
            return _error("the container does not write in your projects", 403)
        if not await asyncio.to_thread(Path(project.root).is_dir):
            return _error("the project folder is not on this machine", 400)
        data = await self._json(request) or {}
        settings, tag = data.get("settings"), data.get("tag", "")
        if not isinstance(settings, dict):
            return _error("settings required", 400)
        details = [
            {"loc": ["settings", k], "msg": "a repository file cannot set this", "type": "value_error"}
            for k in settings
            if k not in REPO_KEYS
        ]
        parsed, notices = repo_settings({k: v for k, v in settings.items() if k in REPO_KEYS})
        details += [
            {"loc": ["settings", n.key], "msg": n.message, "type": "value_error"} for n in notices if n.level == "error"
        ]
        kept = [t.value for t in project.repo.terms] if project.repo is not None else []
        details += term_errors((t.value for t in parsed.terms), ("settings", "terms"), kept)
        # What the save adds to the file that would be set aside there for leaving values in clear: never written, each
        # said by its place. What the file holds already stays, its notice said where it is set aside (the dashboard
        # sends the whole file: a teammate's entry, set aside only where your settings let its type through, is theirs).
        config = self.rt.config
        _, laid = await self._in_pool(effective_config, config, parsed, personal_for(config.projects, project.root))
        details += [
            {"loc": ["settings", *n.key.split(".", 1)], "msg": n.message, "type": "value_error", "reason": n.reason}
            for n in laid
            if n.reason == IN_CLEAR_REASON and _added_by(n.key, parsed, project.repo)
        ]
        if details:
            return JSONResponse({"error": "invalid settings", "details": details}, 400)
        path = project.repo_file or Path(project.root) / PROJECT_FILE
        text = render_project_file(parsed)

        def save() -> Response | None:
            """The file read and compared with the page's tag, then written, in one step that no other save of this
            process comes between, off the event loop. A file there that cannot be read is never written over, whatever
            the page sent ("" included); "" matches only a file that is not there. Another program may still write
            between the read and the write: the editor's tag is a check, not a lock of the file."""
            with self._repo_writes:
                try:
                    current = file_tag(path)
                except ProjectFileUnreadable as exc:
                    return _repo_file_unread(exc)
                if current != tag:
                    return _error("the file changed since it was read: open the project again", 409)
                write_project_file(path, text)
            return None

        if (refused := await asyncio.to_thread(save)) is not None:
            return refused
        assert self.rt.projects is not None
        self.rt.projects.touch(project.id)

        def shown() -> dict[str, Any]:
            return self._project_view(self._find_project(project.id) or project, detail=True)

        return JSONResponse(await asyncio.to_thread(shown))

    async def suggest_project(self, request: Request) -> Response:
        """Draft recognizers from the project's own files (`latet rules suggest`): never a value."""
        no_files = "your projects' files: the server has none of them"
        if (refused := self._sandboxed("your projects' files") or self._on_server(no_files)) is not None:
            return refused
        project = await asyncio.to_thread(self._find_project, request.path_params["id"])
        if project is None:
            return _error("not found", 404)
        root = Path(project.root)
        if not await asyncio.to_thread(root.is_dir):
            return _error("the project folder is not on this machine", 400)

        def run() -> dict[str, Any]:
            from latet.engine.detectors.base import DetectContext
            from latet.suggest import suggest

            masker = project.scope.masker if project.scope is not None else self.rt.built.masker
            context = DetectContext(SlotKind.TOOL_RESULT)
            result = suggest(root, covered=lambda text: masker.detect(text, context), draft_limit=20)
            return {"files": result["files"], "shapes": result["shapes"][:20], "drafts": result.get("drafts") or []}

        return JSONResponse(await asyncio.to_thread(run))

    # --- stats and integrations ---------------------------------------------------------------------

    async def test_recognizer(self, request: Request) -> Response:
        """The recognizer editor's Try it: what `latet rules test` reports, and what it finds in a
        text typed there (nothing is kept)."""
        data = await self._json(request) or {}
        entry, text = data.get("recognizer"), data.get("text")
        if not isinstance(entry, dict):
            return _error("recognizer required", 400)
        if text is not None and (not isinstance(text, str) or len(text) > _MAX_PLAYGROUND):
            return _error("text too long", 413)

        def run() -> dict[str, Any]:
            from latet.engine.build import reserved_types
            from latet.engine.detectors.recognizers import compile_one
            from latet.rules_report import report

            [item] = report([entry])
            if text:
                recognizer, _ = compile_one(entry, reserved_types(), None, include_off=True)
                if recognizer is not None:
                    found, reason = recognizer.explain(text)
                    spans = [{"start": s.start, "end": s.end} for s in recognizer.find(text)]
                    item["try"] = {"found": found, "reason": reason, "spans": spans}
            return item

        return JSONResponse(await self._in_pool(run))

    def _review_settings(self, data: dict[str, Any], shared: Config | None) -> LlmReview:
        """The reviewer's settings to try: the ones given (not saved yet), or the saved ones; with a
        project's guidance after the global one, from a project's page. ``shared``: the settings the server shares
        (``_shared``), None on a computer."""
        given = data.get("llm_review")
        settings = (
            self.rt.config.llm_review
            if given is None
            else LlmReview.model_validate({**self.rt.config.llm_review.model_dump(mode="json"), **given})
        )
        if data.get("project_guidance") is not None:
            here = ProjectSettings.model_validate({"review_guidance": data["project_guidance"]}).review_guidance
            settings = settings.model_copy(update={"guidance": join_guidance(settings.guidance, here)})
        if self.rt.identity is not None:
            return pinned_reviewer((shared or self.rt.config).llm_review, settings)
        if shared is not None:
            return pinned_reviewer(shared.llm_review, settings)  # the server's reviewer, the identity's use of it
        return settings

    async def reviewer_prompt(self, request: Request) -> Response:
        """The local reviewer's own instructions, the very text the daemon sends it: shown read-only.
        Your guidance comes after them (``intro``, then the text), from the settings."""
        from latet.engine.reviewer.prompt import GUIDANCE_INTRO, PROMPT_VERSION, SYSTEM

        return JSONResponse(
            {"system": SYSTEM, "intro": GUIDANCE_INTRO, "version": PROMPT_VERSION, "max_guidance": MAX_GUIDANCE}
        )

    async def test_reviewer(self, request: Request) -> Response:
        return await self._reviewer_call(request, self._test_reviewer)

    async def _test_reviewer(self, request: Request) -> Response:
        """The reviewer's *Try it*: does its server answer, which model, how fast, and what it finds
        in a sample text (or one typed there) beyond the detectors. Nothing is kept."""
        from latet.engine.reviewer.probe import SAMPLE, probe, reviewed_view
        from latet.engine.reviewer.reviewer import client_for

        if (refused := self._sandboxed("the local reviewer")) is not None:
            return refused
        # On a server, once a shared change is published, the shared settings are a round trip to PostgreSQL and the
        # opening of up to 4 MiB before a space's thread has read them again: read once, in a worker thread.
        shared = await self._in_pool(self._shared)
        if shared is not None and not shared.llm_review.enabled:
            # Its server would be the default one, on the server's own port: nothing of what answers there is shown.
            return _error("the local reviewer is off: its administrator sets it up for everyone on this server", 409)
        data = await self._json(request) or {}
        text = data.get("text") or SAMPLE
        if not isinstance(text, str) or len(text) > _MAX_PLAYGROUND:
            return _error("text too long", 413)
        try:
            settings = await self._in_pool(self._review_settings, data, shared)
        except ValidationError as exc:
            details = exc.errors(include_url=False, include_context=False, include_input=False)
            return JSONResponse({"error": "invalid settings", "details": details}, 400)
        view = await self._in_pool(reviewed_view, self.rt.built.masker, text)
        client = await self._in_pool(client_for, settings, 120, key_of=self.rt.reviewer_key)
        result = await probe(settings, view, text, client=client)  # probe closes it
        answer = result.answer
        return JSONResponse(
            {
                "ok": result.ok,
                "error": result.error,
                "models": result.models,
                "model": result.model,
                "slots": result.slots,
                "n_ctx": result.n_ctx,
                "vision": result.vision,
                "seconds": result.seconds,
                "text": text,
                "view": view.text,
                "findings": [
                    {"text": f.value, "category": f.category, "masked": f.category in settings.auto_mask}
                    for f in (answer.findings if answer else [])
                ],
                "block": bool(answer and answer.block),
            }
        )

    async def reviewer_key(self, request: Request) -> Response:
        """The reviewer server's API key: where it comes from (GET), saved (PUT) or forgotten (DELETE).
        The key is never sent back."""
        from latet.config import data_dir
        from latet.engine.reviewer.reviewer import key_source
        from latet.storage.keys import remove_reviewer_key, save_reviewer_key

        mine = "the local reviewer's key, which its administrator sets for everyone"
        refused = self._sandboxed("the local reviewer's key") or self._on_server(mine)
        if request.method != "GET" and refused is not None:
            return refused
        if self.rt.gateway.on_server is not None:  # set by the administrator: nothing of how the server holds it
            return JSONResponse({"source": "server", "variable": None, "stored": None})
        stored = None
        if request.method == "PUT":
            data = await self._json(request) or {}
            key = data.get("key")
            if (
                not isinstance(key, str)
                or len(key) > KEY_LONGEST
                or any(c in key for c in "\r\n")
                or key_problem(key.strip()) is not None
            ):
                return _error(KEY_REQUIREMENT, 400)
            stored = await asyncio.to_thread(save_reviewer_key, data_dir(), key.strip())
        elif request.method == "DELETE":
            await asyncio.to_thread(remove_reviewer_key, data_dir())
        if request.method != "GET":
            self.rt.use_reviewer(self.rt.config)  # the reviewer asks with the new key from now on
        settings = self.rt.config.llm_review
        source = await asyncio.to_thread(key_source, settings)
        return JSONResponse({"source": source, "variable": settings.api_key_env, "stored": stored})

    async def reviewer_certificate(self, request: Request) -> Response:
        """The certificate of the reviewer's server at ``url``: its fingerprint, to compare (POST); kept
        as trusted when it is still the one you compared (PUT ``{url, fingerprint}``). Saving the
        settings with ``llm_review.certificate`` set to it trusts it."""
        from latet.config import data_dir
        from latet.engine.reviewer import certificate as certificates

        theirs = "the local reviewer's certificate, which its administrator sets for everyone"
        if (refused := self._sandboxed("the local reviewer's server") or self._on_server(theirs)) is not None:
            return refused
        data = await self._json(request) or {}
        url = data.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            return _error("an https:// address is required", 400)
        try:
            pem, found = await asyncio.to_thread(certificates.fetch, url)
        except (OSError, ValueError) as exc:
            return _error(f"the server could not be reached: {exc}", 502)
        if request.method == "PUT":
            if data.get("fingerprint") != found:
                return _error("the server shows another certificate now: compare it again", 409)
            await asyncio.to_thread(certificates.save, data_dir(), pem)
        return JSONResponse({"fingerprint": found, "readable": certificates.readable(found)})

    async def draft_recognizer(self, request: Request) -> Response:
        return await self._reviewer_call(request, self._draft_recognizer)

    async def _draft_recognizer(self, request: Request) -> Response:
        """A recognizer drafted by the reviewer's model from a description, with what `latet rules
        test` says of it: the editor shows it only when it passes."""
        from latet.engine.reviewer.client import ReviewError
        from latet.engine.reviewer.draft import draft_recognizer
        from latet.engine.reviewer.reviewer import client_for
        from latet.rules_report import report

        if (refused := self._sandboxed("the local reviewer")) is not None:
            return refused
        data = await self._json(request) or {}
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            return _error("description required", 400)
        settings = self.rt.config.llm_review
        if not settings.enabled:
            return _error("the local reviewer is off: turn it on in Rules first", 409)
        client = await self._in_pool(client_for, settings, 120, key_of=self.rt.reviewer_key)
        try:
            draft = await draft_recognizer(client, description)
        except ReviewError as exc:
            return _error(str(exc), 502)
        finally:
            await client.aclose()
        [checked] = await self._in_pool(report, [draft])
        return JSONResponse({"draft": draft, "report": checked})

    async def _reviewer_call(self, request: Request, call: Callable[[Request], Awaitable[Response]]) -> Response:
        """A server dashboard's three reviewer calls share one turn, taken without waiting."""
        if (refused := self._sandboxed("the local reviewer")) is not None:
            return refused
        if self.rt.identity is None:
            return await call(request)
        turn = self._turn("reviewer")
        if turn.locked():
            from latet.server.logs import REFUSED

            request.scope[REFUSED] = True
            return JSONResponse(
                {"error": "the local reviewer is answering another of your requests"},
                429,
                headers={"Retry-After": "5"},
            )
        await turn.acquire()  # an unlocked asyncio lock is acquired without yielding
        try:
            return await call(request)
        finally:
            turn.release()

    async def stats(self, request: Request) -> Response:
        """The history's numbers over ``days`` (90 at most): read, aggregated and rendered in a worker thread, one call
        at a time for a dashboard (each holds a thread about three seconds at most: ``STATS_MOST``), never on the event
        loop every request shares."""
        try:
            days = max(1, min(90, int(request.query_params.get("days", "7"))))
        except ValueError:
            return _error("invalid days", 400)
        since = time.time() - days * 86400
        async with self._turn("stats"):
            return await self._in_pool(self._stats, since)

    def _stats(self, since: float) -> Response:
        """In a worker thread: the stats of the exchanges since ``since``, the latest ``STATS_MOST`` at most, read a
        part at a time (``HistoryStore.summaries_parts``), each part aggregated before the next is read."""
        store = self.rt.exchanges
        if store is None:
            return JSONResponse(_stats_of([]))
        return JSONResponse(_stats_of(store.summaries_parts(since, STATS_MOST), STATS_MOST))

    def _turn(self, what: str) -> asyncio.Lock:
        """What makes one call of ``what`` at a time for this dashboard, on the event loop that runs it: the others wait
        for it there, holding no thread."""
        loop = asyncio.get_running_loop()
        found = self._turns.get(what)
        if found is None or found[0] is not loop:
            found = self._turns[what] = (loop, asyncio.Lock())
        return found[1]

    def _integrations(self) -> dict[str, Any]:
        from latet.doctor.checks import OK, WARN, Check, check_engine, check_port
        from latet.integrations.agents import ALL, WRAPPED, ClaudeCode, Codex, GeminiCli
        from latet.service import autostart

        port = self.rt.gateway.port
        checks = [Check(OK, "Daemon", f"running on 127.0.0.1:{port}, masking")]
        for group in (check_port, check_engine):
            try:
                checks += group()
            except Exception as exc:
                checks.append(Check(WARN, group.__name__.removeprefix("check_").title(), repr(exc)))
        installed = autostart.installed()
        detail = "installed" if installed else "not installed: `latet service install`"
        checks.append(Check(OK if installed else WARN, "Start at login", detail))
        now = time.time()
        if self._agents is None or now - self._agents[0] > 60:
            agents = []
            for agent in [*ALL.values(), *WRAPPED.values()]:
                info = agent.detect()  # runs `<agent> --version`: cached for a minute
                entry: dict[str, Any] = {
                    "id": agent.id,
                    "name": info.name,
                    "installed": info.installed,
                    "version": info.version,
                    "status": agent.status() if info.installed else "not installed",
                    "notes": info.notes,
                    "route": None,
                }
                if info.installed and isinstance(agent, GeminiCli):  # how it reaches its model, from its settings
                    gemini = agent.route(self.rt.config)
                    entry["route"] = {
                        "summary": gemini.summary or None,
                        "problem": gemini.problem,
                        "notes": gemini.notes,
                    }
                if info.installed and isinstance(agent, (ClaudeCode, Codex)):
                    try:  # where it goes: Anthropic, a company's gateway, Bedrock... or why Latet cannot go in front
                        if isinstance(agent, ClaudeCode):
                            claude = agent.route(self.rt.config)
                            notes = [claude.note] if claude.note else []
                            entry["route"] = {"summary": claude.summary, "problem": claude.problem, "notes": notes}
                        else:
                            codex = agent.route(self.rt.config)
                            entry["route"] = {"summary": codex.summary, "problem": codex.problem, "notes": codex.notes}
                    except Exception as exc:  # a settings file that cannot be read: said, not a 500
                        entry["route"] = {"summary": None, "problem": repr(exc), "notes": []}
                agents.append(entry)
            self._agents = (now, agents)
        # Destinations: where they go, which agent's settings named them, when last used, whether an
        # agent still uses them (an unused one may be removed).
        in_use = self._in_use()
        used = self.rt.exchanges.last_used() if self.rt.exchanges is not None else {}
        destinations = [
            {
                "name": d.name,
                "url": d.url,
                "agent": d.agent,
                "last_used": used.get(d.name),
                "in_use": d.name in in_use,
            }
            for d in self.rt.config.upstreams
        ]
        return {"checks": [vars(c) for c in checks], "agents": self._agents[1], "destinations": destinations}

    def _server_integrations(self) -> dict[str, Any]:
        """An identity's space on a Latet server: no program of the server is run or read; its destinations."""
        shared = self._shared()
        names = {(d.name, d.url) for d in shared.upstreams} if shared is not None else set()
        used = self.rt.exchanges.last_used() if self.rt.exchanges is not None else {}
        on = self.rt.gateway.on_server
        who = self.rt.identity.name if self.rt.identity is not None else "you"
        check = {"status": "ok", "title": "Server", "detail": f"masking for {who} at {on.url if on else ''}"}
        destinations = [
            {"name": d.name, "url": d.url, "agent": d.agent, "last_used": used.get(d.name),
             "in_use": d.name in self.rt.wrapped, "shared": (d.name, d.url) in names}
            for d in self.rt.config.upstreams
        ]  # fmt: skip
        return {"checks": [check], "agents": [], "destinations": destinations}

    def _in_use(self) -> set[str]:
        """The destinations an agent still uses: its settings point at them (Claude Code's, your organization's
        managed settings, Codex's files), or a wrapper registered them since the daemon started
        (``latet claude`` and ``latet codex`` keep no file of their own once their session ends). On a server, the
        wrappers' alone: nothing of the server machine's is read."""
        from latet.integrations import routes
        from latet.integrations.agents import ClaudeCode, Codex

        if self.rt.gateway.on_server is not None:
            return set(self.rt.wrapped)
        texts = [
            _read(ClaudeCode().settings_path()),
            *(_read(path) for path in routes.MANAGED_SETTINGS),
            *routes.codex_texts(Codex().config_path().parent).values(),
        ]
        return routes.destinations_pointed_at(texts, self.rt.gateway.port) | set(self.rt.wrapped)

    async def integrations(self, request: Request) -> Response:
        if self.tutorial is not None:  # the sandbox's own view: no agent of this computer is run or read
            return JSONResponse(self.tutorial.integrations(self.rt.gateway.port))
        if self.rt.gateway.on_server is not None:  # nor of the server's machine
            return JSONResponse(await self._in_pool(self._server_integrations))
        return JSONResponse(await asyncio.to_thread(self._integrations))

    async def remove_destination(self, request: Request) -> Response:
        """One destination removed, and that one alone, from the settings as they are when it goes: a destination
        an agent's wrapper added since the page was loaded stays. 404 when there is none of that name; 409 while an
        agent still uses it (``_in_use``), weighed under the same lock as the removal, so a wrapper registering it
        at the same moment either comes first, and it stays, or after, and adds it again."""
        name = request.path_params["name"]
        refused: list[Response] = []  # why nothing is removed

        def removal(current: Config) -> Config | None:
            shared = self._shared()
            if shared is not None and any(d.name == name for d in shared.upstreams):
                refused.append(_error(f"{name} is set for everyone on this server: its administrator removes it", 409))
            elif all(d.name != name for d in current.upstreams):
                refused.append(_error(f"there is no destination named {name}", 404))
            elif name in self._in_use():
                said = f"{name} is still in use: an agent's settings, your organization's managed settings, "
                refused.append(_error(said + "or a latet claude or latet codex session point at it", 409))
            else:
                return current.model_copy(update={"upstreams": [d for d in current.upstreams if d.name != name]})
            return None

        answer = await self._apply(removal)
        return refused[0] if refused else answer

    async def stream(self, request: Request) -> Response:
        recorder = self.rt.recorder
        if recorder is None:
            return _error("recording is off", 404)
        broadcaster = recorder.broadcaster
        queue = broadcaster.subscribe()
        return _sse(queue, lambda: broadcaster.unsubscribe(queue))

    # --- the Live Reviewer ------------------------------------------------------------------------

    async def approvals(self, request: Request) -> Response:
        queue = self.rt.mediator.approvals
        return JSONResponse({"batches": await queue.pending(), "viewers": queue.viewers})

    async def approvals_stream(self, request: Request) -> Response:
        """The queue, live: a ``snapshot`` first, then ``pending``, ``settled`` and ``viewers`` events. It
        works with the history off; each open stream counts as a dashboard watching."""
        approvals = self.rt.mediator.approvals
        queue = await approvals.watch()
        return _sse(queue, lambda: approvals.unwatch(queue))

    async def decide(self, request: Request) -> Response:
        """``{action: "approve" | "reject", masks?: [{item, start, end, category}], images?: [{item,
        rects?: [[x0, y0, x1, y1]], withhold?}]}`` for one batch."""
        data = await self._json(request) or {}
        action, masks, images = data.get("action"), data.get("masks") or [], data.get("images") or []
        if action not in ("approve", "reject") or not isinstance(masks, list) or not isinstance(images, list):
            return _error("action is approve or reject; masks and images lists", 400)
        try:
            approvals = self.rt.mediator.approvals
            found = await approvals.decide(request.path_params["id"], action == "approve", masks, images)
        except MaskError as exc:
            return _error(str(exc), 422)
        if not found:
            return _error("it is not waiting any more", 409)
        return JSONResponse({"ok": True})

    async def approval_image(self, request: Request) -> Response:
        """An image waiting in the Live Reviewer: ``?view=original`` as it came (the default), ``sent`` as
        it would leave now. Never written to disk, like everything of a batch. Decoded in a worker thread: a batch's
        images weigh 16 MiB, which would hold the event loop every request shares."""
        try:
            item = int(request.path_params["item"])
        except ValueError:
            return _error("not found", 404)
        sent = request.query_params.get("view") == "sent"
        found = await self.rt.mediator.approvals.image(request.path_params["id"], item, sent)
        if found is None:
            return _error("not found", 404)
        data, media = found
        headers = {"cache-control": "no-store", "x-content-type-options": "nosniff"}
        return Response(
            await self._in_pool(base64.b64decode, data),
            media_type=media if media.startswith("image/") else "application/octet-stream",
            headers=headers,
        )

    async def approve_all(self, request: Request) -> Response:
        """``{project?}``: every batch waiting, or only those of one project (null: of no project)."""
        data = await self._json(request) or {}
        project = data.get("project")
        if "project" in data and project is not None and not isinstance(project, str):
            return _error("project is a project id or null", 400)
        queue = self.rt.mediator.approvals
        return JSONResponse({"approved": await queue.approve_all(project, every="project" not in data)})
