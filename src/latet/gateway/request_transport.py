"""The one way a request leaves Latet for its provider over HTTP (the request-coverage plan's ``request_transport.py``,
S1 and S2; RC-REC-8): from a decision, never from a body.

Three steps, each refusing with a ``SendRefusal`` and leaving the next undone, each used once:

- ``validate_send`` authorizes one attempt of the exchange's decision: minted by ``outcome`` and never written over, not
  a refusal, for this very exchange (the arena and the envelope the forwarder holds, never those the decision names),
  its arena unchanged since (``outcome.sendable``), under the rules in force (``request_policy.current()``), and the
  attempt the next one, with the credentials of the first: a decision sends once, and a second time only for the AWS
  retry of an expired token (S2), the same decision checked again. Nothing is built for a decision it refuses.
- ``build`` builds the request of an authorization, once, from what was authorized alone: the decision's frozen payload
  (never the arena's body or the request's, which may have changed since), the method the request came with, the
  client's headers, Latet's signature when it signs, by a client that follows no redirect, to a URL that carries no
  credentials; and freezes what was built (``FinalEnvelope``: the method, the raw URL, the headers in order, the
  content, the name TLS is asked for). Its exact raw target and ordered caller/generated headers must match the proof.
- ``send`` sends a built request once, by the client that built it, and only as it was built: a request whose method,
  target, header list, body or TLS name changed after it was frozen is not sent; httpx follows no redirect and adds no
  authorization of its own, whatever the client is set to by then. While it is sent it carries the transport's token
  (``sending()``): what the e2e and agent suites' send guard looks for (``tests/request_send_guard.py``, R-87).

A proxy the environment names takes an ``https`` request through its own CONNECT, written by httpx before the request
with the route's ``host:port`` alone (P1-0 D-10): transport, which no request content reaches. (A plain ``http``
upstream goes to an HTTP proxy whole: P2-4's classification says so.) The WebSocket adapter (S3, S4, RC-REC-9) is
guarded by the same strict proofs. The final boundary rechecks immutable authority and applied local facts without
walking the body again. The forwarder's Attempted callback runs immediately before the actual invocation; a later
failure cannot erase an earlier attempt. No setting or credential loader is called at that boundary."""

from __future__ import annotations

import base64
import contextvars
import math
import ssl
import threading
import time
import types
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
import truststore
from websockets.asyncio.client import ClientConnection, connect
from websockets.datastructures import HeadersLike
from websockets.http11 import USER_AGENT

from latet.gateway.coverage import Envelope, EnvelopeRefused, FinalEnvelope
from latet.gateway.outcome import Forward, NotSendable, Pass, sendable, still_current
from latet.gateway.request_policy import PolicySnapshot, current
from latet.gateway.sigv4 import AwsCredentials, sign
from latet.protocols.sse import MAX_DECODED

if TYPE_CHECKING:
    from latet.gateway.mediate import Prepared

Credentials = Literal["client", "aws"]  # the client's own (forwarded as they came), or Latet's AWS signature
# At most: a second attempt only for AWS's expired token.
ATTEMPTS: Mapping[str, int] = types.MappingProxyType({"client": 1, "aws": 2})
# Headers httpx adds on its own; dropped again unless the client sent them, so the upstream sees the client's request as
# the client made it. A Cookie from the client's own jar (a provider's cookie of an earlier answer, on a computer shared
# by every agent) is added by httpx at the build, frozen and sent, as 0.10 sends it: a final header with no inbound
# occurrence, for P2-1's proof and P2-4 to decide (the plan's P1-3 record).
CLIENT_DEFAULTS = ("accept", "accept-encoding", "user-agent")
_NO_AUTH = httpx.Auth()  # httpx's own authorization flow left out at the send: the request goes as it was frozen


@dataclass(frozen=True, slots=True)
class SendRefusal:
    """Why the transport did not go on, by code: ``outcome.sendable``'s (``not-a-decision``, ``unknown``,
    ``unminted``, ``tampered``, ``block``, ``proof-incomplete``, ``evidence``, ``foreign``, ``payload``, ``stale``) or
    its own: ``policy`` (not the rules in force), ``transport`` (not an HTTP request), ``attempt`` (past its ceiling),
    ``spent`` (an attempt authorized already, an authorization built already, or a request sent already),
    ``unauthorized`` (built from nothing ``validate_send`` issued), ``method``, ``credentials`` (a signature it was not
    authorized for, or none it was; an attempt with other credentials than the first's), ``redirects`` (a client that
    follows them), ``userinfo`` (credentials in the URL), ``unbuilt`` (sent from nothing ``build`` made), ``client``
    (sent by another client than the one that built it), ``changed`` (changed after it was built).
    ``settings_moved``: a ``stale`` refusal of the request's route authority (``outcome.NotSendable``), the one its
    forwarder tells as settings that changed; never the launch's ``stale`` below, nor any other."""

    code: str
    settings_moved: bool = False


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class AuthorizedRequest:
    """One attempt of a decision, authorized: the decision, the payload it froze (printed never), the rules it was
    authorized under, whose credentials it goes with, which attempt it is and how many it may have."""

    decision: Forward | Pass
    payload: bytes = field(repr=False)
    policy: PolicySnapshot
    credentials: Credentials
    attempt: int
    ceiling: int
    arena: Prepared = field(repr=False)
    attempted: Callable[[], None] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class BuiltRequest:
    """A request built from an authorization: httpx's request, what it was frozen as, the body stream it carries and
    the client that built it, which alone sends it. Printed without its target, headers or content."""

    authorized: AuthorizedRequest
    request: httpx.Request = field(repr=False)
    final: FinalEnvelope
    stream: object = field(repr=False)
    client: object = field(default=None, repr=False)
    signer: AwsCredentials | None = field(default=None, repr=False)


# A decision's attempts authorized so far, and with whose credentials.
_SPENT: weakref.WeakKeyDictionary[object, tuple[int, str]] = weakref.WeakKeyDictionary()
_ISSUED: weakref.WeakSet[AuthorizedRequest] = weakref.WeakSet()  # authorized, not built yet
_CONSUMED: weakref.WeakSet[AuthorizedRequest] = weakref.WeakSet()  # built: never again
_BUILT: weakref.WeakSet[BuiltRequest] = weakref.WeakSet()
_SENT: weakref.WeakSet[BuiltRequest] = weakref.WeakSet()
_LOCK = threading.Lock()
_SENDING: contextvars.ContextVar[httpx.Request | None] = contextvars.ContextVar("latet_sending", default=None)


def validate_send(
    decision: object,
    arena: Prepared | None,
    envelope: Envelope,
    policy: PolicySnapshot,
    attempt: int,
    *,
    credentials: Credentials = "client",
    attempted: Callable[[], None] | None = None,
) -> AuthorizedRequest | SendRefusal:
    """Attempt ``attempt`` of ``decision`` authorized for the exchange whose ``arena`` (None: it had none) and
    ``envelope`` these are, as the forwarder holds them, under ``policy``, which must be the rules in force; or why not.
    Each attempt once, in order, up to its credentials' ceiling, each with the credentials of the first; a refusal
    spends nothing."""
    if credentials not in ATTEMPTS:
        raise ValueError("not credentials Latet sends with")
    if policy is not current():
        return SendRefusal("policy")
    if not isinstance(envelope, Envelope) or envelope.transport != "http":
        return SendRefusal("transport")
    try:
        decided = sendable(decision, arena, envelope)
    except NotSendable as refused:
        return SendRefusal(refused.code, refused.settings_moved)
    if decided.policy != policy:
        return SendRefusal("policy")
    ceiling = ATTEMPTS[credentials]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= ceiling:
        return SendRefusal("attempt")
    if not isinstance(decided.payload, bytes):
        return SendRefusal("payload")
    with _LOCK:
        spent, first = _SPENT.get(decided, (0, credentials))
        if spent != attempt - 1:
            return SendRefusal("spent")
        if first != credentials:
            return SendRefusal("credentials")
        _SPENT[decided] = (attempt, credentials)
        assert arena is not None
        authorized = AuthorizedRequest(
            decided, decided.payload, policy, credentials, attempt, ceiling, arena, attempted
        )
        _ISSUED.add(authorized)
    return authorized


def freeze(request: httpx.Request) -> FinalEnvelope:
    """``request`` as it would go out: its method, raw URL (percent-escapes kept), headers in order with their
    duplicates (credentials included, never printed), content, and the TLS name asked for when it is not the URL's."""
    url = request.url
    raw = url.scheme.encode("ascii") + b"://" + url.netloc + url.raw_path
    tls = request.extensions.get("sni_hostname")
    return FinalEnvelope(request.method, raw, tuple(request.headers.raw), request.content, tls)


def build(
    authorized: AuthorizedRequest,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    signer: tuple[AwsCredentials, str] | None = None,
    host: str | None = None,
    extensions: dict[str, Any] | None = None,
) -> BuiltRequest | SendRefusal:
    """The request of ``authorized`` as it goes out, built once by ``client``, which sends it: to ``url``, with
    ``headers`` (the client's, forwardable), its payload the decision's, signed with ``signer`` when Latet signs (its
    URL as it goes out, percent-escapes kept, and its final body). ``host``: the Host header to send (a destination's
    name, while ``url`` carries the address checked), with ``extensions`` for httpx (TLS's name). Frozen as built."""
    if not isinstance(authorized, AuthorizedRequest):
        return SendRefusal("unauthorized")
    with _LOCK:
        issued, consumed = authorized in _ISSUED, authorized in _CONSUMED
    if not issued:
        return SendRefusal("spent" if consumed else "unauthorized")
    if method != authorized.decision.envelope.method:
        return SendRefusal("method")
    if (signer is not None) != (authorized.credentials == "aws"):
        return SendRefusal("credentials")
    if signer is not None and not _credentials_current(signer[0]):
        return SendRefusal("credentials")
    if client.follow_redirects:
        return SendRefusal("redirects")
    if httpx.URL(url).userinfo:
        return SendRefusal("userinfo")
    proof = authorized.decision.envelope_proof
    expected_final: tuple[tuple[bytes, bytes], ...] | None = None
    expected_url: bytes | None = None
    raw_target: bytes | None = None
    generated: list[tuple[str, str]] | None = None
    if proof is not None:
        expected = list(proof.headers)
        if signer is not None:
            expected = [
                (k, v) for k, v in expected if k.lower() != "authorization" and not k.lower().startswith("x-amz-")
            ]
        if headers != expected:
            return SendRefusal("envelope-headers")
        logical, outgoing_url = urlsplit(proof.target), httpx.URL(url)
        expected_url = outgoing_url.scheme.encode("ascii") + b"://" + outgoing_url.netloc + outgoing_url.raw_path
        raw_target = (logical.path or "/").encode("ascii") + (
            b"?" + logical.query.encode("ascii") if logical.query else b""
        )
        # Pinning may change the address only: checked Host and TLS name must remain the logical destination's.
        if outgoing_url.raw_path != raw_target:
            return SendRefusal("envelope-target")
        if host is None:
            if url != proof.target:
                return SendRefusal("envelope-target")
            if (extensions or {}).get("sni_hostname", logical.hostname) != logical.hostname:
                return SendRefusal("envelope-destination")
        elif host != logical.netloc or (extensions or {}).get("sni_hostname") != logical.hostname:
            return SendRefusal("envelope-destination")
        # An ambient cookie/default header has no inbound provenance. Refuse before the client builds anything.
        if (
            client.cookies
            or any(name.lower() not in (*CLIENT_DEFAULTS, "connection") for name in client.headers)
            or client.headers.get("connection", "keep-alive") != "keep-alive"
        ):
            return SendRefusal("envelope-client-headers")
        framed = [("host", host or outgoing_url.netloc.decode("ascii"))]
        if "connection" in client.headers:
            framed.append(("connection", "keep-alive"))
        framed.extend(headers)
        if authorized.payload or method not in {"GET", "HEAD"}:
            framed.append(("content-length", str(len(authorized.payload))))
        if signer is not None:
            generated = sign(method, url, authorized.payload, signer[0], signer[1])
            framed = [(k, v) for k, v in framed if k.lower() not in {name.lower() for name, _ in generated}]
            framed.extend(generated)
        try:
            expected_final = tuple((k.encode("ascii"), v.encode("latin-1")) for k, v in framed)
            proof.final_bounds(expected_final)
        except (EnvelopeRefused, UnicodeError):
            return SendRefusal("coverage-limit")
    with _LOCK:  # taken once: a second build of it, concurrent or later, is spent
        if authorized not in _ISSUED:
            return SendRefusal("spent")
        _ISSUED.discard(authorized)
        _CONSUMED.add(authorized)
    content = authorized.payload
    outgoing = client.build_request(method, url, headers=headers, content=content, extensions=extensions or {})
    if host is not None:
        outgoing.headers["host"] = host
    sent = {k.lower() for k, _ in headers}
    for name in CLIENT_DEFAULTS:
        if name not in sent and name in outgoing.headers:
            del outgoing.headers[name]
    if signer is not None:
        at = f"{outgoing.url.scheme}://{outgoing.url.netloc.decode('ascii')}{outgoing.url.raw_path.decode('ascii')}"
        for name, value in (
            generated if generated is not None else sign(method, at, outgoing.content, signer[0], signer[1])
        ):
            outgoing.headers[name] = value
    final = freeze(outgoing)
    if final.content != authorized.payload:
        return SendRefusal("payload")
    if proof is not None:
        try:
            proof.final_bounds(final.headers)
        except EnvelopeRefused:
            return SendRefusal("coverage-limit")
        # httpx may position its framing header before the caller's headers. Preserve all other ordering exactly.
        actual = tuple((k.lower(), v) for k, v in final.headers)
        expected_ordered = tuple((k.lower(), v) for k, v in expected_final or ())
        generated_names = {b"host", b"content-length"}
        if (
            sorted((k, v) for k, v in actual if k in generated_names)
            != sorted((k, v) for k, v in expected_ordered if k in generated_names)
            or tuple((k, v) for k, v in actual if k not in generated_names)
            != tuple((k, v) for k, v in expected_ordered if k not in generated_names)
            or final.url != expected_url
            or outgoing.url.raw_path != raw_target
            or final.tls_name != (extensions or {}).get("sni_hostname")
        ):
            return SendRefusal("envelope-final")
    built = BuiltRequest(authorized, outgoing, final, outgoing.stream, client, signer[0] if signer else None)
    with _LOCK:
        _BUILT.add(built)
    return built


def _as_built(built: BuiltRequest) -> bool:
    """Whether its request is still what was frozen: the same method, raw URL, header list, content and TLS name,
    carried by the very stream it was built with, whose bytes are the decision's."""
    request, final, payload = built.request, built.final, built.authorized.payload
    try:
        now = freeze(request)
        # httpx's stream of a body given whole is that very body, in one chunk: compared without a copy of it.
        chunks = list(request.stream) if isinstance(request.stream, httpx.ByteStream) else None
    except Exception:  # a request that cannot be read as it would go out is not what was built
        return False
    return (
        request.stream is built.stream
        and chunks is not None
        and len(chunks) == 1
        and chunks[0] is payload
        and final.content is payload
        and now.content is payload
        and (now.method, now.url, now.headers, now.tls_name) == (final.method, final.url, final.headers, final.tls_name)
    )


async def send(client: httpx.AsyncClient, built: BuiltRequest) -> httpx.Response | SendRefusal:
    """``built`` sent once by ``client``, the one that built it (streamed: nothing of its answer read yet), only as it
    was built, following no redirect and adding no authorization, carrying the transport's token while it is sent; or
    why not."""
    with _LOCK:
        if not isinstance(built, BuiltRequest) or built not in _BUILT:
            return SendRefusal("unbuilt")
        if built in _SENT:
            return SendRefusal("spent")
    if client is not built.client:
        return SendRefusal("client")
    if built.authorized.arena.protected_current is not None and not await built.authorized.arena.protected_current():
        return SendRefusal("stale")
    if not _as_built(built):
        return SendRefusal("changed")
    if (refused := _fresh(built.authorized.decision, built.authorized.arena)) is not None:
        return refused
    if built.signer is not None and not _credentials_current(built.signer):
        return SendRefusal("credentials")
    with _LOCK:
        if built in _SENT:
            return SendRefusal("spent")
        _SENT.add(built)
    token = _SENDING.set(built.request)
    try:
        if built.authorized.attempted is not None:
            built.authorized.attempted()
        return await client.send(built.request, stream=True, auth=_NO_AUTH, follow_redirects=False)
    finally:
        _SENDING.reset(token)


def _credentials_current(credentials: AwsCredentials) -> bool:
    return bool(credentials.access_key and credentials.secret_key) and (
        credentials.expires is None or (math.isfinite(credentials.expires) and credentials.expires > time.time())
    )


def _fresh(decision: Forward | Pass, arena: Prepared) -> SendRefusal | None:
    try:
        still_current(decision, arena)
    except NotSendable as error:
        return SendRefusal(error.code, error.settings_moved)
    return None


def sending() -> httpx.Request | None:
    """The request the transport is sending now, in this context (the token the send guard looks for); None
    elsewhere."""
    return _SENDING.get()


# --- the WebSocket adapter: S3 (the handshake) and S4 (each frame), RC-REC-9 -----------------------------------------

# What websockets adds to a handshake of its own, allowed by name (RC-REC-9): no User-Agent (``user_agent_header``
# None).
WS_LIBRARY_HEADERS = frozenset(
    {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}
)
_WS_SSL = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class AuthorizedConnect:
    """A WebSocket's handshake, authorized once: its decision, the rules it was authorized under, and the client's
    headers it carries (in order, printed never)."""

    decision: Forward | Pass
    policy: PolicySnapshot
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    arena: Prepared = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class AuthorizedFrame:
    """One frame of a WebSocket, authorized once for the connection it goes on: its decision and the payload it froze
    (printed never)."""

    decision: Forward | Pass
    payload: bytes | str = field(repr=False)
    connection: object = field(repr=False)
    arena: Prepared = field(repr=False)
    on_send: Callable[[], None] | None = field(default=None, repr=False)


_CONNECTS: weakref.WeakSet[AuthorizedConnect] = weakref.WeakSet()  # authorized, not opened yet
_FRAMES: weakref.WeakSet[AuthorizedFrame] = weakref.WeakSet()  # authorized, not sent yet
_DECIDED: weakref.WeakSet[object] = weakref.WeakSet()  # handshake and frame decisions authorized already
_OPENED: weakref.WeakKeyDictionary[object, Envelope] = weakref.WeakKeyDictionary()  # socket -> handshake context
_CONNECTING: contextvars.ContextVar[AuthorizedConnect | None] = contextvars.ContextVar("latet_connecting", default=None)


# Handshakes the adapter refused as websockets built them (RC-REC-9), and why: nothing of them written.
_REFUSED: weakref.WeakKeyDictionary[AuthorizedConnect, str] = weakref.WeakKeyDictionary()


def _authorized(
    decision: object, arena: Prepared | None, envelope: Envelope, policy: PolicySnapshot, transport: str
) -> Forward | Pass | SendRefusal:
    if policy is not current():
        return SendRefusal("policy")
    if not isinstance(envelope, Envelope) or envelope.transport != transport:
        return SendRefusal("transport")
    try:
        decided = sendable(decision, arena, envelope)
    except NotSendable as refused:
        return SendRefusal(refused.code, refused.settings_moved)
    if decided.policy != policy:
        return SendRefusal("policy")
    with _LOCK:
        if decided in _DECIDED:
            return SendRefusal("spent")
        _DECIDED.add(decided)
    return decided


def validate_connect(
    decision: object,
    envelope: Envelope,
    policy: PolicySnapshot,
    headers: list[tuple[str, str]],
    arena: Prepared | None = None,
) -> AuthorizedConnect | SendRefusal:
    """The handshake ``envelope`` is (a WebSocket's, as the client sent it) authorized once on its ``decision``, under
    ``policy``, which must be the rules in force, with ``headers`` (the client's, forwardable); or why not."""
    decided = _authorized(decision, arena, envelope, policy, "ws-handshake")
    if isinstance(decided, SendRefusal):
        return decided
    assert arena is not None
    if decided.envelope_proof is None or tuple(headers) != decided.envelope_proof.headers:
        return SendRefusal("envelope-headers")
    authorized = AuthorizedConnect(decided, policy, tuple(headers), arena)
    with _LOCK:
        _CONNECTS.add(authorized)
    return authorized


def validate_frame(
    decision: object,
    arena: Prepared | None,
    envelope: Envelope,
    policy: PolicySnapshot,
    connection: object,
    *,
    on_send: Callable[[], None] | None = None,
) -> AuthorizedFrame | SendRefusal:
    """One frame's ``decision`` authorized once for the exchange whose ``arena`` (None: a frame Latet reads nothing of)
    and ``envelope`` these are, under the rules in force, to go on ``connection``, which the adapter opened; or why
    not."""
    with _LOCK:
        try:
            handshake = _OPENED.get(connection)
        except TypeError:  # an arbitrary argument that cannot be a weakly held opened socket
            handshake = None
    if handshake is None:
        return SendRefusal("unbuilt")
    if not isinstance(envelope, Envelope) or (
        envelope.method,
        envelope.raw_path,
        envelope.query,
        envelope.headers,
        envelope.destination,
        envelope.identity,
        envelope.connection,
    ) != (
        handshake.method,
        handshake.raw_path,
        handshake.query,
        handshake.headers,
        handshake.destination,
        handshake.identity,
        handshake.connection,
    ):
        return SendRefusal("foreign")
    decided = _authorized(decision, arena, envelope, policy, "ws-frame")
    if isinstance(decided, SendRefusal):
        return decided
    assert arena is not None
    authorized = AuthorizedFrame(decided, decided.payload, connection, arena, on_send)
    with _LOCK:
        _FRAMES.add(authorized)
    return authorized


def _handshake_refusal(request: Any, authorized: AuthorizedConnect | None, generated_key: str) -> str | None:
    """Why the handshake websockets built (``request``) is not the one ``authorized``, or None: the client's headers it
    carries, last and in order, and before them only what the library adds of its own, by name (RC-REC-9)."""
    if authorized is None:
        return "unauthorized"
    if (refused := _fresh(authorized.decision, authorized.arena)) is not None:
        return refused.code
    proof = authorized.decision.envelope_proof
    if proof is None:
        return "proof-incomplete"
    target = urlsplit(proof.target)
    expected_path = (target.path or "/") + ("?" + target.query if target.query else "")
    if request.path != expected_path:
        return "envelope-target"
    built = list(request.headers.raw_items())
    given = list(authorized.headers)
    prefix = len(built) - len(given)
    if prefix < 0 or built[prefix:] != given:
        return "changed"
    generated = built[:prefix]
    expected = [
        ("Host", target.netloc),
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Key", generated_key),
        ("Sec-WebSocket-Version", "13"),
        ("Sec-WebSocket-Extensions", "permessage-deflate; client_max_window_bits"),
    ]
    if len(generated) != len(expected):
        return "changed"
    for (name, value), (key, fixed) in zip(generated, expected, strict=True):
        if name.lower() not in WS_LIBRARY_HEADERS or name != key or (fixed is not None and value != fixed):
            return "changed"
        if key == "Sec-WebSocket-Key":
            try:
                if len(value) != 24 or len(base64.b64decode(value, validate=True)) != 16:
                    return "changed"
            except ValueError:
                return "changed"
    try:
        proof.final_bounds(tuple((k.encode("ascii"), v.encode("latin-1")) for k, v in built))
    except (EnvelopeRefused, UnicodeError):
        return "coverage-limit"
    return None


class _Checked(ClientConnection):
    """websockets' connection, the handshake it builds seen before a byte of it is written (RC-REC-9): frozen
    (``final``), and refused, unwritten, when it is not the handshake authorized (``authorized``, which the
    send guard checks against ``connecting()``)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.authorized = _CONNECTING.get()
        self.final: FinalEnvelope | None = None
        written = self.protocol.send_request

        def checked(request: Any) -> None:
            raw = tuple((k.encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.raw_items())
            self.final = FinalEnvelope("GET", request.path.encode("latin-1"), raw, b"")
            code = _handshake_refusal(request, self.authorized, self.protocol.key)
            if code is not None:  # nothing of it written: the connection dropped, and why kept for ``connect_ws``
                if self.authorized is not None:
                    with _LOCK:
                        _REFUSED[self.authorized] = code
                self.transport.abort()
                return
            written(request)

        self.protocol.send_request = checked  # type: ignore[method-assign]

    async def handshake(
        self, additional_headers: HeadersLike | None = None, user_agent_header: str | None = USER_AGENT
    ) -> None:
        authorized = self.authorized
        if (
            authorized is not None
            and authorized.arena.protected_current is not None
            and not await authorized.arena.protected_current()
        ):
            with _LOCK:
                _REFUSED[authorized] = "stale"
            self.transport.abort()
            raise OSError("protected handshake authority is stale")
        # The library sends under send_context; its checked send_request above
        # revalidates synchronous authority after this final await and before bytes.
        await super().handshake(additional_headers, user_agent_header)


class _NoRedirect(connect):
    """websockets' ``connect``, following no redirect, on a computer as on a server, as Latet's HTTP requests follow
    none: whether it leads to another origin (into a server's own network) or to the same one (a path or a query the
    identity chooses could make an open redirect of it send Latet on). A redirect is raised as the handshake's refusal
    (``InvalidStatus``), which the relay answers with a clear error, handing the client no address to go to."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


async def connect_ws(
    authorized: AuthorizedConnect, url: str, pinned: tuple[str, int] | None, max_size: int = MAX_DECODED
) -> ClientConnection | SendRefusal:
    """S3: the upstream's WebSocket of an authorized handshake, opened once, following no redirect (``_NoRedirect``),
    its handshake checked as websockets builds it (``_Checked``); or why not. ``pinned``: on a Latet server, a
    destination of the identity's own, reached at the address checked while the Host header and TLS's name stay the
    destination's (``url``'s), taking no proxy of the server's environment. The upstream's refusal of the handshake, a
    redirect included, is raised as websockets raises it (``InvalidStatus``), and so are the failures to reach it.
    ``max_size``: the largest message of the upstream's it reads, inflated (the relay's bound)."""
    with _LOCK:
        if not isinstance(authorized, AuthorizedConnect) or authorized not in _CONNECTS:
            return SendRefusal("unauthorized")
        _CONNECTS.discard(authorized)
    proof = authorized.decision.envelope_proof
    if proof is None or url != proof.target:
        return SendRefusal("envelope-target")
    if (refused := _fresh(authorized.decision, authorized.arena)) is not None:
        return refused
    options: dict[str, Any] = {
        "additional_headers": list(authorized.headers),
        "user_agent_header": None,
        "compression": "deflate",
        "max_size": max_size,  # one message of the upstream's, inflated: no more than Latet reads of an answer
        "open_timeout": 30,
        "ping_interval": 20,
        "ping_timeout": 60,
        "ssl": _WS_SSL if url.startswith("wss://") else None,
        "create_connection": _Checked,
    }
    if pinned is not None:
        options |= {"host": pinned[0], "port": pinned[1], "proxy": None}
    token = _CONNECTING.set(authorized)
    try:
        opened = await _NoRedirect(url, **options)
    except Exception:
        with _LOCK:
            code = _REFUSED.pop(authorized, None)
        if code is None:
            raise
        return SendRefusal(code)
    finally:
        _CONNECTING.reset(token)
    with _LOCK:
        code = _REFUSED.pop(authorized, None)
        if code is None:
            _OPENED[opened] = authorized.decision.envelope
    if code is not None:  # a handshake refused that the library took for one that ended: never opened
        opened.transport.abort()
        return SendRefusal(code)
    return opened


async def send_frame(connection: ClientConnection, authorized: AuthorizedFrame) -> SendRefusal | None:
    """S4: an authorized frame sent once, as it was decided, on the connection it was authorized for; or why not."""
    with _LOCK:
        if not isinstance(authorized, AuthorizedFrame) or authorized not in _FRAMES:
            return SendRefusal("unauthorized")
        _FRAMES.discard(authorized)
    if connection is not authorized.connection:
        return SendRefusal("client")
    if authorized.arena.protected_current is not None and not await authorized.arena.protected_current():
        return SendRefusal("stale")
    if (refused := _fresh(authorized.decision, authorized.arena)) is not None:
        return refused
    if authorized.on_send is not None:
        authorized.on_send()
    await connection.send(authorized.payload)
    return None


def connecting() -> AuthorizedConnect | None:
    """The handshake the adapter is opening now, in this context (the WebSocket token the send guard looks for, with
    the connection that carries it); None elsewhere."""
    return _CONNECTING.get()
