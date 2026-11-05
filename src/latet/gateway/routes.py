"""Upstream route table: which local path prefix forwards to which provider."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from latet.config import Destination


def wire_path(scope: Mapping[str, Any]) -> str:
    """The request's path as the client sent it, percent-escapes kept: the server decodes `path`,
    and a decoded `%2F` (in a Bedrock model ARN) would change the path the provider reads."""
    raw = scope.get("raw_path")
    if raw:
        return raw.decode("latin-1").split("?", 1)[0]
    return quote(scope["path"], safe="/:@!$&'()*+,;=~")


def _unreserved(match: re.Match[str]) -> str:
    char = chr(int(match.group(1), 16))
    return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group(0)


def readable_path(path: str) -> str:
    """A path as any server reads it: the escapes of letters, digits and `-._~` decoded (`%69` is `i`),
    the others kept (an ARN's `%2F` stays). What Latet reads a request by; it is sent as written."""
    return re.sub(r"%([0-9A-Fa-f]{2})", _unreserved, path)


@dataclass(frozen=True, slots=True)
class Resolved:
    """Where one request goes, and what Latet knows of it from its route."""

    url: str
    host: str  # for the record: where it went
    shape: str | None = None  # a cloud's model call (`bedrock`, `bedrock-count`, `vertex`): checked, never rewritten
    region: str | None = None
    signing: Any = None  # who signs it for Amazon when Latet does (an AWS profile, or a session's credentials)
    path: str | None = None  # the path to record, when the one sent holds a key no record may keep


@dataclass(frozen=True, slots=True)
class Upstream:
    """One upstream provider reachable under a local path prefix."""

    websocket: ClassVar[bool] = True  # Codex's Responses API may come over a WebSocket
    # A route Latet builds for a client of its own (the tutorial's pretend model): that client's correlation headers,
    # each by the closed grammar its source draws it from. A class declaration of such a fixture route, never a field,
    # so no Destination, setting or request can name one; every other route has none.
    fixture_ids: ClassVar[Mapping[str, re.Pattern[str]]] = MappingProxyType({})

    name: str
    prefix: str
    base_url: str
    keep_prefix: bool = False
    # On a Latet server, a destination of the identity's own: its address is checked when the server connects to it
    # (the route a request took says so, whatever the settings say by then).
    own: bool = False
    # Google's route: its Live API's WebSocket, whose frames Latet does not read, is refused at its handshake, never
    # relayed as they came (``ws.py``). A body Latet does not read (Google's Interactions API, its Batch API, file
    # uploads) is refused on every route, this one or another, on a computer as on a server (P2-2).
    refuses_unread: bool = False
    # Exact configured resource values. No first-seen trust; P2-3 supplies provider provenance separately.
    envelope_expected: frozenset[tuple[str, str]] = field(default=frozenset(), repr=False)
    envelope_resource_sources: tuple[tuple[str, str, str], ...] = field(default=(), repr=False)
    request_profile: Literal["standard", "azure-openai-1", "foundry-1"] = "standard"
    # Your approved request exceptions for this destination (``config.RequestException``), of this profile revision:
    # consulted only for a header or query parameter no profile names; their lifetime and project checked as decided.
    envelope_exceptions: tuple[Any, ...] = field(default=(), repr=False)
    # Their body selectors, built once per profile (``request_policy.ProfileGrants``): what an undeclared body member
    # may be admitted by (``body_selectors``), chosen as the request's project is known.
    envelope_body_grants: tuple[tuple[str, Any], ...] = field(default=(), repr=False, compare=False)
    # Internal construction authority, never populated from Destination/configuration or request markers.
    provider_origin: str | None = field(default=None, repr=False)

    def target(self, path: str, query: str) -> str:
        """Build the upstream URL for a local request path (which starts with ``prefix``)."""
        rest = path if self.keep_prefix else path[len(self.prefix) :]
        if rest and not rest.startswith("/"):
            rest = "/" + rest
        url = self.base_url.rstrip("/") + rest
        return f"{url}?{query}" if query else url

    def resolve(self, method: str, path: str, query: str) -> Resolved | None:
        """Where a request to ``path`` (as sent) goes; None when it has no route here."""
        url = self.target(path, query)
        return Resolved(url, urlsplit(url).netloc)

    def ws_target(self, path: str, query: str) -> str:
        url = self.target(path, query)
        parts = urlsplit(url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return parts._replace(scheme=scheme).geturl()


DEFAULT_UPSTREAMS: tuple[Upstream, ...] = (
    Upstream("anthropic", "/anthropic", "https://api.anthropic.com", provider_origin="anthropic"),
    Upstream("openai", "/openai", "https://api.openai.com", provider_origin="openai"),
    # Codex signed in with ChatGPT: base URL must keep the /backend-api/codex suffix.
    Upstream("chatgpt", "/backend-api/codex", "https://chatgpt.com", keep_prefix=True, provider_origin="chatgpt"),
    Upstream("openrouter", "/openrouter", "https://openrouter.ai"),
    Upstream(
        "gemini", "/gemini", "https://generativelanguage.googleapis.com", refuses_unread=True, provider_origin="gemini"
    ),
)


def destination_upstream(destination: Destination, *, own: bool = False) -> Upstream:
    """A destination of your settings as a route: `/<name>/...` goes to its address. ``own``: on a Latet server, a
    destination of the identity's own (``Upstream.own``)."""
    return Upstream(
        destination.name, "/" + destination.name, destination.url, own=own, request_profile=destination.request_profile
    )


class RouteTable:
    """Longest-prefix match over the configured upstreams."""

    def __init__(
        self,
        upstreams: Iterable[Upstream] = DEFAULT_UPSTREAMS,
        resources: tuple[tuple[str, str, str, str], ...] = (),
        exceptions: tuple[Any, ...] = (),
    ) -> None:
        self.use(upstreams, resources, exceptions)

    def use(
        self,
        upstreams: Iterable[Upstream],
        resources: tuple[tuple[str, str, str, str], ...] = (),
        exceptions: tuple[Any, ...] = (),
    ) -> None:
        """Route over these upstreams from now on: one swap, so a request being routed keeps the table it
        started with (the destinations change while the daemon runs). ``exceptions``: your approved request
        exceptions, each bound to the destination it names."""
        bound = []
        for upstream in upstreams:
            named = tuple(row for row in exceptions if row.destination == upstream.name)
            if named:
                from latet.gateway.request_policy import body_grants

                upstream = replace(upstream, envelope_exceptions=named, envelope_body_grants=body_grants(named))
            rows = tuple((url, role, value) for name, url, role, value in resources if name == upstream.name)
            if rows:
                upstream = replace(
                    upstream,
                    envelope_expected=upstream.envelope_expected
                    | frozenset((role, value) for url, role, value in rows if url == upstream.base_url),
                    envelope_resource_sources=rows,
                )
            bound.append(upstream)
        self._upstreams = tuple(sorted(bound, key=lambda u: len(u.prefix), reverse=True))

    def match(self, path: str) -> Upstream | None:
        for upstream in self._upstreams:
            if path == upstream.prefix or path.startswith(upstream.prefix + "/"):
                return upstream
        return None

    @property
    def upstreams(self) -> list[Upstream]:
        return list(self._upstreams)
