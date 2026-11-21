"""Events to the company's security systems (SIEM): a JSON webhook over HTTPS (Elastic Agent's HTTP endpoint, a Logic
App in front of Microsoft Sentinel, a collector of yours), Splunk's HTTP Event Collector, or syslog (RFC 5424) over TLS
(RFC 5425). Each output takes a batch of events and says whether it took it (``send``); `delivery.py` sends each
output's queue, in order.

Each connection goes to the address E0b's egress resolves and checks at that moment (R10, as E3's identity provider is
reached): an administrator's own service, so the company's network is allowed, never a link-local address (the cloud's
metadata address), a zone or an IPv6 form carrying an IPv4 address, and this computer only where the address names
it; TLS's name stays the one written. A name with several addresses is reached at the next one the same lookup
checked when the TCP connection to the one before failed, before any byte of a batch left (``request_transport``:
eight addresses at most, within the connect time; never once a connection was made, so that a batch is never sent
twice). No proxy of the environment is used: a proxy would resolve the name elsewhere.
A receiver's answer is read as E3 reads an identity provider's (C1): nothing compressed is asked for or read, at most
``ANSWER_BYTES`` of it, and the whole send (the lookup, the connection, TLS, the body out, the answer in) within
``SEND_SECONDS``: a receiver never holds an instance's jobs, nor fills its memory. Every function here that connects
runs in a worker thread (an HTTP send runs its own event loop there; ``pinned`` runs egress's lookup to its end).

What is sent is the events' JSON, and every event's free text (a project's name, which may hold ``"``, ``]``, ``=`` or
``|``) stays inside it: syslog's header takes the time and the type alone, each as the schema writes it or ``-``, and
no field of an event goes into a header. A secret goes where ``{secret}`` stands: percent-encoded in the address, so
that its characters never reshape it, and in a header only when it is visible ASCII."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import ssl
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
import truststore
from cryptography.exceptions import InvalidTag

from latet import __version__
from latet.ee.events.schema import epoch, iso
from latet.ee.settings import SiemOutput, siem_bound
from latet.events import FIELDS
from latet.gateway import request_transport
from latet.server import egress

USER_AGENT = f"latet/{__version__}"
SOURCETYPE = "latet:event"
FACILITY = 13  # log audit
CONNECT_SECONDS = 10.0  # each phase of a connection
# One send whole (C1): the lookup, the connection, TLS, the body out and the answer in. Below half the delivery's lock
# (LOCK_SECONDS, 60 s), so that a send is over long before the lock it was made under can be another instance's.
SEND_SECONDS = 20.0
ANSWER_BYTES = 64 * 1024  # what is read of an answer at most: a HEC's or a webhook's is a few bytes, and unneeded
_IDENTITY = frozenset({"", "identity"})
# It may take the batch later: its token, its address, a method or a type it is set to take (405, 410, 411, 415: a
# receiver moved or configured otherwise, never the batch itself).
RETRY = frozenset({401, 403, 404, 405, 408, 409, 410, 411, 415, 425, 429})
# Whose lookups egress counts: each output's own (`siem:<name>`, fix round 1), apart from every identity's and from
# each other's, so that outputs whose names never resolve hold none of the lookups another output needs.
HOLDER = "siem"
# Fixed words: the address is never repeated (its query may hold a secret), nor what it resolved to.
NOT_REACHED = (
    "its address cannot be reached now: its name did not resolve, or not in time, or too many of its lookups were "
    "under way, or it resolves to an address the server never reaches"
)
_CHUNK = 16 * 1024  # what syslog hands TLS at a time, each within what is left of the send's deadline
CLOSE_SECONDS = 2.0  # how long syslog waits for the receiver's close once all is sent (within SEND_SECONDS)
# What syslog's header takes of an event (RFC 5424's TIMESTAMP and MSGID), as the schema writes them; else "-".
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MSGID = re.compile(r"[a-z][a-z-]{0,31}")
# Why a certificate was refused, in words of Latet's for each of OpenSSL's codes that say a fixed thing (the
# X509_V_ERR_ names); any other code, or none (the system's verifier, through truststore, writes words of its own, a
# name among them), is said as "not trusted" alone. Python writes the host into the message of 62 and 64.
_REFUSED_FOR = {
    2: "its issuer is not one the server trusts",  # UNABLE_TO_GET_ISSUER_CERT
    9: "it is not valid yet",  # CERT_NOT_YET_VALID
    10: "it has expired",  # CERT_HAS_EXPIRED
    18: "it is self-signed",  # DEPTH_ZERO_SELF_SIGNED_CERT
    19: "a certificate of its chain is self-signed",  # SELF_SIGNED_CERT_IN_CHAIN
    20: "its issuer is not one the server trusts",  # UNABLE_TO_GET_ISSUER_CERT_LOCALLY
    21: "its issuer is not one the server trusts",  # UNABLE_TO_VERIFY_LEAF_SIGNATURE
    23: "it was revoked",  # CERT_REVOKED
    26: "it is not for a server",  # INVALID_PURPOSE
    62: "it is not for the name in the address",  # HOSTNAME_MISMATCH
    64: "it is not for the name in the address",  # IP_ADDRESS_MISMATCH
}
# What may stay as written when a secret goes into an address: unreserved characters, and `=` (a base64 token's end).
_KEPT_IN_ADDRESS = "-._~="


class SendError(Exception):
    """An output did not take a batch: ``retry`` when it may later (it is down, its token is wrong), not when it
    refused the batch itself."""

    def __init__(self, message: str, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


class Output(Protocol):
    name: str

    def send(self, events: Sequence[Mapping[str, Any]]) -> None: ...
    def close(self) -> None: ...


def default_context() -> ssl.SSLContext:
    """The operating system's certificate store, as for the upstreams: a company's own authority is in it."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _why(exc: BaseException) -> str:
    """Why a connection failed, in words that never repeat its address (it may hold a secret)."""
    cause: BaseException | None = exc
    for _ in range(8):
        if cause is None:
            break
        if isinstance(cause, ssl.SSLCertVerificationError):  # its verify_message is never said
            said = _REFUSED_FOR.get(cause.verify_code) if isinstance(cause.verify_code, int) else None
            return f"its certificate is not trusted ({said})" if said else "its certificate is not trusted"
        if isinstance(cause, ssl.SSLError):
            return "the TLS handshake failed"
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "it did not answer in time"
    return "it could not be reached"


def holder(name: str) -> str:
    """Whose lookups egress counts for the output ``name``: its own (fix round 1)."""
    return f"{HOLDER}:{name}"


def pinned(url: str, name: str) -> tuple[str, str, dict[str, Any]]:
    """In a worker thread: ``url`` with its host replaced by the address egress resolved and checked now, the ``Host``
    to send, and the TLS name to keep (``egress.pin``, R10), the lookup counted for the output ``name``. SendError
    (``NOT_REACHED``, tried again later: a name may point back where it may) when egress refuses it."""
    try:
        return asyncio.run(egress.pin(url, holder=holder(name), inside=True))
    except egress.EgressRefused:
        raise SendError(NOT_REACHED) from None


class PinnedTransport(httpx.AsyncBaseTransport):
    """How a webhook or Splunk is reached: each request sent to the address egress resolves and checks for it now
    (R10), its lookup counted for the output ``name``, its ``Host`` and TLS's name the ones written, with no proxy and
    no retry of httpx's own; at the next address the same lookup checked when the TCP connection to the one before
    failed, before any byte of the batch left (``request_transport.reaching``), each connection kept for its address
    and TLS name alone, with httpx's own limits."""

    def __init__(self, context: ssl.SSLContext, name: str) -> None:
        self._sent = request_transport.reaching(context)
        self._holder = holder(name)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            url, host, extensions = await egress.pin(str(request.url), holder=self._holder, inside=True)
        except egress.EgressRefused:
            raise SendError(NOT_REACHED) from None
        request.url = httpx.URL(url)
        request.headers["host"] = host
        request.extensions = {**request.extensions, **extensions}
        return await self._sent.handle_async_request(request)

    async def aclose(self) -> None:
        await self._sent.aclose()


def _visible(text: str) -> bool:
    """Visible ASCII, code points 32 to 126: what a header's value is sent in (I8)."""
    return all(32 <= ord(c) <= 126 for c in text)


def _filled(text: str, secret: str | None, name: str, *, address: bool = False) -> str:
    """``text`` with the secret where ``{secret}`` stands: percent-encoded in an ``address``, so that a ``/``, ``?``,
    ``#`` or ``&`` of it never reshapes where the batch goes."""
    if "{secret}" not in text:
        return text
    if secret is None:
        raise SendError(f"its secret is not set: `latet server siem secret {name}`")
    return text.replace("{secret}", quote(secret, safe=_KEPT_IN_ADDRESS) if address else secret)


def _header(value: str, name: str) -> str:
    """A header's value as sent: one line of visible ASCII, or SendError (held: the secret is set again), never
    httpx's UnicodeEncodeError nor a second header (I8)."""
    if not _visible(value):
        raise SendError(
            f"its secret cannot go in a header: a header's value is visible ASCII (set it again: `latet server siem "
            f"secret {name}`)"
        )
    return value


class HttpOutput:
    """A JSON webhook, or Splunk's HTTP Event Collector."""

    def __init__(
        self, settings: SiemOutput, secret: str | None, context: ssl.SSLContext, *, host: str | None = None
    ) -> None:
        self.name, self.settings, self.host, self.context = settings.name, settings, host, context
        # The output's own secret, or none: a secret given for an output that names none is no secret of its own (I1).
        self.secret = secret if settings.secret is not None else None

    def request(self, events: Sequence[Mapping[str, Any]]) -> tuple[str, bytes, dict[str, str]]:
        """Where a batch goes, its body and its headers (an answer in no compression asked for: C1)."""
        s = self.settings
        headers = {"user-agent": USER_AGENT, "accept-encoding": "identity"}
        if s.kind == "splunk":
            if self.secret is None:
                raise SendError(f"its HEC token is not set: `latet server siem secret {s.name}`")
            body = b"".join(json.dumps(self._envelope(e), separators=(",", ":")).encode() for e in events)
            headers |= {"authorization": _header(f"Splunk {self.secret}", s.name), "content-type": "application/json"}
            return _filled(s.url, self.secret, s.name, address=True), body, headers
        if s.secret is not None and self.secret is None:  # never sent unsigned where a signature is expected
            raise SendError(f"its secret is not set: `latet server siem secret {s.name}`")
        if s.format == "ndjson":
            body = b"".join(json.dumps(e, separators=(",", ":")).encode() + b"\n" for e in events)
            headers["content-type"] = "application/x-ndjson"
        else:
            body = json.dumps(list(events), separators=(",", ":")).encode()
            headers["content-type"] = "application/json"
        for key, value in s.headers.items():
            headers[key.lower()] = _header(_filled(value, self.secret, s.name), s.name)
        if self.secret is not None:
            headers["x-latet-signature"] = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return _filled(s.url, self.secret, s.name, address=True), body, headers

    def _envelope(self, event: Mapping[str, Any]) -> dict[str, Any]:
        envelope: dict[str, Any] = {"time": epoch(event["ts"]), "source": "latet", "sourcetype": SOURCETYPE}
        if self.host:
            envelope["host"] = self.host
        return envelope | {"event": event}

    def send(self, events: Sequence[Mapping[str, Any]]) -> None:
        """In a worker thread (no event loop there): one POST, whole within ``SEND_SECONDS``. A redirect is never
        followed, and holds the batch as an answer of 5xx does: only a 4xx that is no matter of the token or the
        address refuses it."""
        url, body, headers = self.request(events)
        status = asyncio.run(self._post(url, body, headers))
        if 200 <= status < 300:
            return
        raise SendError(f"it answered {status}", retry=not 400 <= status < 500 or status in RETRY)

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        """The answer's status. Its body is read raw, never decoded, ``ANSWER_BYTES`` at most, and the body of an answer
        said to be compressed is not read at all (a few compressed bytes can stand for gigabytes); the whole within
        ``SEND_SECONDS`` (C1, as ``oidc._read``). A client of its own: no proxy and no .netrc of the environment
        (``trust_env=False``, M14), each request pinned."""
        try:
            async with (
                asyncio.timeout(SEND_SECONDS),
                httpx.AsyncClient(
                    transport=PinnedTransport(self.context, self.name), timeout=CONNECT_SECONDS,
                    follow_redirects=False, trust_env=False,
                ) as client,
                client.stream("POST", url, content=body, headers=headers) as response,
            ):  # fmt: skip
                if response.headers.get("content-encoding", "").strip().lower() not in _IDENTITY:
                    # Its status says whether it took the batch: a body compressed though Latet asked for none is
                    # neither read nor inflated (C1), and a batch taken is never sent again for it (fix round 1).
                    return response.status_code
                read = 0
                async for chunk in response.aiter_raw():
                    read += len(chunk)
                    if read > ANSWER_BYTES:
                        break  # the status is the answer: what it says past this is not needed
                return response.status_code
        except TimeoutError:  # the whole send, its connection and its answer together
            raise SendError(f"it did not answer within {SEND_SECONDS:g} seconds") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise SendError(_why(exc)) from None

    def close(self) -> None:
        return None  # a client is made for each send, and closed with it


class SyslogOutput:
    """Syslog over TLS: each event an RFC 5424 message, framed by its length (RFC 5425)."""

    def __init__(self, settings: SiemOutput, context: ssl.SSLContext, *, host: str | None = None) -> None:
        parts = urlsplit(settings.url)
        self.name, self.address, self.port = settings.name, parts.hostname or "", parts.port or 6514
        self.host, self.context = host, context

    def frame(self, event: Mapping[str, Any]) -> bytes:
        """One message: facility 13 (log audit), a warning for a stopped request, the event's JSON as its text, and the
        server's host name (never a computer's: Latet masks those). The header takes the event's time and type only as
        the schema writes them (``-`` otherwise), so that nothing of an event reshapes the frame."""
        kind, ts = event.get("type"), event.get("ts")
        severity = 4 if kind == "stopped" else 6
        stamp = ts if isinstance(ts, str) and _TIMESTAMP.fullmatch(ts) else "-"
        msgid = kind if isinstance(kind, str) and _MSGID.fullmatch(kind) else "-"
        body = json.dumps(event, separators=(",", ":"))  # ASCII: RFC 5424's MSG-ANY, so no BOM
        message = f"<{FACILITY * 8 + severity}>1 {stamp} {_hostname(self.host)} latet - {msgid} - {body}"
        data = message.encode("ascii")
        return str(len(data)).encode() + b" " + data

    def send(self, events: Sequence[Mapping[str, Any]]) -> None:
        """The events, each a frame, over one TLS connection to the first of the addresses egress checked now for the
        receiver's name that takes a TCP connection (R10, ``request_transport.connected``): the next one only when the
        connection to the one before failed, so that nothing of a batch is ever sent to two of them."""
        payload = b"".join(self.frame(event) for event in events)
        deadline = time.monotonic() + SEND_SECONDS  # C1: the lookup, the connection, TLS and the sending, together
        where, _, extensions = pinned(f"tls://{_netloc(self.address)}:{self.port}", self.name)  # checked now (R10)
        checked = egress.checked(extensions, urlsplit(where).hostname or "")  # written as numbers, never looked up

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return remaining

        try:
            with request_transport.connected(checked, self.port, min(CONNECT_SECONDS, left())) as raw:
                raw.settimeout(left())  # the handshake's
                with self.context.wrap_socket(raw, server_hostname=self.address) as tls:  # the name written
                    view = memoryview(payload)
                    while view:  # each piece within what is left: a receiver that reads slowly is cut at the deadline
                        tls.settimeout(left())
                        view = view[tls.send(view[:_CHUNK]) :]
                    # RFC 5425, 4.4: TLS's close_notify, and the receiver's own close waited for a moment, so that what
                    # it sent unasked (TLS 1.3's session tickets) is read: a socket closed with them unread resets the
                    # connection, and the receiver may then lose what it was sent. What was sent is sent: a receiver
                    # that does not close in time, or closes without its own close_notify, fails nothing.
                    with contextlib.suppress(OSError, ValueError):
                        tls.settimeout(min(CLOSE_SECONDS, left()))
                        tls.unwrap()
        except TimeoutError:
            raise SendError(f"it did not answer within {SEND_SECONDS:g} seconds") from None
        except OSError as exc:  # refused, unreachable, TLS (an ssl.SSLError is an OSError)
            raise SendError(_why(exc)) from None

    def close(self) -> None:
        return None


def _netloc(host: str) -> str:
    """A host as an address's part: an IPv6 address in brackets."""
    return f"[{host}]" if ":" in host else host


def _hostname(host: str | None) -> str:
    """The server's host name as syslog's HOSTNAME (I8): ASCII, a name of other letters in IDNA's form, ``-`` when it
    cannot be one (a frame is ASCII: ``encode("ascii")`` would raise at each send)."""
    if not host:
        return "-"
    try:
        found = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "-"
    return found if 0 < len(found) <= 255 and all(33 <= ord(c) <= 126 for c in found) else "-"


def make_output(
    settings: SiemOutput, secret: str | None, context: ssl.SSLContext | None = None, *, host: str | None = None
) -> Output:
    """The output ``settings`` describe; ``host``: the server's name, for Splunk's envelope and syslog's HOSTNAME."""
    context = context or default_context()
    if settings.kind == "syslog":
        return SyslogOutput(settings, context, host=host)
    return HttpOutput(settings, secret, context, host=host)


def trial_event(clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """What `latet server siem test` and the test button send: an event of type ``test``, which the log never holds."""
    return {name: None for name in FIELDS} | {
        "v": 1, "id": uuid.uuid4().hex, "ts": iso(clock()), "type": "test", "masked": {}, "stopped": 0,
        "reason": "latet-siem-test",
    }  # fmt: skip


def send_test(
    settings: SiemOutput, secret: str | None, context: ssl.SSLContext | None = None, *, host: str | None = None
) -> tuple[bool, str]:
    """Send one test event to an output now, in a worker thread: whether it arrived, and why not. Whatever the output
    raises is said by its type, never raised to the route (I8)."""
    output: Output | None = None
    try:
        output = make_output(settings, secret, context, host=host)
        output.send([trial_event()])
    except SendError as exc:
        return False, str(exc)
    except Exception as exc:  # by its type: its words could quote what it was given
        return False, f"it could not be sent ({type(exc).__name__})"
    finally:
        if output is not None:
            output.close()
    return True, "it arrived"


class SecretElsewhere(Exception):
    """An output's secret was sealed for another output, kind or address (I1): it is set again for this one."""


def secret_of(secrets: Any, output: SiemOutput) -> str | None:
    """The output's secret as it was sealed for where it sends now (``siem_bound``): None when it names none or none
    is kept; ``SecretElsewhere`` when it was set before the output moved, so that it never goes where it was not
    meant to."""
    if output.secret is None:
        return None
    try:
        return secrets.load(output.secret, bound=siem_bound(output))
    except (InvalidTag, ValueError):  # the master key's refusal; ValueError: the tests' secrets in memory
        raise SecretElsewhere from None
