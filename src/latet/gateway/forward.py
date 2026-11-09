"""HTTP forwarding to the upstream provider, streaming the response back unbuffered (an answer restored once it is
whole is read first, no further than Latet reads it decoded)."""

from __future__ import annotations

import asyncio
import hmac
import http.cookiejar
import logging
import ssl
import time
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

import httpx
import truststore
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from latet.engine.pipeline import RequestScope, request_scope
from latet.engine.vault import MaskingError
from latet.gateway import request_policy, request_profiles, request_transport
from latet.gateway.aws import AwsCredentialStore, AwsError, profile_tag
from latet.gateway.body_coverage import BodyProof
from latet.gateway.capture import Capture, Exchange
from latet.gateway.cloud import Signing, shape_of
from latet.gateway.coverage import (
    EmptyBodyProof,
    Envelope,
    EnvelopeProof,
    EnvelopeRefused,
    envelope_of,
    envelope_status,
    inventory,
    preflight_scope,
)
from latet.gateway.headers import RESPONSE_DROP, forwardable, unread_redacted
from latet.gateway.launch_authority import ABSENT, REQUEST_LAUNCH, RequestLaunch
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import (
    REWRITE,
    STOPPED_ANSWER,
    VERIFY,
    Mediator,
    Prepared,
    ResponseTransform,
    protocol_for,
)
from latet.gateway.outcome import (
    Attempted,
    Block,
    Execution,
    Forward,
    NotAttempted,
    Pass,
    PolicyEvidence,
    SendDecision,
    finalize,
    refuse,
    response_plan,
)
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture, agent_of
from latet.gateway.request_audit import (
    RequestAudit,
    SafeDecisionRecord,
    emit_decision,
    method_code,
    reason_code,
    route_code,
)
from latet.gateway.request_policy import current
from latet.gateway.request_transport import SendRefusal
from latet.gateway.response_finalizer import ResponseFinalizer, close_response
from latet.gateway.routes import RouteTable, Upstream, readable_path, wire_path
from latet.gateway.sigv4 import AwsCredentials
from latet.protocols.codec import DecodedTooLarge
from latet.protocols.sse import ResponseError
from latet.server.egress import EgressRefused

log = logging.getLogger(__name__)

_REFUSED = ("ResponseError", "ResponseTooLarge")  # an answer stopped: recorded with the status its client got
# What an upstream answers to send the request elsewhere: these, and any other 3xx that names a Location. Never
# followed, and never passed on as it came: a client that followed it would send its request, unmasked, straight to
# the address it names, past Latet. The client is answered REDIRECTED instead, a 502 naming no address.
REDIRECTS = frozenset({301, 302, 303, 307, 308})
REDIRECTED = "the provider answered with a redirect ({status}), which Latet does not follow"


def redirected(status: int, headers: Mapping[str, str]) -> bool:
    """Whether an upstream's answer sends the request elsewhere: one of ``REDIRECTS``, or any 3xx naming a Location.
    ``headers``: the answer's, read without regard to case (httpx's, websockets')."""
    return status in REDIRECTS or (300 <= status < 400 and "location" in headers)


def history_coverage(decision: SendDecision) -> int:
    """Freeze only the actual HTTP factory result at its capture seam; never revisit its proof or content."""
    if type(decision) is Forward and (type(decision.proof) is BodyProof or type(decision.proof) is EmptyBodyProof):
        return 21
    if type(decision) is Pass and type(decision.evidence) is PolicyEvidence and type(decision.body_proof) is BodyProof:
        return 25
    if (
        type(decision) is Pass
        and type(decision.evidence) is PolicyEvidence
        and decision.body_proof is None
        and type(decision.envelope_proof) is EnvelopeProof
        and decision.envelope.transport == "http"
        and decision.envelope.body == b""
        and decision.payload == b""
    ):
        # A call with no body (a model list) let through by a rule or an exception of its envelope's: every part of it
        # accounted for, as the same call with nothing to name is (a Forward, 21), so the history shows what it passed
        # by (D4 (b)'s "checked only", an exception), never an unknown coverage (the review of Task 3, M-1).
        return 25
    return 28 if type(decision) is Block else 0


def decision_passes(decision: SendDecision) -> tuple[str, ...]:
    """What a decision rests on besides a complete proof, by fixed categories (``request_policy.PASSES``): each built-in
    rule a ``Pass`` names, and ``exception`` when its evidence names a request exception (an envelope's or a body's:
    a request sent under one is always a ``Pass`` that names it). Empty for a ``Forward`` and for a ``Block``; never a
    rule's argument or an exception's id."""
    if type(decision) is not Pass:
        return ()
    categories = request_policy.PASS_CATEGORIES
    out = {categories[rule] for rule in decision.evidence.rules if rule in categories}
    if decision.evidence.exceptions:
        out.add("exception")
    return tuple(sorted(out))


@dataclass(slots=True)
class _Whole:
    """An answer restored once it is whole, and what it is recorded with as it is read."""

    response: httpx.Response
    seen: ResponseCapture
    finalizer: ResponseFinalizer
    headers: list[tuple[str, str]]


# A refusal a stored exception may lift (``request_profiles._excepted``): one an exception added on another instance
# or worker may name, so the settings in force are read again first when PostgreSQL holds newer ones (RC-REC-3).
_MAY_BE_EXCEPTED = frozenset({"envelope-header-unknown", "envelope-query-unknown"})


async def _local_fresh() -> tuple[datetime, bool]:
    return datetime.now(UTC), False


def _used(admission: Any, prepared: Prepared, upstream: Upstream) -> tuple[tuple[tuple[Any, ...], Any], ...]:
    """What a request lets through by a stored exception, each component with the exceptions that may let it through
    and the refusal it would have had without them (``request_policy.granted`` judges which applies): its envelope's
    (``Admission.exceptions``), and its body's, each named by the rules its body was read by (``body_selectors``; its
    project's already chosen as Latet trusts it, so the refusal stands for a binding that changed meanwhile)."""
    named = request_policy.body_exception_ids(prepared.body_rules)
    rows = {row.id: row for row in upstream.envelope_exceptions}
    body = (((rows[one],), EnvelopeRefused("envelope-binding")) for one in named if one in rows)
    return (*admission.exceptions, *body)


def _newly_open(admission: Any, prepared: Prepared) -> EnvelopeRefused | None:
    """D4 (b): a header the open list lets through at a later reading of the request's envelope (``_judged``) that it
    was not prepared with: one an exception let through at its admission, the exception revoked meanwhile. Its value
    was not read as the open list reads it (a destination credential's never is), so it is refused as it was refused
    before the exception, by that refusal. None: each was there at its preparation, which let it through."""
    kept = set(prepared.envelope_open)
    return next((error for name, error in admission.open if name not in kept), None)


def _settings_moved(prepared: Prepared) -> bool:
    """Whether a request's route authority went stale after it was prepared: its route or its rules moved (its settings
    applied again), which is why ``finalize`` refused it, not a part left unaccounted for."""
    authority = prepared.route_authority
    return authority is not None and not authority.valid()


def _unrouted(prepared: Prepared, upstream: Upstream) -> bool:
    """A rule of its body names an exception its route no longer holds: it is never sent under it."""
    rows = {row.id for row in upstream.envelope_exceptions}
    return any(one not in rows for one in request_policy.body_exception_ids(prepared.body_rules))


def _visible(audit: RequestAudit | None, turn: _Turn, sent_under: tuple[Any, ...]) -> bool:
    """Each exception a request is sent under made visible first (M1): its status reserved under the reference its
    identity's audit keeps for it (``RequestAudit.reference_for``), kept on ``turn`` until the request ends. False, with
    what was reserved abandoned, when one cannot be (no reference made or no status reserved, the audit full): the
    request is not sent. Without an audit owner (a test's gateway) there is nothing to reserve."""
    if not sent_under or audit is None:
        return True
    for row in {row.id: row for row in sent_under}.values():
        # The two calls stay back to back, with no await or hand-off to a thread between them: a reference with no
        # status row yet is let go when a reservation of it fails, so another request of the same exception taking
        # its turn in between could reserve under a reference its audit no longer keeps, splitting its status in two
        # (item 10 of the minors after 0.11; the review of Task 4, M-3).
        reference = audit.reference_for(row.id, request_policy.EXCEPTION_SCOPES[row.reason])
        reservation = None if reference is None else audit.reserve_exception(reference)
        if reservation is None:
            for held in turn.reservations:
                audit.abandon(held)
            turn.reservations.clear()
            return False
        turn.reservations.append(reservation)
    return True


def _settle(audit: RequestAudit | None, turn: _Turn, attempts: int | None, answers: int | None) -> None:
    """The exceptions' reservations of a request that ended: counted with what happened at its send, or let go."""
    held, turn.reservations = turn.reservations, []
    if audit is None or not held:
        return
    sent = type(turn.decision) is Pass and attempts is not None and attempts > 0
    status = turn.reply_status if answers else None
    record = SafeDecisionRecord(source=1, outcome=2, coverage=1, attempts=attempts, answered=answers, status=status)
    for reservation in held:
        if not (sent and audit.finalize(reservation, record)):
            audit.abandon(reservation)


class _KeepsNone(http.cookiejar.DefaultCookiePolicy):
    """No cookie an answer sets is kept: a request carries the cookies its agent sent, and only those."""

    def set_ok(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        return False


def cookieless() -> http.cookiejar.CookieJar:
    """The cookie jar of the clients Latet sends with (G2 review C-1). httpx keeps each answer's ``Set-Cookie`` in its
    client's jar and adds it to later requests; Cloudflare sets one on api.openai.com's and chatgpt.com's answers. Kept,
    it would leave with requests their agents never sent it with, and ``request_transport.build``, which refuses a
    request while the client holds a cookie, would refuse every later request until Latet restarts."""
    return http.cookiejar.CookieJar(policy=_KeepsNone())


def client_options(*, trust_env: bool = True) -> dict[str, Any]:
    """What every client Latet sends with is made with (``make_client``), and the tests' recording clients too, so that
    a test of what a client keeps tests Latet's own (G2 re-review N-1): it follows no redirect, keeps no cookie
    (``cookieless``), with its timeouts, and ``trust_env`` as given."""
    return {
        "timeout": httpx.Timeout(connect=20.0, read=600.0, write=120.0, pool=60.0),
        "follow_redirects": False,
        "trust_env": trust_env,
        "cookies": cookieless(),
    }


def make_client(*, trust_env: bool = True) -> httpx.AsyncClient:
    """One pooled client for every upstream, trusting the OS certificate store. It follows no redirect (httpx's default,
    kept on purpose): Latet never goes on to the address an answer names, inside a server's network say, and the agent
    is answered a clear error in place of the redirect, which it would follow with its request unmasked
    (``Forwarder._redirected``). ``trust_env`` False: no proxy and no credential of the environment's (a destination of
    an identity's own, which a server reaches at the addresses it checked, the next tried only when the connection to
    the one before failed: ``request_transport.reaching``). It keeps no cookie (``cookieless``)."""
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not trust_env:
        return httpx.AsyncClient(transport=request_transport.reaching(context), **client_options(trust_env=False))
    return httpx.AsyncClient(verify=context, **client_options(trust_env=trust_env))


def _raw_headers(request: Request) -> list[tuple[str, str]]:
    return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw]


ErrorShape = Literal["anthropic", "bedrock", "openai", "google"]
# The names Google's errors give each status (google.rpc.Code), which its SDKs show.
_GOOGLE_STATUS = {
    400: "INVALID_ARGUMENT",
    404: "NOT_FOUND",
    413: "INVALID_ARGUMENT",
    500: "INTERNAL",
    502: "UNAVAILABLE",
}


def marked(message: str) -> str:
    """``message`` marked as coming from Latet, once: a message that names Latet first is marked already."""
    return message if message.startswith("Latet") else f"Latet: {message}"


def upstream_error(message: str, status: int = 502, *, shape: ErrorShape) -> Response:
    """An error in the shape the client's SDK parses, marked as coming from Latet (`marked`). Never a 401 or
    403: Claude Code takes those for a failed login."""
    text = marked(message)
    if shape == "openai":
        body: dict[str, object] = {"error": {"message": text, "type": "latet_error", "code": "latet_blocked"}}
    elif shape == "google":
        body = {"error": {"code": status, "message": text, "status": _GOOGLE_STATUS.get(status, "UNKNOWN")}}
    else:
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": text}}
        if shape == "bedrock":
            body["message"] = text  # where Bedrock's own errors say it, and what Claude Code shows first
    return JSONResponse(body, status_code=status, headers={"x-should-retry": "false"})


@dataclass(slots=True)
class _Turn:
    """What ``Forwarder.handle`` learns of a request as it is forwarded: what its masking prepared, whether the relay of
    a restored stream lets the conversation's hold go itself, its decision (``outcome``): a refusal (``Block``, also
    of a request refused before its content was read), a ``Forward`` or a ``Pass`` under a built-in rule; and what
    happened at its send (``execution``: not attempted, and why, or attempted, how many times, with the provider's last
    status). The transport sends only from ``decision``, for the arena ``prepared`` is. ``renewer``:
    the task that renews its hold while its answer is awaited (``renewing``), owned by its lease once it holds."""

    prepared: Prepared | None = None
    streams: bool = False
    decision: SendDecision | None = None
    execution: Execution | None = None
    renewer: asyncio.Task[None] | None = None
    route: int = 0
    replies: int = 0
    reply_status: int | None = None
    refusal: int | None = None
    # The status reservations of the request exceptions it is sent under (M1): each made visible before it is sent,
    # settled as it ends (``Forwarder.handle``).
    reservations: list[Any] = field(default_factory=list)

    def refused(self, block: Block) -> Block:
        """``block`` its decision: nothing of it is sent."""
        self.decision, self.execution = block, NotAttempted(block.code)
        return block


# How often a conversation's hold is renewed while its answer is awaited (``renewing``), well within the vault's
# ``HOLD_SECONDS`` (600); and for how long at most, should an answer never end (a relay that never starts).
RENEW_SECONDS = 60.0
# How many times a request is prepared again because the rules moved while it was prepared (``Forwarder.handle``):
# settings that keep moving past that still refuse it, as before.
_REDERIVE = 2
RENEW_CAP = 3600.0


async def renewing(engine: Any, turn: _Turn) -> None:
    """While ``turn``'s answer is awaited, what its conversation's vault holds for it renewed every ``RENEW_SECONDS``
    (``Engine.renew``, on the event loop: a lock and a time): a model silent for longer than the hold before its first
    token (a long reasoning phase) keeps the values its answer restores (the re-review's R-I2); an API key's too. Ends
    once the answer is let go (its lease released: ``Forwarder._answered``, or the stream that owns it), for a turn that
    held nothing, once the engine is gone or its space closed (it holds the engine weakly, as the lease does: a stream
    whose relay never ends keeps no closed space alive, r6), or past ``RENEW_CAP``. Each renewal goes through the turn's
    lease (``ExchangeLease.renew``): a lease released, or handed on and released since, renews nothing."""
    held = weakref.ref(engine)
    del engine
    began = time.monotonic()
    while time.monotonic() - began < RENEW_CAP:
        await asyncio.sleep(RENEW_SECONDS)
        prepared = turn.prepared
        if prepared is None:
            continue  # still being masked
        lease = prepared.lease
        if lease is None or held() is None or not lease.renew():
            return


def error_shape(upstream: Upstream, prepared: Prepared | None, *, method: str = "", path: str = "") -> ErrorShape:
    """The shape this request's client reads: Anthropic's for the Messages API, whatever the route, with
    Bedrock's own field for a Bedrock call; Google's for the Gemini API and for Google's calls on Vertex (a request
    Latet read as another's than Claude's: Claude Code reads Anthropic's)."""
    shape = prepared.shape if prepared is not None else shape_of(method, path)
    if upstream.name == "bedrock" or shape in ("bedrock", "bedrock-count"):
        return "bedrock"
    # A refused envelope has no arena yet. Route syntax chooses its error format only, never send authority.
    protocol = prepared.protocol if prepared is not None else protocol_for(upstream, method, path) if method else None
    if upstream.name == "gemini" or protocol == "gemini":
        return "google"
    if upstream.name == "vertex" and prepared is not None and protocol != "anthropic":
        return "google"
    return "anthropic" if protocol == "anthropic" or upstream.name in ("anthropic", "vertex") else "openai"


_RECORDED = ("anthropic", "chat", "responses", "gemini")  # protocols whose responses the dashboard shows
# How the error of a stopped request ends: where to see it. In the tutorial's sandbox, its own page: `latet
# dashboard` would open the user's own dashboard, where the sandbox's requests are not.
# Requests refused before their content is read: what their client is told (each a ``Block``, nothing of it sent).
_NO_ROUTE = "no upstream for this path"
_UNREAD = "Latet does not read this kind of request, so it does not send it"
_NO_SUCH_ROUTE = "there is no such route"
# A destination of the identity's own the server may not reach: its ``Block`` names no address (its agent is told the
# address's words, which are never logged nor kept).
_NOT_REACHED = "the server may not reach this destination for you"
# A request whose settings changed after it was prepared, which is not taken again (its route changed, or it was
# reviewed under the rules before): refused, its agent told so.
_SETTINGS_MOVED = "the settings changed while this request was prepared: send it again"
# A request whose route the settings removed between two attempts of it (AWS's expired token): its first one left.
_ROUTE_GONE = "the route of this request was removed while it was sent"
_SEE = ". Nothing was sent; run `latet dashboard` to see where it was"
_SEE_IN_TUTORIAL = ". Nothing was sent; see where it was in Latet's pane"


def too_large(error: DecodedTooLarge) -> str:
    """Why a request whose body would decode past a server's bound is stopped (413): the bound, nothing of the body."""
    return f"{error}, the most this server takes (max_request_mb)"


def stopped(reason: str, tutorial: bool = False) -> str:
    """What an agent is told of a request Latet stopped, over HTTP or a WebSocket: Latet named once, why, that
    nothing was sent, and where to see it (in the tutorial's sandbox, its own pane)."""
    # A reason that ends its own sentence keeps its one period: "...its history. Nothing was sent".
    return marked(reason.removesuffix(".") + (_SEE_IN_TUTORIAL if tutorial else _SEE))


_FURTHER = "No further attempt was sent; prior delivery or processing may have occurred"


def _after_an_attempt(text: str) -> str:
    """What ``stopped`` says, for a request refused after an attempt of it reached its provider (AWS's second attempt,
    its first answered ExpiredToken): no further attempt was sent, and the first may have arrived; never that nothing
    was sent."""
    return text.replace("Nothing was sent", _FURTHER)


def _run_cleanup(*steps: Callable[[], object] | None) -> BaseException | None:
    """Attempt every owned step. Ordinary cleanup failures are observations."""
    interrupted: BaseException | None = None
    for step in steps:
        if step is None:
            continue
        try:
            step()
        except Exception as exc:
            log.warning("HTTP cleanup failed: %s", type(exc).__name__)
        except BaseException as exc:
            if interrupted is None:
                interrupted = exc
    return interrupted


async def _finish_response(
    answer: httpx.Response,
    finalizer: ResponseFinalizer,
    error: str | None,
    *steps: Callable[[], object] | None,
) -> None:
    interrupted = _run_cleanup(*steps)
    if interrupted is not None:
        finalizer.stop()
    await close_response(answer, finalizer, error or ("response-cleanup" if interrupted is not None else None))
    if interrupted is not None and error is None:
        raise interrupted


class _OwnedStream(StreamingResponse):
    """A streamed answer that owns, from the moment it is made, its exchange's lease (a restored stream's, handed on by
    ``Forwarder._handle``) and the provider's answer: its relay releases the one and closes the other as it ends; one
    whose body was never iterated (its client gone before the answer's start, so that its relay's ``finally`` never
    runs) releases its lease, ends its renewer and closes the answer in its own ``finally``, before its ASGI call
    returns, never through a garbage-collection finalizer (RC-REC-11 (a), R-78); one cut short mid-way has its relay
    closed there too, so that the relay's own ``finally`` runs at once, as the finalizer would have run it later.
    Nothing is recorded or sent differently."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        started: list[bool],
        lease: ExchangeLease | None,
        answer: httpx.Response,
        transform: ResponseTransform | None,
        status_code: int,
        finalizer: ResponseFinalizer,
        outcome: Outcome | None,
    ) -> None:
        super().__init__(content, status_code=status_code)
        self._finalizer, self._outcome = finalizer, outcome
        self._started, self._lease, self._answer, self._transform = started, lease, answer, transform
        self._abandoned = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        failed = False
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            failed = True
            raise
        finally:
            try:
                if self._started[0]:
                    self._outcome = None
                    body = self.body_iterator
                    if isinstance(body, AsyncGenerator):
                        await body.aclose()
                else:
                    await self.abandon()
            except BaseException:
                if not failed:
                    raise

    async def abandon(self) -> None:
        """Let go of an answer whose body was never iterated, at once: its lease released, its renewer ended, the
        provider's answer closed, the record sealed unfinished. The ASGI call's own end does it for a client gone
        before the answer's start; ``Forwarder.handle``, for a response its call never returned (it failed after the
        response was made, an interruption in its last steps), which no server will ever call."""
        if self._started[0] or self._abandoned:
            return
        self._abandoned = True  # once: a second call has nothing left to let go
        self._finalizer.seal(self._outcome, complete=False)
        self._outcome = None
        await _finish_response(
            self._answer,
            self._finalizer,
            "response-unstarted",
            self._transform.close if self._transform is not None else None,
            self._lease.release if self._lease is not None else None,
            self._lease.cancel_renewer if self._lease is not None else None,
        )


class Forwarder:
    def __init__(
        self,
        routes: RouteTable,
        capture: Capture,
        client: httpx.AsyncClient,
        mediator: Mediator | None = None,
        recorder: Recorder | None = None,
        aws: AwsCredentialStore | None = None,
        token: str | None = None,
        tutorial: bool = False,
        pin: Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]] | None = None,
        personal: Callable[[], set[str]] | None = None,
        direct: httpx.AsyncClient | None = None,
        request_audit: RequestAudit | None = None,
        exception_fresh: Callable[[], Awaitable[tuple[datetime, bool]]] | None = None,
    ) -> None:
        self.request_audit = request_audit  # borrowed SAME runtime owner; never Recorder authority
        # RC-REC-3: the decision's clock (D3: PostgreSQL's on a server) and whether the settings in force were read
        # again because PostgreSQL's counts were ahead of them; a computer's own, never behind.
        self.exception_fresh = exception_fresh or _local_fresh
        self.routes = routes
        self.capture = capture
        self.client = client
        self.tutorial = tutorial  # the tutorial's sandbox: a stopped request's error points to its own pane
        self.mediator = mediator
        self.recorder = recorder if recorder is not None and recorder.enabled else None
        self.aws = aws  # credentials for the Bedrock requests Latet signs
        self.token = token  # the key of a signing route is made from it
        # On a Latet server: where a destination of the identity's own goes (the address checked when the server
        # connects), the names of those destinations, and the client they are reached with (no proxy, no redirect).
        self.pin = pin
        self.personal: Callable[[], set[str]] = personal or set
        self.direct = direct

    async def _judged(
        self,
        turn: _Turn,
        upstream: Upstream,
        prepared: Prepared,
        envelope: Any,
        used: tuple[tuple[tuple[Any, ...], Any], ...],
        envelope_count: int,
        method: str,
        wire: str,
        body: bytes | None = None,
        *,
        attempted: bool = False,
    ) -> tuple[tuple[Any, ...], int] | Response:
        """What a request is sent under, judged by a reading made now (I-1, R-1): the exceptions each of its components
        takes (``request_policy.granted``) by the clock of this reading, so that one that ended meanwhile is refused
        naming it, settings PostgreSQL holds newer than those it was admitted under read first (RC-REC-3: an exception
        revoked on another instance meanwhile, its announcement lost or not), and its route and envelope then taken
        again under them: a revoked exception refuses it as it was refused before the exception, and any other change
        of the rules refuses it at its send (its route authority is stale). A project's applies where Latet trusts
        that project alone: the folders its preparation compared, in its worker (``Prepared.excepted``), never here
        on the event loop (I-6). Its exceptions and how many are its envelope's, or the refusal answered; an envelope
        refusal goes to the history with ``body``'s size when ``body`` is given (``_envelope_refused``). ``attempted``:
        an attempt of it reached its provider already, which a refusal's words say (``_after_an_attempt``)."""
        try:
            now, refreshed = await self.exception_fresh()
        except Exception as exc:
            return self._state_unread(turn, upstream, prepared, exc, attempted=attempted)
        if refreshed:
            matched = self.routes.match(wire)
            if matched is None or matched.name != upstream.name:
                turn.refused(refuse("http", None, "no-route", _NO_ROUTE, 404))
                if attempted:  # its first attempt reached its provider: said, in its provider's shape (item 7)
                    text = _after_an_attempt(stopped(_ROUTE_GONE, self.tutorial))
                    return upstream_error(text, 404, shape=error_shape(upstream, prepared))
                return JSONResponse({"error": _NO_ROUTE}, status_code=404)
            try:
                again = request_profiles.admit(inventory(envelope), matched)
            except EnvelopeRefused as error:
                return self._envelope_refused(
                    turn, matched, prepared, error, method, wire, body=body, attempted=attempted
                )
            if (opened := _newly_open(again, prepared)) is not None:
                return self._envelope_refused(
                    turn, matched, prepared, opened, method, wire, body=body, attempted=attempted
                )
            if _unrouted(prepared, matched):
                binding = EnvelopeRefused("envelope-binding")
                return self._envelope_refused(
                    turn, matched, prepared, binding, method, wire, body=body, attempted=attempted
                )
            used, envelope_count = _used(again, prepared, matched), len(again.exceptions)
        sent_under, refusal = request_policy.granted(used, now, trusted=prepared.excepted)
        if refusal is not None:
            return self._envelope_refused(
                turn, upstream, prepared, refusal, method, wire, body=body, attempted=attempted
            )
        return sent_under, envelope_count

    async def _judged_again(
        self,
        turn: _Turn,
        upstream: Upstream,
        prepared: Prepared,
        envelope: Any,
        used: tuple[tuple[tuple[Any, ...], Any], ...],
        sent_under: tuple[Any, ...],
        method: str,
        wire: str,
        body: bytes,
        *,
        sent: bool = False,
    ) -> Response | None:
        """R-1: a request decided under exceptions judged once more (``_judged``) after a wait before it leaves. None
        when the same exceptions still let it through; the refusal answered otherwise (one ended or revoked, or
        another one would now apply: what was decided, and made visible, is what it is sent under or nothing), in the
        history with ``body``'s size. ``sent``: before a second attempt, its first having left under them: its decision
        and what happened at its send stay as they were, so that the use is counted as it went (M1), no further attempt
        is made, the record its first attempt makes is its record, and its agent is told that the first may have arrived
        (G2 review I-2)."""
        if not sent_under:
            return None
        decision, execution = turn.decision, turn.execution
        kept = None if sent else body
        judged = await self._judged(turn, upstream, prepared, envelope, used, 0, method, wire, kept, attempted=sent)
        if not isinstance(judged, Response) and [row.id for row in judged[0]] != [row.id for row in sent_under]:
            binding = EnvelopeRefused("envelope-binding")
            judged = self._envelope_refused(turn, upstream, prepared, binding, method, wire, body=kept, attempted=sent)
        if not isinstance(judged, Response):
            return None
        if sent:
            turn.decision, turn.execution = decision, execution
        return judged

    def _envelope_refused(
        self,
        turn: _Turn,
        upstream: Upstream,
        prepared: Prepared | None,
        error: EnvelopeRefused,
        method: str | None = None,
        wire: str | None = None,
        *,
        body: bytes | None = None,
        attempted: bool = False,
    ) -> Response:
        """A request refused for its envelope, nothing (more) sent: D4's name is the agent's alone; the shared decision,
        the log and the history keep its category. ``body``: the request's, whose size its history record keeps
        (``_envelope_record``); None writes no record (a second attempt, whose first one's record stands).
        ``attempted``: an attempt of it reached its provider already, which its words say (``_after_an_attempt``)."""
        reason = f"request envelope refused ({error.code})"
        turn.refused(refuse("http", upstream.name, error.code, reason, error.status))
        log.warning("blocked HTTP envelope: %s", error.code)
        if body is not None and method is not None and wire is not None:
            self._envelope_record(upstream, error, method, wire, body)
        shape = (
            error_shape(upstream, prepared, method=method, path=readable_path(wire))
            if method is not None and wire is not None
            else error_shape(upstream, prepared)
        )
        text = stopped(error.agent_message(), self.tutorial)
        return upstream_error(_after_an_attempt(text) if attempted else text, error.status, shape=shape)

    def _envelope_record(self, upstream: Upstream, error: EnvelopeRefused, method: str, wire: str, body: bytes) -> None:
        """A request its envelope's admission refused, in the history as any refused request is (D4, R-79; P2-8a): its
        category's code and the route template its profile names (never the request's own path or query), no header
        kept (the refused one's value never), its body's size alone. Nothing of it was sent."""
        if self.recorder is None:
            return
        template = request_profiles.refused_template(upstream, method, wire)
        outcome = Outcome({}, upstream.name, template, "http", time.time())
        outcome.history_code = 28  # a refusal, as ``history_coverage`` codes a Block
        reason = f"request envelope refused ({error.code})"
        self._refused(outcome, upstream, method, readable_path(wire), body, reason, error.status, error.code)

    def _state_unread(
        self, turn: _Turn, upstream: Upstream, prepared: Prepared | None, exc: Exception, *, attempted: bool = False
    ) -> Response:
        """The state a stored exception is judged by could not be read (RC-REC-3): refused, never sent on its word.
        ``attempted``: an attempt of it reached its provider already, which its words say (``_after_an_attempt``)."""
        log.warning("not sent %s: settings unread (%s)", upstream.name, type(exc).__name__)
        reason = "request exception state unavailable"
        turn.refused(refuse("http", upstream.name, "envelope-state-unavailable", reason, 503))
        text = stopped(reason, self.tutorial)
        return upstream_error(
            _after_an_attempt(text) if attempted else text, 503, shape=error_shape(upstream, prepared)
        )

    def _may_sign(self, signing: Signing) -> bool:
        """A session's route, or a profile's whose key this Latet made (another program cannot)."""
        if self.aws is None:
            return False
        if signing.profile is None:
            return True
        return bool(self.token) and hmac.compare_digest(
            signing.key or "", profile_tag(self.token or "", signing.profile)
        )

    async def _credentials(self, signing: Signing) -> AwsCredentials:
        assert self.aws is not None
        if signing.profile is not None:
            return await self.aws.profile(signing.profile)
        return self.aws.session(signing.session or "")

    async def _route(
        self, upstream: Upstream, url: str, request: Request, path: str, outcome: Outcome | None
    ) -> tuple[str, str | None, dict[str, Any] | None] | Response:
        """Where a request to ``upstream`` goes: a destination of the identity's own (on a server) to the address
        checked now, with the Host header and TLS name to keep (``pin``); any other to ``url`` as it is, with neither.
        Or the answer that stops the request, when the address is one the server may not reach for the identity."""
        if self.pin is None or not (upstream.own or upstream.name in self.personal()):
            return url, None, None
        try:
            return await self.pin(url)
        except EgressRefused as error:  # its words name the identity's own destination: said to its agent, never logged
            log.warning("not sent %s %s: egress refused", request.method, path)
            self._record(outcome, status=400, error="egress-refused")
            return upstream_error(str(error) + ". Nothing was sent", 400, shape=error_shape(upstream, None))

    async def _prepared(
        self,
        upstream: Upstream,
        request: Request,
        read: str,
        lowered: dict[str, str],
        body: bytes,
        path: str,
        outcome: Outcome | None,
        turn: _Turn,
        checked: tuple[str, ...] = (),
        envelope_occurrences: int = 0,
        query: str = "",
        launch: RequestLaunch = ABSENT,
        body_grants: Any = None,
        envelope_open: tuple[tuple[str, EnvelopeRefused], ...] = (),
        envelope_family: str | None = None,
    ) -> Prepared | Response:
        """The request masked (``Mediator.prepare``); or the answer that stops it, when the vault is at its limit or no
        fake is free and ``prepare`` lets it through, or when its body would decode past a server's ``max_request_mb``:
        never sent unmasked, nor decoded further, and refused (``turn.decision``). A request stopped so is recorded as
        one Latet stopped, with its reason (``_refused``). ``envelope_open``: what D4 (b)'s open list let through at its
        envelope (its profile's ``envelope_family``), which its preparation refuses unless Latet only checks it."""
        assert self.mediator is not None
        try:
            # Given up while it masks: what the masking holds is released as the executor ends (RC-REC-11 (b)).
            return await self.mediator.run_held(
                self.mediator.prepare_http,
                upstream,
                request.method,
                read,
                lowered,
                body,
                checked,
                envelope_occurrences,
                query,
                launch=launch,  # always a launch decision: never the preparation seam (G2 review M-3)
                **({"body_grants": body_grants} if body_grants is not None else {}),
                envelope_open=envelope_open,
                envelope_family=envelope_family,
            )
        except DecodedTooLarge as error:  # a few KiB that would inflate past the limit: stopped (its words, the limit)
            log.warning("stopped %s %s: %s", request.method, path, type(error).__name__)
            reason = too_large(error)
            turn.refused(refuse("http", upstream.name, "too-large", reason, 413))
            if outcome is not None:
                outcome.history_code = 28
            self._refused(outcome, upstream, request.method, read, body, reason, 413, "too-large")
            return upstream_error(stopped(reason, self.tutorial), 413, shape=error_shape(upstream, None))
        except MaskingError as error:  # VaultFull, or no fake free: stopped, and said so (its words name no value)
            log.warning("stopped %s %s: %s", request.method, path, type(error).__name__)
            reason = f"masking failed: {error}"
            turn.refused(refuse("http", upstream.name, "masking-failed", reason, 400))
            if outcome is not None:
                outcome.history_code = 28
            self._refused(outcome, upstream, request.method, read, body, reason, 400, "masking-failed")
            return upstream_error(stopped(reason, self.tutorial), 400, shape=error_shape(upstream, None))

    def _refused(
        self,
        outcome: Outcome | None,
        upstream: Upstream,
        method: str,
        read: str,
        body: bytes,
        reason: str,
        status: int,
        code: str,
    ) -> None:
        """A request ``prepare`` did not give back, recorded as one Latet stopped (``reason``, never sent; ``code``, its
        refusal's, is what the history's summary says of it): it was shown as a request sent unread, with an error. Its
        status alone: an error's code would read as something else (a signed request's as its AWS login)."""
        if outcome is not None:
            outcome.prepared = Prepared(VERIFY, protocol_for(upstream, method, read), b"", block_reason=reason)
            outcome.body_bytes = len(body)
            outcome.block_code = code
        self._record(outcome, status=status)

    async def _sent(
        self,
        turn: _Turn,
        envelope: Envelope,
        attempt: int,
        target: tuple[str, str, list[tuple[str, str]]],
        signer: tuple[AwsCredentials, str] | None,
        route: tuple[str | None, dict[str, Any] | None, httpx.AsyncClient],
    ) -> httpx.Response | SendRefusal:
        """Attempt ``attempt`` of the exchange's decision through the guarded transport (``request_transport``): the
        decision ``turn`` holds, for its own arena and ``envelope``, authorized under the rules in force, then built
        (``target``: the method, the URL and the client's headers; ``signer`` when Latet signs; ``route``: the Host
        header and TLS's name of a destination of the identity's own, and the client) and sent as built; or why it was
        not."""
        method, url, headers = target
        host, extensions, client = route
        credentials: request_transport.Credentials = "aws" if signer is not None else "client"
        if turn.prepared is not None and not turn.prepared.opaque_current():
            return SendRefusal("opaque-expired")
        authorized = request_transport.validate_send(
            turn.decision,
            turn.prepared,
            envelope,
            current(),
            attempt,
            credentials=credentials,
            attempted=lambda: setattr(turn, "execution", Attempted(attempt, None)),
        )
        if isinstance(authorized, SendRefusal):
            return authorized
        built = request_transport.build(authorized, client, method, url, headers, signer, host, extensions)
        if isinstance(built, SendRefusal):
            return built
        return await request_transport.send(client, built)

    def _not_sent(
        self,
        turn: _Turn,
        refusal: SendRefusal,
        method: str,
        path: str,
        upstream: Upstream,
        outcome: Outcome | None,
        exchange: Exchange | None,
        *,
        received: int,
    ) -> Response:
        """A decision the transport did not send (``SendRefusal``: it changed, or was never one it may send): nothing
        more of the request is sent, and its client is told so (500: Latet's own failure). No attempt is recorded for a
        first attempt refused; a retry refused keeps the first attempt's.

        A stale decision its route authority refused (``settings_moved``: its rules or its route moved after its
        decision, its settings applied again meanwhile) is refused as a request whose settings changed while it was
        prepared is (``settings-changed``, 400: send it again), never as Latet's own failure (after 0.11, item 3); any
        other stale decision (its launch no longer current, its arena changed, its opaque evidence) keeps its 500, even
        while settings are applied (item 23's fix round, M2). Nothing of it sent, its history row is the row of that
        refusal at its decision: no error, no passes, no header kept, its size as it came (``received``)."""
        turn.refusal = reason_code(refusal.code)  # numeric category only, never raw reason
        moved = refusal.code == "stale" and refusal.settings_moved
        if moved or refusal.code in {"coverage-limit", "opaque-expired"}:
            if moved:
                code, reason, status = "settings-changed", _SETTINGS_MOVED, 400
                turn.refusal = reason_code(code)
            elif refusal.code == "coverage-limit":
                code, reason = refusal.code, "request envelope refused (coverage-limit)"
                status = envelope_status(refusal.code)
            else:
                code, reason, status = "evidence", "opaque evidence expired; send the request again", 400
            block = refuse("http", upstream.name, code, reason, status)
            attempted = isinstance(turn.execution, Attempted)
            if attempted:
                turn.decision = block  # the earlier AWS attempt remains recorded, with its original status
            else:
                turn.refused(block)
            if outcome is not None:
                outcome.history_code = 28
                if not attempted:  # stopped by its code, nothing sent; a retry refused keeps its first attempt's record
                    outcome.block_code, outcome.forwarded = code, None
            said = code if moved else refusal.code  # what the log and the record name: never "stale" for moved settings
            log.warning("blocked HTTP request: %s", said)
            error: str | None = said
            if moved and not attempted and outcome is not None:
                # Recorded as a request refused at its decision because its settings changed is (the fix round's M1):
                # no error, no passes of the decision it was not sent under, no header kept, its size as it came.
                outcome.passes, outcome.request_headers, outcome.body_bytes = (), [], received
                error = None
            self._record(outcome, status=status, error=error)
            text = stopped(reason, self.tutorial)
            if attempted:
                text = _after_an_attempt(text)
            return upstream_error(text, status, shape=error_shape(upstream, turn.prepared))
        log.warning("not sent HTTP request: %s", refusal.code)
        if turn.execution is None:
            turn.execution = NotAttempted(refusal.code)
            if outcome is not None:
                outcome.forwarded = None  # never sent: Latet added no time before a sending that did not happen
        if exchange:
            exchange.response_end(error="not-sendable")
        self._record(outcome, status=500, error="not-sendable")
        text = stopped(f"Latet could not send this request as it was decided ({refusal.code})", self.tutorial)
        if isinstance(turn.execution, Attempted):
            text = _after_an_attempt(text)
        return upstream_error(text, 500, shape=error_shape(upstream, turn.prepared))

    async def handle(self, request: Request) -> Response:
        """``request`` forwarded (``_handled``). A forwarder with no launch ingress before it (neither the computer's
        launch dispatch nor a server's gate, which hand it a launch and the request's selection) makes the selection
        itself, as the WebSocket relay does, so that the request's missing launch is decided as ABSENT, a claim, and
        never reaches the preparation seam that a claimed project loosens (G2 review M-3)."""
        owned = request_scope.set(RequestScope()) if request_scope.get() is None else None
        try:
            return await self._handled(request)
        finally:
            if owned is not None:
                request_scope.reset(owned)

    async def _handled(self, request: Request) -> Response:
        """``request`` forwarded (``_handle``); and what a conversation's vault held in memory for its answer
        (``Engine.hold``: an API key's) let go however the request ends: by the restore of its answer as it is streamed
        (the relay lets it go as it ends), or here, for an answer restored whole, refused, never sent or never
        answered."""
        turn = _Turn()
        renewer = asyncio.create_task(renewing(self.mediator.engine, turn)) if self.mediator is not None else None
        turn.renewer = renewer
        failed = False
        response: Response | None = None
        try:
            response = await self._handle(request, turn)
            return response
        except BaseException:
            failed = True
            raise
        finally:
            try:
                execution = turn.execution
                attempts = (
                    execution.attempts
                    if type(execution) is Attempted
                    else 0
                    if type(execution) is NotAttempted
                    else None
                )
                answers = (
                    turn.replies if type(execution) is Attempted else 0 if type(execution) is NotAttempted else None
                )
                emit_decision(
                    self.request_audit,
                    turn.decision,
                    source=1,
                    method=method_code(request.method),
                    route=turn.route,
                    attempts=attempts,
                    answered=answers,
                    status=turn.reply_status if answers else None,
                    reason=turn.refusal,
                )
                _settle(self.request_audit, turn, attempts, answers)
            except BaseException:
                if not failed:
                    failed = True
                    raise
            finally:
                # A streamed answer owns its hold and the provider's answer from the moment it is made; one this call
                # never returned (it failed after the answer was made) is never called by a server, and is let go here,
                # as an answer restored whole is.
                unserved = failed and turn.streams
                interrupted = _run_cleanup(
                    turn.prepared.close_receipt if turn.prepared is not None else None,
                    partial(self._answered, turn.prepared) if not turn.streams or unserved else None,
                    renewer.cancel if (not turn.streams or unserved) and renewer is not None else None,
                )
                if unserved and isinstance(response, _OwnedStream):
                    try:
                        await response.abandon()
                    except Exception as exc:
                        log.warning("HTTP cleanup failed: %s", type(exc).__name__)
                if interrupted is not None and not failed:
                    raise interrupted

    async def _handle(self, request: Request, turn: _Turn) -> Response:
        from latet.gateway.request_policy import LocalAuthority

        if self.mediator is None:
            reason = "request protection is unavailable"
            turn.refused(refuse("http", None, "proof-incomplete", reason, 400))
            return upstream_error(stopped(reason, self.tutorial), 400, shape="openai")
        try:
            preflight_scope(request.scope)
        except EnvelopeRefused as error:
            reason = f"request envelope refused ({error.code})"
            upstream = self.routes.match(request.scope.get("path", ""))
            turn.route = route_code(upstream)
            turn.refused(refuse("http", upstream.name if upstream else None, error.code, reason, error.status))
            log.warning("blocked HTTP envelope: %s", error.code)
            return upstream_error(
                stopped(error.agent_message(), self.tutorial),
                error.status,
                shape=error_shape(upstream, None) if upstream else "openai",
            )
        wire = wire_path(request.scope)
        upstream = self.routes.match(wire)
        turn.route = route_code(upstream)
        if upstream is None:
            turn.refused(refuse("http", None, "no-route", _NO_ROUTE, 404))
            return JSONResponse({"error": _NO_ROUTE}, status_code=404)
        launch = request.scope.pop(REQUEST_LAUNCH, None)  # one launch authority on both machines (Revision 9)
        if launch is None:  # no launch ingress before it: decided as the relay decides it, a claim (G2 review M-3)
            launch = ABSENT
        if type(launch) is not RequestLaunch:
            reason = "request launch authority refused"
            turn.refused(refuse("http", upstream.name, "proof-incomplete", reason, 400))
            return upstream_error(stopped(reason, self.tutorial), 400, shape=error_shape(upstream, None))
        authority = LocalAuthority.capture(self.mediator.engine, upstream, self.routes, wire)

        gathered = bytearray()
        async for chunk in request.stream():
            if len(gathered) + len(chunk) > self.mediator.body_limit:
                reason = too_large(DecodedTooLarge(self.mediator.body_limit))
                turn.refused(refuse("http", upstream.name, "too-large", reason, 413))
                return upstream_error(stopped(reason, self.tutorial), 413, shape=error_shape(upstream, None))
            gathered.extend(chunk)
        body = bytes(gathered)
        envelope = envelope_of(request.scope, "http", upstream.name, body)
        # RC-REC-3: a request that may be decided by a stored exception (one its envelope's admission consulted, one its
        # profile's body selectors could, or a component no profile names, which a new exception may now name: the open
        # list's, D4 (b), as a refused one's, since an exception applies first) reads the settings' counts and the
        # decision's clock first, in one statement; settings behind what PostgreSQL holds are read again before
        # deciding, so that an exception revoked or added on another instance or worker counts at once. A computer's
        # own settings are always the ones in force: its clock alone.
        for again in (False, True):
            refused_by: EnvelopeRefused | None = None
            try:
                admission = request_profiles.admit(inventory(envelope), upstream)
            except EnvelopeRefused as error:
                admission, refused_by = None, error
            if refused_by is not None or admission is None:
                may_except = refused_by is not None and refused_by.code in _MAY_BE_EXCEPTED
            else:
                may_except = (
                    bool(admission.exceptions)
                    or admission.profile.id in dict(upstream.envelope_body_grants)
                    or any(error.code in _MAY_BE_EXCEPTED for _, error in admission.open)
                )
            if not may_except or again:
                break
            try:
                _, refreshed = await self.exception_fresh()
            except Exception as exc:
                return self._state_unread(turn, upstream, None, exc)
            if not refreshed:
                break
            # Read again: the request is decided by the settings in force now, on its route as they make it.
            matched = self.routes.match(wire)
            if matched is None or matched.name != upstream.name:
                turn.refused(refuse("http", None, "no-route", _NO_ROUTE, 404))
                return JSONResponse({"error": _NO_ROUTE}, status_code=404)
            upstream = matched
            authority = LocalAuthority.capture(self.mediator.engine, upstream, self.routes, wire)
        if refused_by is not None or admission is None:
            error = refused_by if refused_by is not None else EnvelopeRefused("envelope-binding")
            return self._envelope_refused(turn, upstream, None, error, request.method, wire, body=body)

        query = envelope.query.decode("ascii")
        resolved = upstream.resolve(request.method, wire, query)
        if resolved is None:  # a cloud's call Latet cannot read (Bedrock's Converse API, another publisher)
            turn.refused(refuse("http", upstream.name, "unread", _UNREAD, 404))
            return upstream_error(_UNREAD, 404, shape=error_shape(upstream, None))
        signing: Signing | None = resolved.signing
        if signing is not None and not self._may_sign(signing):
            turn.refused(refuse("http", upstream.name, "no-signing-route", _NO_SUCH_ROUTE, 404))
            return upstream_error(_NO_SUCH_ROUTE, 404, shape="bedrock")
        url = resolved.url
        path = resolved.path or request.url.path  # what records and logs show: never a signing key
        if admission is not None:
            path = admission.profile.template  # no request-supplied resource/route token in safe records or logs
        incoming = _raw_headers(request)  # lowering is now after every raw occurrence was admitted
        lowered = {k.lower(): v for k, v in incoming}
        started = time.time()
        outcome = Outcome(lowered, upstream.name, path, "http", started) if self.recorder else None
        if outcome is not None:
            outcome.host = resolved.host
            if signing is not None:
                outcome.signed = f"aws-profile:{signing.profile}" if signing.profile else "aws-session"

        read = readable_path(wire)  # what a server would make of it: `complet%69ons` is `completions`
        if self.mediator.engine.stranded:
            # Settings that raised midway, and those that ran before raising as well as they were put back: what runs
            # is a mix of both, and nothing is prepared under it, nor sent, until settings are applied whole (minors
            # after 0.11, item 23's fix round, I1). Refused as settings that changed, its agent told to send it again.
            log.warning("blocked %s %s: %s", request.method, path, _SETTINGS_MOVED)
            turn.refused(refuse("http", upstream.name, "settings-changed", _SETTINGS_MOVED, 400))
            if outcome is not None:
                outcome.history_code = 28
            self._refused(outcome, upstream, request.method, read, body, _SETTINGS_MOVED, 400, "settings-changed")
            return upstream_error(stopped(_SETTINGS_MOVED, self.tutorial), 400, shape=error_shape(upstream, None))
        attempt = 0
        outlasted = False  # an apply under way outlasted the wait for it: refused as it is
        while True:
            masked = await self._prepared(
                upstream,
                request,
                read,
                lowered,
                body,
                path,
                outcome,
                turn,
                admission.checked if admission is not None else (),
                len(admission.inventory.entries) if admission is not None else 0,
                query,
                launch,
                dict(upstream.envelope_body_grants).get(admission.profile.id) if admission is not None else None,
                admission.open if admission is not None else (),
                admission.profile.family if admission is not None else None,
            )
            if isinstance(masked, Response):
                return masked
            if masked.open_refusal is not None:
                # D4 (b): a request Latet rewrites, with what the open list alone let through at its envelope: refused
                # as its admission refused it before the open list, nothing of it masked or learnt (its preparation
                # stopped before it read a text), recorded as such a refusal is.
                turn.prepared = masked
                return self._envelope_refused(
                    turn, upstream, masked, masked.open_refusal, request.method, wire, body=body
                )
            # Never refused only because the rules moved while it was prepared (on a server, a settings change another
            # instance announced, applied meanwhile): its authority is taken again under the rules now in force, on the
            # same route, and it is prepared again by them; stricter rules may refuse it, looser ones still ask what
            # they ask (the reviewers' approvals below). A route changed meanwhile keeps its refusal. Settings still
            # being applied are waited for first (``request_policy.settled``), the preparation in hand kept meanwhile,
            # so that it is never taken again while they are half applied (item 23 of the minors after 0.11). Past the
            # wait's bound, or with the mark held for settings that could not be put back, no authority taken now
            # could pass: it is refused as it is, at once, because its settings changed, neither prepared again nor
            # read by a reviewer (its fix round, M3).
            if attempt >= _REDERIVE or authority.valid() or not authority.rederivable():
                break
            await request_policy.settled(self.mediator.engine)
            if self.mediator.engine.applying:
                outlasted = True
                break
            attempt += 1
            self._answered(masked)  # the first preparation's hold goes; the next one holds its own
            authority = LocalAuthority.capture(self.mediator.engine, upstream, self.routes, wire)
        prepared = turn.prepared = masked
        if prepared.protected_unavailable:
            reason = "request launch authority unavailable"
            turn.refused(refuse("http", upstream.name, "proof-incomplete", reason, 503))
            return upstream_error(stopped(reason, self.tutorial), 503, shape=error_shape(upstream, prepared))
        prepared.route_authority = authority
        if prepared.lease is not None and turn.renewer is not None:
            prepared.lease.own_renewer(turn.renewer)  # it renews while the lease holds, and ends with it
        if prepared.reviews and not prepared.block_reason and not outlasted:  # the local reviewer reads it first
            prepared = await self.mediator.review_prepared(prepared)
        if not prepared.block_reason and not outlasted:  # then you, when the Live Reviewer is on
            agent = agent_of(lowered, request.url.path)[0]
            prepared = await self.mediator.live_prepared(prepared, agent, request.is_disconnected)
        # Its decision, once: nothing changes its arena from here (a change made later means it sends nothing).
        assert admission is not None
        # The exceptions it is sent under, its envelope's and its body's (``body_selectors``), judged now its project is
        # known and nothing will change it: a project's only where Latet trusts that project (RC-REC-7, D7), each
        # within its lifetime by the decision's clock (D3, PostgreSQL's on a server).
        used = _used(admission, prepared, upstream)
        envelope_count = len(admission.exceptions)
        sent_under: tuple[Any, ...] = ()
        if not prepared.block_reason and _unrouted(prepared, upstream):
            binding = EnvelopeRefused("envelope-binding")
            return self._envelope_refused(turn, upstream, prepared, binding, request.method, wire, body=body)
        if used and not prepared.block_reason:
            # Judged now, after its preparation and its reviews (the Live Reviewer waits until you answer), and again
            # after each wait before it leaves (``_judged``).
            judged = await self._judged(
                turn, upstream, prepared, envelope, used, envelope_count, request.method, wire, body
            )
            if isinstance(judged, Response):
                return judged
            sent_under, envelope_count = judged
        proof = EnvelopeProof(
            envelope,
            len(admission.inventory.entries),
            admission.profile.id,
            admission.treatments,
            admission.headers,
            resolved.url,
            admission.checked,
            admission.removed,
            admission.inventory.limits.header_occurrences,
            admission.inventory.limits.header_bytes,
            tuple(dict.fromkeys(row.id for row in sent_under[:envelope_count])),
            admission.rules,  # D4 (b): "checked only" when the open list let a header or a beta value through
        )
        decision = turn.decision = finalize(
            prepared,
            envelope,
            current(),
            response_plan(prepared),
            envelope_proof=proof,
        )
        if isinstance(decision, Block) and decision.code == "proof-incomplete" and _settings_moved(prepared):
            # Its route or rules moved after it was prepared (and reviewed): the settings changed, as its agent, the
            # log, the history and the audit say, never a fault of coverage (G2 review M-2; minors after 0.11, Q-M1).
            decision = turn.decision = refuse("http", upstream.name, "settings-changed", _SETTINGS_MOVED, 400)
        if outcome is not None:
            outcome.history_code = history_coverage(decision)
            outcome.passes = decision_passes(decision)
            outcome.profile = admission.profile.id
            outcome.removed_headers = admission.removed
            outcome.prepared = prepared
            outcome.body_bytes = len(body)
        if isinstance(decision, Block):
            turn.refused(decision)
            if outcome is not None:
                outcome.block_code = decision.code
            if decision.code == "envelope-sensitive":
                log.warning("blocked HTTP envelope: envelope-sensitive")
            else:
                log.warning("blocked %s %s: %s", request.method, path, decision.reason)
            self._record(outcome, status=decision.status)
            text = stopped(decision.reason, self.tutorial)
            return upstream_error(text, decision.status, shape=error_shape(upstream, prepared))
        if not _visible(self.request_audit, turn, sent_under):
            # M1: an exception in use is visible while it is used; unseen, it is not used (503, nothing sent).
            reason = "request exception status could not be kept visible"
            log.warning("not sent %s: %s", path, reason)
            turn.refused(refuse("http", upstream.name, "envelope-state-unavailable", reason, 503))
            return upstream_error(stopped(reason, self.tutorial), 503, shape=error_shape(upstream, prepared))
        received = len(body)  # its size as it came, which a refusal's history row keeps
        body = prepared.body

        # DNS/pin/connect only after every raw occurrence and checked envelope text was admitted.
        route = await self._route(upstream, url, request, path, None)
        if isinstance(route, Response):
            turn.refused(refuse("http", upstream.name, "egress-refused", _NOT_REACHED, 400))
            return route
        url, host_header, extensions = route
        # R-1: what it is sent under judged again after each wait before it leaves (its address looked up and pinned,
        # its AWS credentials read, AWS's second attempt), by the same reading: one that ended or was revoked meanwhile
        # sends nothing more.
        again = await self._judged_again(
            turn, upstream, prepared, envelope, used, sent_under, request.method, wire, body
        )
        if again is not None:
            return again
        client = self.direct if host_header is not None and self.direct is not None else self.client

        signer: tuple[AwsCredentials, str] | None = None
        if signing is not None:  # read before anything is built: a login that ended sends nothing
            try:
                signer = (await self._credentials(signing), signing.region)
            except AwsError as error:
                log.warning("not signed %s %s: %s", request.method, path, error.code)
                # Its decision now: a refusal, nothing sent (what it was decided before cannot be sent any more).
                turn.refused(refuse("http", upstream.name, "credentials", str(error), 400))
                if outcome is not None:
                    outcome.history_code = 28
                    outcome.block_code = "credentials"
                if prepared is not None:  # the dashboard says why, in plain words
                    prepared.block_reason = str(error)
                self._record(outcome, status=400, error=error.code)
                return upstream_error(str(error) + ". Nothing was sent", 400, shape="bedrock")
            again = await self._judged_again(
                turn, upstream, prepared, envelope, used, sent_under, request.method, wire, body
            )
            if again is not None:
                return again
        headers = list(admission.headers)
        if signer is not None:  # the client's own authorization goes: Latet signs instead
            headers = [
                (k, v) for k, v in headers if k.lower() != "authorization" and not k.lower().startswith("x-amz-")
            ]
        # What records keep (the history, a capture): a credential sent unread never in clear, whatever its name (I-3).
        kept = unread_redacted(headers, admission.unread)
        if outcome is not None:
            outcome.request_headers = kept
            outcome.body_bytes = len(body)
        exchange = self.capture.exchange("http", upstream.name, request.method, url)
        if exchange:
            exchange.request(kept, body, lowered.get("content-encoding"))

        # S1, and S2 for AWS's expired token: the decision's own payload, authorized for each attempt, built, then sent
        # as built (``request_transport``); never the arena's body or the request's.
        target, to = (request.method, url, headers), (host_header, extensions, client)
        if outcome is not None:
            outcome.forwarded = time.time()
        attempt = 1
        try:
            response = await self._sent(turn, envelope, attempt, target, signer, to)
            if isinstance(response, SendRefusal):
                return self._not_sent(
                    turn, response, request.method, path, upstream, outcome, exchange, received=received
                )
            turn.replies += 1
            turn.reply_status = response.status_code if 100 <= response.status_code <= 599 else None
            turn.execution = Attempted(attempt, response.status_code)
            stale = response.headers.get("x-amzn-errortype", "").lower().startswith("expiredtoken")
            if signer is not None and signing is not None and signing.profile and stale:
                # Amazon found the credentials stale: read them again, and send once more (nothing streamed yet).
                await response.aclose()
                assert self.aws is not None
                self.aws.invalidate(signing.profile)
                try:
                    signer = (await self._credentials(signing), signing.region)
                except AwsError as error:
                    self._record(outcome, status=400, error=error.code)
                    return upstream_error(f"{error}. {_FURTHER}", 400, shape="bedrock")
                again = await self._judged_again(
                    turn, upstream, prepared, envelope, used, sent_under, request.method, wire, body, sent=True
                )
                if again is not None:  # no further attempt sent; its first may have been delivered or processed
                    self._record(outcome, status=again.status_code, error="exception")
                    return again
                attempt = 2
                response = await self._sent(turn, envelope, attempt, target, signer, to)
                if isinstance(response, SendRefusal):
                    return self._not_sent(
                        turn, response, request.method, path, upstream, outcome, exchange, received=received
                    )
                turn.replies += 1
                turn.reply_status = response.status_code if 100 <= response.status_code <= 599 else None
                turn.execution = Attempted(attempt, response.status_code)
        except httpx.HTTPError as exc:
            assert self.mediator is not None
            if outcome is not None:
                outcome.status = 502
            failed = ResponseFinalizer(ResponseCapture(None), self.mediator.executor, self.recorder, exchange, 502)
            failed.seal(outcome, complete=False, error="response-error")
            await failed.complete()
            unreachable = f"upstream {upstream.name} unreachable ({type(exc).__name__})"
            return upstream_error(unreachable, 502, shape=error_shape(upstream, prepared))
        assert self.mediator is not None
        content_type = response.headers.get("content-type") or (
            "text/event-stream" if prepared is not None and prepared.stream else ""
        )
        seen = ResponseCapture(
            prepared.protocol if prepared is not None else None,
            content_type,
            response.headers.get("content-encoding"),
        )
        seen.metadata(forwardable(response.headers.multi_items(), RESPONSE_DROP))
        finalizer = ResponseFinalizer(seen, self.mediator.executor, self.recorder, exchange, response.status_code)
        if outcome is not None:
            outcome.status = response.status_code
        if self.mediator is not None and prepared is not None:  # it reached its provider: what it carried left
            self.mediator.left(prepared)
            if admission is not None and admission.removed:
                self.mediator.envelope_removed_headers = min(
                    (1 << 63) - 1,
                    self.mediator.envelope_removed_headers + admission.removed,
                )
                if response.status_code in {401, 403}:
                    status = response.status_code
                    finalizer.seal(outcome, complete=False, error="headers-removed")
                    await close_response(response, finalizer, None)
                    return upstream_error(
                        f"the provider answered {status} after Latet removed {admission.removed} SDK/tracing headers",
                        status,
                        shape=error_shape(upstream, prepared),
                    )
        if redirected(response.status_code, response.headers):  # neither followed nor passed on
            return await self._redirected(response, finalizer, request.method, path, upstream, prepared, outcome)

        transform = None
        if prepared is not None and prepared.mode == REWRITE and response.status_code < 400 and self.mediator:
            transform = self.mediator.response_transform(
                prepared, content_type, response.headers.get("content-encoding")
            )
        # What the model writes in an answer passed on as it came (Claude Code's), read before each chunk goes on.
        written: Callable[[bytes | None], Awaitable[None]] | None = None
        capture = None
        if prepared is not None and prepared.session is not None and response.status_code < 400 and self.mediator:
            capture = self.mediator.written(
                prepared, content_type, response.headers.get("content-encoding"), observe=transform is None
            )
            if capture is not None:
                written = partial(self.mediator.read_written, capture, prepared.session)
        drop = RESPONSE_DROP | ({"content-encoding"} if transform is not None else frozenset())
        response_headers = forwardable(response.headers.multi_items(), drop)
        if transform is not None and not transform.streams:
            whole = _Whole(response, seen, finalizer, response_headers)
            return await self._restored_whole(whole, transform, request.method, path, upstream, prepared, outcome)

        # The answer's headers as they go out, before anything is handed on: one that cannot be written so (a header
        # outside latin-1) fails here, the provider's answer closed, the hold left to ``handle``'s ``finally``.
        try:
            raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response_headers]
        except BaseException:
            finalizer.seal(outcome, complete=False, error="response-headers")
            await _finish_response(
                response,
                finalizer,
                "response-headers",
                capture.opaque.close if capture is not None and capture.opaque is not None else None,
                transform.close if transform is not None else None,
            )
            raise
        # A restored stream's lease handed on to the response, which releases it as its relay ends, or, unstarted, as
        # its ASGI call does (``_OwnedStream``); the provider's answer is the response's to close in either case.
        lease = prepared.lease if prepared is not None else None
        if lease is not None and prepared is not None:
            lease = prepared.lease = lease.transfer()
        started = [False]

        async def relay() -> AsyncIterator[bytes]:
            started[0] = True
            error: str | None = None
            completed = False
            pending: list[bytes] = []
            pending_charge = 0
            echo_owner = (
                self.mediator.echoes if self.mediator is not None and capture is not None and capture.opaque else None
            )
            try:
                # Whole raw JSON stays behind its EOF commit, including trailing chunks. Every owned chunk
                # is charged under the opaque owner cap; SSE commits in written() before its terminal chunk.
                async for chunk in response.aiter_raw(chunk_size=64 * 1024 if echo_owner else None):
                    seen.feed(chunk)

                    if written is not None:
                        await written(chunk)  # before it is passed on
                    if transform is None:
                        if (
                            echo_owner is not None
                            and capture is not None
                            and capture.opaque is not None
                            and capture.holds_answer
                        ):
                            charge = len(chunk) + 128  # bytes object plus rounded pending-list capacity
                            if echo_owner.reserve(charge):
                                pending.append(chunk)
                                pending_charge += charge
                                continue
                            capture.opaque.close()
                            for previous in pending:
                                yield previous
                            pending = []
                            previous = b""
                            echo_owner.release(pending_charge)
                            pending_charge = 0
                            echo_owner = None
                        yield chunk
                        continue
                    async for out in transform.restored(chunk):
                        if out:
                            yield out
                        await asyncio.sleep(0)
                    if transform.stopped is not None:
                        break
                if written is not None:
                    await written(None)
                if pending:
                    for previous in pending:
                        yield previous
                    pending = []
                    previous = chunk = b""
                    if echo_owner is not None:
                        echo_owner.release(pending_charge)
                    pending_charge = 0
                if transform is not None:
                    tail = await transform.restored_end()
                    if tail:
                        yield tail
                    if (
                        transform.stopped is not None
                    ):  # the answer, cut at what Latet reads: said, and not recorded whole
                        error = transform.stopped
                        log.warning("stopped the answer to %s %s: %s", request.method, path, error)
                completed = error is None
            except BaseException as exc:  # includes the client going away (cancellation)
                error = type(exc).__name__
                raise
            finally:
                finalizer.seal(outcome, complete=completed, error=error)
                if completed:
                    # Every byte of it handed to its client: recorded whole whatever the client does next, its request
                    # cancelled as it goes at once included (F2's HTTP counterpart; minors after 0.11, item 3).
                    finalizer.relayed()
                pending = []
                previous = chunk = b""  # no yielded or queued pending reference survives its reservation
                await _finish_response(
                    response,
                    finalizer,
                    error,
                    partial(echo_owner.release, pending_charge) if echo_owner is not None and pending_charge else None,
                    capture.opaque.close if capture is not None and capture.opaque is not None else None,
                    transform.close if transform is not None else None,
                    lease.release if lease is not None else None,
                    lease.cancel_renewer if lease is not None else None,
                )

        streamed = _OwnedStream(
            relay(),
            started,
            lease,
            response,
            transform,
            status_code=response.status_code,
            finalizer=finalizer,
            outcome=outcome,
        )
        streamed.raw_headers = raw_headers
        turn.streams = lease is not None  # the request freeze precedes release, including pass-through answers
        return streamed

    def _answered(self, prepared: Prepared | None) -> None:
        """A conversation's answer restored (or ended): what its vault kept in memory for it may go again
        (``Engine.release``; an API key's vault, ``StoredVault.hold``), through its lease: once, and only for a request
        that held (any other release would let another request of the conversation, its answer awaited meanwhile, lose
        its hold), its own hold's count, never a newer one's."""
        if self.mediator is None or prepared is None or prepared.lease is None or prepared.lease.released:
            return
        prepared.lease.release()

    async def _redirected(
        self,
        response: httpx.Response,
        finalizer: ResponseFinalizer,
        method: str,
        path: str,
        upstream: Upstream,
        prepared: Prepared | None,
        outcome: Outcome | None,
    ) -> Response:
        """An upstream's answer that sends the request elsewhere (``redirected``), streamed or not: closed unread, and
        the client answered 502 with a clear error in its place, with no Location and nothing of the answer, so that it
        has nothing to follow. The log says the provider's status, never the address it names; the record, the 502 the
        client got."""
        status = response.status_code
        if outcome is not None:
            outcome.status = 502
        finalizer.seal(outcome, complete=False, error="redirect")
        await close_response(response, finalizer, None)
        log.warning("stopped the answer to %s %s: a redirect (%d), which is not followed", method, path, status)
        return upstream_error(REDIRECTED.format(status=status), 502, shape=error_shape(upstream, prepared))

    async def _restored_whole(
        self,
        whole: _Whole,
        transform: ResponseTransform,
        method: str,
        path: str,
        upstream: Upstream,
        prepared: Prepared | None,
        outcome: Outcome | None,
    ) -> Response:
        """An answer restored once it is whole: read first, no further than Latet reads it decoded, so that one that
        does not decode or is larger than that is answered with a clear error rather than cut short, and nothing of it
        is passed on. Then the answer, restored (a large one on the engine's thread, the event loop running meanwhile),
        with the provider's status and headers."""
        response, error = whole.response, None
        completed = False
        try:
            async for chunk in response.aiter_raw():
                whole.seen.feed(chunk)

                async for _ in transform.restored(chunk):  # held: nothing goes out yet
                    await asyncio.sleep(0)  # what one chunk decodes to is held in turns with the others' work
            body = await transform.restored_end()
            completed = True
        except ResponseError as refused:
            error = type(refused).__name__
            log.warning("stopped the answer to %s %s: %s", method, path, error)
            text = STOPPED_ANSWER.format(refused)
            return upstream_error(text, 502, shape=error_shape(upstream, prepared))
        except BaseException as exc:  # includes the client going away (cancellation)
            error = type(exc).__name__
            raise
        finally:
            if outcome is not None and error in _REFUSED:
                outcome.status = 502
            whole.finalizer.seal(outcome, complete=completed, error=error)
            await _finish_response(
                response,
                whole.finalizer,
                error,
                transform.close,
                partial(self._answered, prepared),
            )
        answered = Response(body, status_code=response.status_code)
        answered.raw_headers = [
            *((k.encode("latin-1"), v.encode("latin-1")) for k, v in whole.headers),
            (b"content-length", str(len(body)).encode()),
        ]
        return answered

    def _record(self, outcome: Outcome | None, status: int | None = None, error: str | None = None) -> None:
        if outcome is None or self.recorder is None:
            return
        if status is not None:
            outcome.status = status
        outcome.error = outcome.error or error
        self.recorder.finish(outcome)
