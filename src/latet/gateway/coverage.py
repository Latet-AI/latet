"""What a request is, as it came and as it leaves, for the decision that sends it or not (the request-coverage plan's
Phase 1 declarations, ``notes/server-request-coverage-plan.md``): the ``Envelope`` the forwarder and the WebSocket relay
build of each request, handshake and frame, before Latet changes anything of it; the ``FinalEnvelope`` its transport
freezes of what it builds (P1-3); the bounds a coverage walk will keep (``CoverageLimits``), and the shape of the proof
a ``Forward`` needs (``CoverageProof``).

P2-1 adds the bounded raw HTTP ``Inventory`` and ``EnvelopeProof`` alongside the legacy body decision. The original
``CoverageProof`` remains the full-coverage declaration for later phases. This module reads no environment/settings;
an envelope describes bytes and grants nothing until its profile and final checked values authorize them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote

Transport = Literal["http", "ws-handshake", "ws-frame"]
TRANSPORTS: frozenset[str] = frozenset({"http", "ws-handshake", "ws-frame"})


@dataclass(frozen=True, slots=True)
class CoverageLimits:
    """The bounds Phase 2's coverage walk keeps, each tested at its edge and one past it there (the plan's "Finite
    budgets"): past any of them a request is refused (422), never proved in part. Declared in Phase 1, checked by no
    code in P2-1 for HTTP envelope inventory; body-walk limits belong to P2-2."""

    raw_target: int = 16 * 1024  # bytes of the raw path and query
    query_occurrences: int = 256
    header_occurrences: int = 256  # final generated headers included
    header_bytes: int = 64 * 1024
    header_name: int = 64  # printable characters of a header's name in a refusal
    depth: int = 64  # of a JSON document
    occurrences: int = 100_000  # JSON members and values, keys and removed items counted
    ledger_bytes: int = 16 << 20  # the walk's own accounting, no value string copied
    # Body bytes the shadow projections of one request read at most, all of them together: each materializes and reads
    # the whole body, its bytes given back to the ledger once read (P2-8 review M-2).
    shadow_bytes: int = 16 << 20


LIMITS = CoverageLimits()
# Characters of a word (between dashes) of an unknown header's or query parameter's name that a refusal tells as they
# are; a longer word is told by its length (``EnvelopeRefused._named``). The open list's bound on a word of a new
# anthropic-* name is the same (``request_profiles._OPEN_WORD``): a word it refuses for its length is one the refusal
# never shows.
NAMED_WORD = 16


@dataclass(frozen=True, slots=True, eq=False)
class Envelope:
    """One request, handshake or frame as the client sent it, before Latet changed anything of it: its method, its raw
    path and query (percent-escapes kept), its headers in order with their duplicates, its body or frame (shared with
    the request, never copied: bytes and texts do not change), where it goes (the route's destination), and, on a Latet
    server, the identity whose request it is (the gate's). ``connection``: the WebSocket a handshake opened and its
    frames came on (a number of this process's, never reused). Its printed form names none of what the request carried:
    no path, query, header (credentials among them) or body."""

    transport: Transport
    method: str
    raw_path: bytes = field(repr=False)
    query: bytes = field(repr=False)
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes | str = field(repr=False)
    destination: str
    identity: str | None = None
    connection: int | None = None

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise ValueError("not a transport Latet knows")
        if not isinstance(self.raw_path, bytes) or not isinstance(self.query, bytes):
            raise TypeError("an envelope's path and query are bytes, as the client sent them")
        if type(self.headers) is not tuple:
            raise TypeError("an envelope's headers are a tuple, in order")
        if self.transport == "ws-frame" and not isinstance(self.body, bytes | str):
            raise TypeError("a frame is text or bytes")
        if self.transport != "ws-frame" and not isinstance(self.body, bytes):
            raise TypeError("a request's body is bytes")


def envelope_of(
    scope: Mapping[str, Any],
    transport: Transport,
    destination: str,
    body: bytes | str = b"",
    connection: int | None = None,
) -> Envelope:
    """The envelope of the request (or WebSocket) whose ASGI ``scope`` this is, its ``body`` read: the raw path as the
    server received it (its decoded path written back only when the server keeps none), its query, its headers in order;
    a WebSocket's method is the handshake's GET. The identity is the one a Latet server's gate put in the scope, never
    one the request names."""
    raw = scope.get("raw_path")
    raw_path = raw.split(b"?", 1)[0] if raw else quote(scope["path"], safe="/:@!$&'()*+,;=~").encode("latin-1")
    identity = scope.get("latet.identity")
    return Envelope(
        transport,
        "GET" if scope.get("type") == "websocket" else str(scope["method"]),
        raw_path,
        bytes(scope.get("query_string") or b""),
        tuple((bytes(name), bytes(value)) for name, value in scope.get("headers") or ()),
        body,
        destination,
        identity if isinstance(identity, str) else None,
        connection,
    )


def frame_envelope(handshake: Envelope, frame: bytes | str) -> Envelope:
    """The envelope of one frame a client sent on the WebSocket ``handshake`` opened: the handshake's route, identity
    and connection, the frame as it came."""
    return Envelope(
        "ws-frame",
        handshake.method,
        handshake.raw_path,
        handshake.query,
        handshake.headers,
        frame,
        handshake.destination,
        handshake.identity,
        handshake.connection,
    )


@dataclass(frozen=True, slots=True, eq=False)
class FinalEnvelope:
    """The request as its transport built it, frozen before it is sent (P1-3's guard): the method, the raw URL, the
    headers in order (credentials included, never logged), the content, and the name TLS is asked for when it is not
    the URL's (``tls_name``: a destination of an identity's own, reached at the address checked), compared with what is
    sent. Its printed form names the method alone."""

    method: str
    url: bytes = field(repr=False)
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    content: bytes = field(repr=False)
    tls_name: str | None = field(default=None, repr=False)


def occurrences(envelope: Envelope) -> int:
    """How many parts the request ``envelope`` is has, each of which a proof accounts for: its method, its path, each
    part of its query, each header occurrence, and its body or frame when it has one. P2-1's inventory counts them
    finer (path segments, JSON members); a proof is over this count until then."""
    query = sum(1 for part in envelope.query.split(b"&") if part) if envelope.query else 0
    return 2 + query + len(envelope.headers) + (1 if envelope.body else 0)


@dataclass(frozen=True, slots=True)
class CoverageProof:
    """What a ``Forward`` rests on: the request it was proved over (``envelope``, the very one), every occurrence of it
    as it came accounted for by number (``inbound``, which must be that envelope's ``occurrences``), and every one of it
    as it leaves (``final``, at least one). Phase 2 gives each its entry (inspected, checked, removed, authorized) and
    the mapping of a rewritten or dropped occurrence; Phase 1 has no prover. A proof of another request, one that names
    no part of its own, leaves any occurrence out or names one the request does not have, is not complete."""

    envelope: Envelope
    inbound: int
    final: int
    covered_inbound: frozenset[int]
    covered_final: frozenset[int]

    @property
    def complete(self) -> bool:
        return (
            isinstance(self.envelope, Envelope)
            and self.inbound == occurrences(self.envelope)
            and self.final >= 1
            and self.covered_inbound == frozenset(range(self.inbound))
            and self.covered_final == frozenset(range(self.final))
        )

    def proves(self, envelope: Envelope) -> bool:
        """Whether it is a complete proof of ``envelope``, the very request, and no other."""
        return self.envelope is envelope and self.complete


ENVELOPE_STATUSES: Mapping[str, int] = MappingProxyType(
    {
        "coverage-limit": 422,
        "envelope-state-unavailable": 503,
        **dict.fromkeys(
            (
                "envelope-binding",
                "envelope-region",
                "envelope-location",
                "envelope-route",
                "envelope-resource",
                "envelope-escape",
                "envelope-encoding",
                "envelope-control",
                "envelope-path",
                "envelope-bodyless",
                "envelope-query-unknown",
                "envelope-query-duplicate",
                "envelope-query-value",
                "envelope-header-unknown",
                "envelope-header-duplicate",
                "envelope-header-syntax",
                "envelope-header-value",
                "envelope-credential",
                "envelope-sensitive",
                "envelope-profile",
                # An exception that named what the request carries is past its lifetime (D3).
                "envelope-exception-expired",
                # A member of the body no profile names (D6), refused by its own code, its object by category alone.
                "body-unknown-member",
                # One no exception can reach: inside a provider's item or an image's source, or in a frame (I-8).
                "body-unknown-member-no-exception",
            ),
            400,
        ),
    }
)


def envelope_status(code: str) -> int:
    """RC-REC-5: policy400, exhausted coverage bounds422, unavailable required state503; no word inference."""
    return ENVELOPE_STATUSES[code]


class EnvelopeRefused(ValueError):
    """A safe category, with an optional header or query parameter name for the agent alone. Never print the detail.
    Its code, one of ``ENVELOPE_STATUSES``' fixed categories and never set again, is what a log line may hold of it
    (tests/unit/test_logging_calls.py, ``KEPT``)."""

    def __init__(
        self,
        code: str,
        *,
        header: bytes | None = None,
        query: bytes | None = None,
        template: str | None = None,
        expired: tuple[str, str] | None = None,
    ) -> None:
        if code not in ENVELOPE_STATUSES:
            raise ValueError("unknown envelope refusal category")
        super().__init__(code)
        self._code, self.template = code, template
        self._header = header
        self._query = query  # an unknown query parameter's name (P2-8a, root's ruling 5), never its value
        # An expired exception's Latet id and the day it ended (D3): never its description or what it names.
        self._expired = expired

    @property
    def code(self) -> str:
        return self._code

    @property
    def status(self) -> int:
        return envelope_status(self.code)

    def _named(self, limits: CoverageLimits) -> str | None:
        """The header's or the query parameter's name as the agent is told it: printable ASCII, cut to the limit. An
        unknown header's name has each word (between its dashes) longer than ``NAMED_WORD`` told by its length before
        the cut (the scoped check of Task 3's third fix round, P-1): a client may put the answer into its conversation,
        and a key's form in the name would come back, lowercased, in a later request's body. An unknown query
        parameter's name is told the same way (Task 8 of the 0.12 plan); a known one's, which Latet names, as it is."""
        if self._header is not None:
            kind, raw = "header", self._header
        elif self._query is not None:
            kind, raw = "query parameter", self._query
        else:
            return None
        if self._code in {"envelope-header-unknown", "envelope-query-unknown"}:
            words = "".join(chr(c) if 32 <= c <= 126 else "?" for c in raw).split("-")
            told = "-".join(word if len(word) <= NAMED_WORD else f"<{len(word)} characters>" for word in words)
            return f"{kind} " + told[: limits.header_name]
        return f"{kind} " + "".join(chr(c) if 32 <= c <= 126 else "?" for c in raw[: limits.header_name])

    def agent_message(self, limits: CoverageLimits = LIMITS) -> str:
        text = f"request envelope refused ({self.code})"
        if (named := self._named(limits)) is not None:
            text += f": {named}"
        if self.template is not None:
            # No exception can name a route (P2-6 F-4 (a)): what is possible is not to make this call.
            text += (
                f": route {self.template}; this version of Latet does not support this call, so change the agent or "
                "its settings so that it does not make it"
            )
        if self._expired is not None:
            text += f": exception {self._expired[0]} expired on {self._expired[1]}; ask the owner to renew it"
        return text

    def close_reason(self, limits: CoverageLimits = LIMITS, most: int = 123) -> str:
        """A WebSocket handshake's 1008 close reason for it: its category and the header's or query parameter's name,
        never a value; at most ``most`` UTF-8 bytes, the room a close frame has (P2-8a, root's ruling 5). ASCII alone,
        so a cut never splits a character."""
        text = f"latet: refused {self.code}"
        if (named := self._named(limits)) is not None:
            text += f": {named}"
        return text[:most]


def preflight_scope(scope: Mapping[str, Any], limits: CoverageLimits = LIMITS) -> None:
    """Raw size checks before path decoding, header tuple construction, body reads or envelope inventory."""
    query = scope.get("query_string") or b""
    raw = scope.get("raw_path")
    if raw:
        if len(raw) > limits.raw_target:
            raise EnvelopeRefused("coverage-limit")
        separator = raw.find(b"?")
        path_bytes = separator if separator >= 0 else len(raw)
    else:
        # A server without raw_path needs the same quoted fallback as envelope_of. Count one character at a time
        # before constructing it; each temporary is at most one UTF-8 scalar's escaped bytes.
        path = scope["path"]
        if len(path) > limits.raw_target:
            raise EnvelopeRefused("coverage-limit")
        path_bytes = 0
        try:
            for char in path:
                path_bytes += len(quote(char, safe="/:@!$&'()*+,;=~"))
                if path_bytes > limits.raw_target:
                    raise EnvelopeRefused("coverage-limit")
        except UnicodeError:
            raise EnvelopeRefused("envelope-encoding") from None
    if path_bytes + (1 + len(query) if query else 0) > limits.raw_target:
        raise EnvelopeRefused("coverage-limit")
    if query and query.count(b"&") + 1 > limits.query_occurrences:
        raise EnvelopeRefused("coverage-limit")
    headers = scope.get("headers") or ()
    if len(headers) > limits.header_occurrences:
        raise EnvelopeRefused("coverage-limit")
    charged = 0
    for name, value in headers:
        charged += len(name) + len(value) + 4
        if charged > limits.header_bytes:
            raise EnvelopeRefused("coverage-limit")


@dataclass(frozen=True, slots=True)
class Occurrence:
    """A location in the original envelope, without a copied value string."""

    region: str
    index: int
    start: int = 0
    end: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class Inventory:
    envelope: Envelope
    entries: tuple[Occurrence, ...]
    ledger_bytes: int
    limits: CoverageLimits


def inventory(envelope: Envelope, limits: CoverageLimits = LIMITS) -> Inventory:
    """Inventory raw occurrences before dictionaries, percent decoding or resolving a cloud host."""
    target_bytes = len(envelope.raw_path) + (1 + len(envelope.query) if envelope.query else 0)
    if target_bytes > limits.raw_target or len(envelope.headers) > limits.header_occurrences:
        raise EnvelopeRefused("coverage-limit")
    if sum(len(k) + len(v) + 4 for k, v in envelope.headers) > limits.header_bytes:
        raise EnvelopeRefused("coverage-limit")
    query_count = envelope.query.count(b"&") + 1 if envelope.query else 0
    if query_count > limits.query_occurrences:
        raise EnvelopeRefused("coverage-limit")
    entries: list[Occurrence] = []
    charged = 0

    def add(region: str, index: int = 0, start: int = 0, end: int = 0) -> None:
        nonlocal charged
        # Conservative fixed allocation charge: descriptor + tuple/list capacity + ordinal bookkeeping.
        charged += 256
        if charged > limits.ledger_bytes or len(entries) >= limits.occurrences:
            raise EnvelopeRefused("coverage-limit")
        entries.append(Occurrence(region, index, start, end))

    add("method")
    start = 0
    for end in range(len(envelope.raw_path) + 1):
        if end == len(envelope.raw_path) or envelope.raw_path[end] == 47:
            add("path", start=start, end=end)
            start = end + 1
    start = 0
    for index in range(query_count):
        end = envelope.query.find(b"&", start)
        end = len(envelope.query) if end < 0 else end
        equal = envelope.query.find(b"=", start, end)
        equal = end if equal < 0 else equal
        add("query-name", index, start, equal)
        add("query-value", index, min(equal + 1, end), end)
        start = end + 1
    for index in range(len(envelope.headers)):
        add("header-name", index)
        add("header-value", index)
    return Inventory(envelope, tuple(entries), charged, limits)


@dataclass(frozen=True, slots=True, repr=False)
class EnvelopeProof:
    """Closed-profile envelope authority, separate from the worker's own body proof."""

    envelope: Envelope
    inbound: int
    profile: str
    treatments: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]
    target: str
    checked: tuple[str, ...] = field(repr=False)
    removed: int = 0
    header_occurrences: int = LIMITS.header_occurrences
    header_bytes: int = LIMITS.header_bytes
    # The request exceptions whose components it let through, by Latet's id: the decision is then a visible ``Pass``.
    exceptions: tuple[str, ...] = ()
    # The built-in rules what it let through rests on (``request_profiles.Admission.rules``: "checked only" for D4 (b)'s
    # open list), named in the ``Pass`` beside its body's.
    rules: tuple[str, ...] = ()

    def proves(self, envelope: Envelope) -> bool:
        query = envelope.query.count(b"&") + 1 if envelope.query else 0
        count = 2 + envelope.raw_path.count(b"/") + 2 * query + 2 * len(envelope.headers)
        return (
            self.envelope is envelope
            and self.inbound == count
            and len(self.treatments) == count
            and all(value in {"inspected", "checked", "removed", "authorized"} for value in self.treatments)
        )

    def final_bounds(self, headers: tuple[tuple[bytes, bytes], ...]) -> None:
        if len(headers) > self.header_occurrences or sum(len(k) + len(v) + 4 for k, v in headers) > self.header_bytes:
            raise EnvelopeRefused("coverage-limit")


@dataclass(frozen=True, slots=True, repr=False)
class EmptyBodyProof:
    """The exact empty representation of a worker-admitted control or bodyless request."""

    envelope: Envelope

    def proves(self, envelope: Envelope) -> bool:
        return envelope is self.envelope and envelope.body == b""
