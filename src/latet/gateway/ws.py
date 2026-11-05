"""WebSocket relay (Codex's Responses API over WebSocket), one upstream connection per client.

On a Latet server the gate gives the socket's app what counts its turns in its identity's requests in flight
(``latet.turns`` in its scope): a turn is counted from the client's frame that asks until the answer's last event (or
until the frame is blocked, or the socket ends), never the socket's whole life, so that an idle session holds no place.
A turn past the cap is refused with an error frame, nothing of it sent.

What answers one frame of the client's carries at most as many frames as a stream carries events
(``latet.protocols.sse.MAX_EVENTS``), each one an event restored on the event loop every identity shares: past it the
turn is stopped with an error frame, and both connections close, nothing more of it read or passed on.

A socket's conversation is read in the engine's thread, as an HTTP request's is (on a server a round trip to Valkey, and
the values another instance showed it loaded from PostgreSQL). Its two legs, the client's frames and the upstream's,
end with the relay, whatever ends it (on a server, its space closing cancels it): a leg that fails (a turn the engine's
thread, gone as its space closes, could not mask) closes the socket with 1011, nothing of that turn sent."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import CloseCode

from latet.engine.pipeline import RequestScope, request_scope
from latet.gateway import request_profiles, request_transport
from latet.gateway.capture import Capture
from latet.gateway.coverage import EnvelopeProof, EnvelopeRefused, envelope_of, frame_envelope, inventory
from latet.gateway.forward import (
    REDIRECTED,
    decision_passes,
    error_shape,
    history_coverage,
    marked,
    redirected,
    stopped,
    upstream_error,
)
from latet.gateway.headers import WS_HANDSHAKE_DROP, unread_redacted
from latet.gateway.launch_authority import ABSENT, REQUEST_LAUNCH, RequestLaunch
from latet.gateway.lease import ExchangeLease
from latet.gateway.mediate import STOPPED_ANSWER, VERIFY, Mediator, Prepared, WSResponseRewriter
from latet.gateway.outcome import Block, decide, finalize, refuse, response_plan
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture, agent_of
from latet.gateway.request_audit import RequestAudit, emit_decision, reason_code, route_code
from latet.gateway.request_policy import LocalAuthority, current
from latet.gateway.request_transport import SendRefusal
from latet.gateway.response_finalizer import ResponseFinalizer, drain_task
from latet.gateway.routes import RouteTable, Upstream, wire_path
from latet.protocols import sse
from latet.protocols.sse import MAX_DECODED, ResponseTooLarge, TooManyEvents, too_large, too_many
from latet.server.egress import EgressRefused

# Handshake response headers that belong to each leg's own negotiation.
_ACCEPT_DROP = WS_HANDSHAKE_DROP | {"date", "server", "content-length", "content-type"}
_SESSION_HEADERS = ("session-id", "session_id", "thread-id", "x-session-id")
# Why a WebSocket to a destination of the identity's own is closed before it opens: a close's reason holds 123 bytes
# at most, so it says what matters (the address's words go to an HTTP request's answer).
_NOT_REACHED = "latet: the server may not reach this destination for you: nothing was sent"
# Why a WebSocket is closed (1011) when Latet cannot go on with it: its conversation could not be read (a store did not
# answer), or a turn or an answer could not be (the engine's thread gone as its space closes). Nothing unread is sent.
_UNREAD_CONVERSATION = "latet: this conversation could not be read now: nothing was sent"
# A close reason is at most 123 bytes: Google's Live API on a route that refuses what Latet does not read.
_LIVE_UNREAD = "latet: Latet does not read Google's Live API yet, so it does not connect: nothing was sent"
_CANNOT_GO_ON = "latet: this connection could not go on: nothing more of it was sent"
# A handshake the guarded transport would not open as it was decided (never in Phase 1).
_NOT_CONNECTED = "latet: Latet could not open this connection as it was decided: nothing was sent"
# D5, in a close's reason (at most 123 bytes): Latet is off in the socket's project, so it opens nothing there.
_OFF_CLOSED = "latet: Latet is off in this project: turn it on there, or reach the provider directly"
# The events that end a turn's answer, as ``ResponseCapture`` judges a turn done.
_TURN_ENDS = frozenset({"response.completed", "response.done", "response.incomplete", "response.failed", "error"})
# The events after which nothing more of a turn's answer is restored, so that its hold may go (RC-REC-11 (c)). Not an
# ``error``: one may come mid-answer and the answer go on. Ambiguous errors keep one connection owner until its
# outstanding generations finish or the socket closes; successful supersedes consolidate into the newest hold.
_ANSWER_ENDS = _TURN_ENDS - {"error"}
# Each WebSocket's number in this process, for its envelopes: never reused, as an object's id is once it is freed.
_CONNECTIONS = itertools.count(1)


log = logging.getLogger(__name__)


def _ends_turn(data: str, ends: frozenset[str] = _TURN_ENDS) -> bool:
    """Whether an upstream's text frame ends the turn it answers (``ends``: by default any event that gives its place
    back): read only when it names such an event (most frames are deltas, and name none)."""
    if not any(end in data for end in ends):
        return False
    try:
        found = json.loads(data)
    except ValueError:
        return False
    return isinstance(found, dict) and found.get("type") in ends


def _count_removed(mediator: Any, removed: int) -> None:
    """D4's payload-free saturating count of declared removable headers, as a request over HTTP counts them."""
    if removed:
        mediator.envelope_removed_headers = min((1 << 63) - 1, mediator.envelope_removed_headers + removed)


async def _closed(websocket: WebSocket, block: Block) -> None:
    """A socket refused before anything of it was read or connected: closed with its decision's code and words."""
    await websocket.close(code=block.status, reason=block.reason)


def _envelope_refusal(upstream: Upstream, code: str) -> Block:
    """The decision of a handshake refused for its envelope, made of its category alone (``EnvelopeRefused.code``), as
    an HTTP request's is: nothing else of the error goes into it, nor into what is made of it (its log lines)."""
    return refuse("ws-handshake", upstream.name, code, f"request envelope refused ({code})", 1008)


def _refusal_recorded(
    recorder: Recorder | None,
    upstream: Upstream,
    template: str,
    shook: Block,
    status: int,
    prepared: Prepared,
    *,
    profile: str | None = None,
    removed: int = 0,
) -> None:
    """A handshake refused before anything connected, in the history as a refused request over HTTP is (D4, R-79): its
    category's code and its route template, no header kept; once its profile admitted it, that profile's id and the
    count of headers it removes, as over HTTP."""
    if recorder is None or not recorder.enabled:
        return
    outcome = Outcome({}, upstream.name, template, "ws", time.time(), status=status)
    outcome.prepared = prepared
    outcome.history_code = history_coverage(shook)
    outcome.block_code = shook.code
    outcome.profile = profile
    outcome.removed_headers = removed
    recorder.finish(outcome)


async def _envelope_closed(
    websocket: WebSocket,
    recorder: Recorder | None,
    upstream: Upstream,
    wire: str,
    error: EnvelopeRefused,
    shook: Block,
) -> None:
    """A handshake refused for its envelope, nothing connected (P2-8a, root's ruling 5): its decision (``shook``, made
    by ``_envelope_refusal``) and the log keep its category alone, as an HTTP request's do; the 1008 close tells the
    agent that category and the header's or query parameter's name, never a value (``EnvelopeRefused.close_reason``),
    and the history records it as a refused request over HTTP is recorded (``_refusal_recorded``), under the route
    template its profiles would read it by."""
    log.warning("blocked WebSocket envelope: %s", error.code)  # as an HTTP request's: its fixed category alone
    template = request_profiles.refused_template(upstream, "POST", wire)  # a handshake is read as its POST
    prepared = Prepared(VERIFY, None, b"", block_reason=shook.reason)
    _refusal_recorded(recorder, upstream, template, shook, error.status, prepared)
    await websocket.close(code=CloseCode.POLICY_VIOLATION, reason=error.close_reason())


async def _pinned(pin: Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]], url: str) -> tuple[str, int]:
    """The address a WebSocket to a destination of the identity's own connects to: checked now, as an HTTP request's
    is (``pin``, given the destination's own form of ``url``). Raises EgressRefused: nothing is sent."""
    parts = urlsplit(url)
    checked, _, _ = await pin(parts._replace(scheme="https" if parts.scheme == "wss" else "http").geturl())
    address = urlsplit(checked)
    if address.hostname is None:
        raise EgressRefused("its address cannot be used")
    return address.hostname, address.port or (443 if address.scheme == "https" else 80)


@dataclass(slots=True, repr=False)
class _ResponseTurn:
    finalizer: ResponseFinalizer
    outcome: Outcome | None
    sent: asyncio.Event = field(default_factory=asyncio.Event)
    transported: asyncio.Event = field(default_factory=asyncio.Event)


async def relay(
    websocket: WebSocket,
    routes: RouteTable,
    capture: Capture,
    mediator: Mediator | None = None,
    recorder: Recorder | None = None,
    tutorial: bool = False,  # the tutorial's sandbox: a stopped turn's error points to its own pane
    pin: Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]] | None = None,
    personal: Callable[[], set[str]] | None = None,
    request_audit: RequestAudit | None = None,
) -> None:
    """``pin`` and ``personal``: on a Latet server, how a destination of the identity's own is reached (the address
    checked when the server connects) and the names of those destinations, as the forwarder's."""
    shook = None
    handshake_route = 0
    handshake_attempts = handshake_answers = handshake_status = None
    handshake_reason = None
    try:
        # One launch authority on both machines (Revision 9): its ingress refused a carrier it denies before here.
        launch = websocket.scope.pop(REQUEST_LAUNCH, None)
        if launch is None:
            launch = ABSENT
        if type(launch) is not RequestLaunch:
            await _closed(websocket, shook := refuse("ws-handshake", None, "proof-incomplete", _NOT_CONNECTED, 1008))
            return
        path = websocket.url.path
        wire = wire_path(websocket.scope)
        upstream = routes.match(wire)
        handshake_route = route_code(upstream)
        # Each refusal before reading the socket is its handshake's decision,
        # a Block (its close code
        # and words), nothing of it connected.
        if upstream is None or not upstream.websocket:
            await _closed(websocket, shook := refuse("ws-handshake", None, "no-route", "", 4404))
            return
        if upstream.refuses_unread:  # Google's Live API: frames Latet does not read, never relayed as they came
            await _closed(websocket, shook := refuse("ws-handshake", upstream.name, "unread", _LIVE_UNREAD, 4400))
            return

        if mediator is None:
            await _closed(
                websocket, shook := refuse("ws-handshake", upstream.name, "proof-incomplete", _NOT_CONNECTED, 1008)
            )
            return
        executor = mediator.executor
        authority = LocalAuthority.capture(mediator.engine, upstream, routes, wire)
        url = upstream.ws_target(wire, websocket.url.query)
        # The handshake as it came, before any change: each frame's envelope is the socket's route with the frame.
        handshake = envelope_of(websocket.scope, "ws-handshake", upstream.name, connection=next(_CONNECTIONS))
        try:
            admission = request_profiles.admit(inventory(handshake), upstream)
        except EnvelopeRefused as error:
            # Its decision from its category alone: nothing else of the error reaches what is made of ``shook``.
            shook = _envelope_refusal(upstream, error.code)
            await _envelope_closed(websocket, recorder, upstream, wire, error, shook)
            return
        if admission.exceptions:
            # Request exceptions are taken over HTTP alone in this revision: a WebSocket's handshake that only an
            # exception would admit is refused as it would be without one (fail closed, never loosened).
            excepted = admission.exceptions[0][1]
            shook = _envelope_refusal(upstream, excepted.code)
            await _envelope_closed(websocket, recorder, upstream, wire, excepted, shook)
            return
        warning_path = admission.profile.template
        profile_id = admission.profile.id  # what each turn's history names its profile by, as a request over HTTP
        incoming = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in websocket.headers.raw]
        lowered = {k.lower(): v for k, v in incoming}
        headers = list(admission.headers)
        # What records keep (each turn's history, a capture): a header sent unread by its role never in clear, whatever
        # its name, as over HTTP (I-3).
        kept = unread_redacted(headers, admission.unread)
        exchange = capture.exchange("ws", upstream.name, "GET", url)
        if exchange:
            exchange.request(unread_redacted(incoming, admission.unread), b"", None)

        session = None
        rewriter = None
        if mediator is not None:
            key = next((f"{h}:{lowered[h]}" for h in _SESSION_HEADERS if lowered.get(h)), None)
            try:
                # In the engine's thread, as an HTTP request's (``Mediator.prepare``): on a server it is a round trip to
                # Valkey, and the values another instance showed the conversation are loaded from PostgreSQL.
                session = await mediator.run(mediator.engine.session, key or f"ws:{id(websocket)}")
            except Exception as exc:  # a store that did not answer, or the engine's thread gone (its space closes)
                log.warning(
                    "not connected %s: its conversation could not be read (%s)", warning_path, type(exc).__name__
                )
                if exchange:
                    exchange.response_end(error=type(exc).__name__)
                unread = refuse("ws-handshake", upstream.name, "conversation-unread", _UNREAD_CONVERSATION, 1011)
                shook = unread
                await _closed(websocket, unread)
                return
            rewriter = WSResponseRewriter(mediator, session)

        # S3: strict envelope and empty-body authority before connecting. The guarded transport also checks the
        # concrete handshake websockets builds before a byte of it is written (RC-REC-9).
        assert session is not None
        selection_token = request_scope.set(RequestScope())
        try:
            handshake_arena = await mediator.run(
                mediator.prepare_handshake, upstream, wire, lowered, admission.checked, session, launch
            )
        finally:
            request_scope.reset(selection_token)
        handshake_arena.route_authority = authority
        proof = EnvelopeProof(
            handshake,
            len(admission.inventory.entries),
            admission.profile.id,
            admission.treatments,
            admission.headers,
            url,
            admission.checked,
            admission.removed,
            admission.inventory.limits.header_occurrences,
            admission.inventory.limits.header_bytes,
        )
        shook = finalize(
            handshake_arena,
            handshake,
            current(),
            response_plan(handshake_arena),
            envelope_proof=proof,
        )
        authorized = (
            request_transport.validate_connect(shook, handshake, current(), headers, handshake_arena)
            if not isinstance(shook, Block)
            else SendRefusal("block")
        )
        if isinstance(authorized, SendRefusal):
            handshake_attempts = handshake_answers = 0
            handshake_reason = reason_code(authorized.code)
        pinned: tuple[str, int] | None = None
        if (
            not isinstance(authorized, SendRefusal)
            and pin is not None
            and (upstream.own or upstream.name in (personal() if personal is not None else ()))
        ):
            try:
                pinned = await _pinned(pin, url)
            except EgressRefused:
                await _closed(
                    websocket, shook := refuse("ws-handshake", upstream.name, "egress-refused", _NOT_REACHED, 4400)
                )
                return
        try:
            up = (
                await request_transport.connect_ws(authorized, url, pinned, MAX_DECODED)
                if not isinstance(authorized, SendRefusal)
                else authorized
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            handshake_attempts = handshake_answers = 1
            _count_removed(mediator, admission.removed)  # the handshake reached its provider: what it removed left
            handshake_status = status
            # Following a redirect would take the frames there unmasked.
            if redirected(status, exc.response.headers):
                log.warning("not connected %s: the upstream answered a redirect, which is not followed", warning_path)
                if exchange:
                    exchange.response_end(error="redirect")
                refused = upstream_error(REDIRECTED.format(status=status), 502, shape=error_shape(upstream, None))
                await websocket.send_denial_response(refused)
                return
            # Relay the upstream's refusal (401, 426...) so the client reacts as it would directly.
            denial_headers = {k: v for k, v in exc.response.headers.raw_items() if k.lower() not in _ACCEPT_DROP}
            if exchange:
                exchange.response_start(status, list(denial_headers.items()))
                exchange.response_end(error="handshake refused")
            await websocket.send_denial_response(
                Response(bytes(exc.response.body or b""), status_code=status, headers=denial_headers)
            )
            return
        except Exception as exc:
            if exchange:
                exchange.response_end(error=repr(exc))
            await websocket.close(code=1011, reason="latet: upstream unreachable")
            return
        if isinstance(up, SendRefusal):  # refused at its decision (D5: an off project's), or not opened as decided
            handshake_attempts = handshake_answers = 0
            handshake_reason = reason_code(up.code)
            log.warning("not connected %s: %s", warning_path, up.code)
            if exchange:
                exchange.response_end(error="not-sendable")
            off = isinstance(shook, Block) and handshake_arena.off
            if isinstance(shook, Block):  # in the history as the same refusal over HTTP is (F1 of the 0.11 baseline)
                _refusal_recorded(
                    recorder,
                    upstream,
                    warning_path,
                    shook,
                    shook.status,
                    handshake_arena,
                    profile=profile_id,
                    removed=admission.removed,
                )
            await websocket.close(code=CloseCode.POLICY_VIOLATION, reason=_OFF_CLOSED if off else _NOT_CONNECTED)
            return

        assert up.response is not None  # set once the handshake succeeded
        handshake_attempts = handshake_answers = 1
        handshake_status = 101
        _count_removed(mediator, admission.removed)
    finally:
        # Exactly one handshake unit, before local accept/long frame relay.
        if type(shook) is Block:
            handshake_attempts = handshake_answers = 0
            handshake_status = None
        emit_decision(
            request_audit,
            shook,
            source=2,
            method=9,
            route=handshake_route,
            attempts=handshake_attempts,
            answered=handshake_answers,
            status=handshake_status,
            reason=handshake_reason,
        )
    accept_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in up.response.headers.raw_items()
        if k.lower() not in _ACCEPT_DROP
    ]
    if exchange:
        exchange.response_start(101, [(k.decode(), v.decode()) for k, v in accept_headers])
    await websocket.accept(headers=accept_headers)

    # One dashboard record per turn: a response.create frame and the events answering it.
    recording = recorder is not None and recorder.enabled
    active: _ResponseTurn | None = None
    closing: _ResponseTurn | None = None
    stopping = False
    response_headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in accept_headers]
    too_big: list[bool] = []  # the upstream sent a message larger than Latet reads
    flooded: list[bool] = []  # the upstream sent more frames for one of the client's than a stream has events

    def take(
        expected: _ResponseTurn | None, reason: str | None = None, *, terminal: bool = False
    ) -> _ResponseTurn | None:
        nonlocal active, closing
        if expected is None:
            return None
        if active is not expected:
            return closing if closing is expected else None
        if closing is not None:
            raise RuntimeError("response-closing-owner")
        active = None
        closing = expected
        if terminal:
            expected.transported.clear()
        expected.finalizer.seal(expected.outcome, complete=reason is None, error=reason)
        expected.outcome = None
        return expected

    async def barrier(*, propagate: bool = True) -> None:
        nonlocal closing
        owner = closing
        if owner is None:
            return
        # A provider can complete during send_frame; both transport decisions
        # settle before screening can commit or another request can take ownership.
        try:
            await owner.sent.wait()
            await owner.transported.wait()
        except asyncio.CancelledError:
            owner.finalizer.stop()
            raise
        await owner.finalizer.complete(propagate=propagate)
        if closing is owner:
            closing = None

    # On a server: the turn in flight, counted in its identity's requests in flight (the module's words). One at a time
    # for a socket: a frame sent while one is answered belongs to it.
    turns = websocket.scope.get("latet.turns")
    counted: list[bool] = []
    # The upstream's frames since the client last sent one: at most a stream's events (the module's words), each one
    # restored on the event loop. Counted afresh only when the client asks: an end of a turn the upstream writes
    # itself, as often as it likes, counts for nothing.
    answered = [0]
    # RC-REC-11 (c): the pending frame owns its hold until refusal or send. Successful supersedes replace one
    # connection owner, releasing its exact old handle only after a new hold protects the whole session. Scalar
    # generations track oldest completions without retaining a lease per ambiguous error/retry.
    pending: list[ExchangeLease] = []
    answering: list[ExchangeLease] = []  # one bounded connection owner, plus pending while the next send runs
    sent_generation = 0
    completed_generation = 0
    revoked = False

    def let_go(leases: list[ExchangeLease]) -> None:
        while leases:
            leases.pop().release()

    def turn_over() -> None:
        if counted and turns is not None:
            counted.clear()
            turns.end()

    async def client_to_upstream() -> None:
        nonlocal sent_generation, revoked, active
        prepared: Prepared | None = None
        provisional: _ResponseTurn | None = None
        try:
            while True:
                if prepared is not None:
                    prepared.close_receipt()
                    prepared = None
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                decision = None
                frame_attempts = frame_answers = None
                frame_reason = None
                selection_token = request_scope.set(RequestScope())
                try:
                    if not authority.valid():
                        revoked = True
                        return
                    if turns is not None and not counted:  # a turn asked: counted, or refused with nothing of it sent
                        if (refused := turns.begin()) is not None:  # its decision, a ``Block``, before it is read
                            busy = refuse("ws-frame", upstream.name, "busy", refused, 429)
                            decision = busy
                            frame_attempts = frame_answers = 0
                            error = {"type": "error", "error": {"type": "latet_busy", "message": busy.reason}}
                            await websocket.send_text(json.dumps(error))
                            continue
                        counted.append(True)
                    text = message.get("text")
                    came: str | bytes = text if text is not None else message.get("bytes") or b""  # as it came
                    data = came
                    started = time.time()
                    if text is None:
                        if mediator is not None and session is not None:  # a server reads none: refused there
                            prepared = mediator.unread_frame("a binary frame", session)
                    elif mediator is not None and session is not None:
                        # Given up while it masks: what it holds is released as the executor ends (RC-REC-11).
                        data, prepared = await mediator.run_held(
                            mediator.mask_ws_frame, text, session, upstream, wire, websocket.url.query, launch, lowered
                        )
                        if prepared is not None and prepared.lease is not None:
                            pending.append(prepared.lease)
                        if prepared is not None and not prepared.block_reason:
                            masked = prepared.masked_count
                            if prepared.reviews:  # the local reviewer reads it first
                                prepared = await mediator.review_prepared(prepared)
                            if not prepared.block_reason:  # then you, when the Live Reviewer is on
                                prepared = await mediator.live_prepared(prepared, agent_of(lowered, path)[0])
                            # Written anew on any change they made, an image withheld or covered with no text masked as
                            # well: as a body over HTTP, which leaves as ``prepared.body`` holds it.
                            if not prepared.block_reason and (prepared.amended or prepared.masked_count != masked):
                                assert prepared.frame_payload is not None
                                data = prepared.frame_payload
                    # Its decision, once, as it will leave (written anew): a frame read or not, refused or a ``Pass``.
                    if not authority.valid():
                        revoked = True
                        return
                    envelope = frame_envelope(handshake, came)
                    if prepared is not None:
                        prepared.route_authority = authority
                        if prepared.control_target is not None and rewriter is not None:
                            prepared.control_current = rewriter.target_check(prepared.control_target)
                            if not prepared.control_current():
                                prepared.block_reason = "cancel target is not the current response of this connection"
                    decision = decide(prepared, envelope, payload=data)
                    if prepared is not None and not prepared.opaque_current():
                        decision = refuse(
                            envelope.transport,
                            upstream.name,
                            "evidence",
                            "opaque evidence expired; send the request again",
                            400,
                        )
                    if isinstance(decision, Block):
                        frame_attempts = frame_answers = 0
                        log.warning("blocked a WebSocket turn: %s", decision.reason)
                        if recording and recorder is not None:  # on the dashboard as a request stopped over HTTP is
                            # Over at once, never in `turn`: the turn answering goes on, this frame never left.
                            recorder.finish(
                                Outcome(
                                    lowered,
                                    upstream.name,
                                    warning_path,  # its route template, as a request over HTTP is recorded
                                    "ws",
                                    started,
                                    prepared,
                                    400,
                                    request_headers=kept,
                                    body_bytes=len(data),
                                    payload=data,
                                    block_code=decision.code,
                                    history_code=history_coverage(decision),
                                    profile=profile_id,
                                    removed_headers=admission.removed,  # what its profile removed at the handshake (D4)
                                )
                            )
                        told = stopped(decision.reason, tutorial)  # as a request over HTTP is told
                        error = {"type": "error", "error": {"type": "latet_blocked", "message": told}}
                        turn_over()  # nothing will answer it
                        let_go(pending)  # nor restore anything for it
                        await websocket.send_text(json.dumps(error))
                        continue
                    # End the exact old owner before final authority/transport validation.
                    creates = prepared is None or prepared.protocol != "ws-cancel"
                    if creates:
                        take(active, "superseded")
                    await barrier()
                    if stopping:
                        return
                    if not authority.valid():
                        revoked = True
                        return
                    # Recheck the original live predicate without changing its sealed identity.
                    if prepared is not None and (
                        (prepared.control_current is not None and not prepared.control_current())
                        or not prepared.opaque_current()
                    ):
                        frame = SendRefusal("evidence")
                    else:
                        frame = (
                            request_transport.validate_frame(
                                decision,
                                prepared,
                                envelope,
                                current(),
                                up,
                                on_send=rewriter.begin if rewriter is not None and creates else None,
                            )
                            if not isinstance(decision, Block)
                            else SendRefusal("block")
                        )
                    if isinstance(frame, SendRefusal):  # never in Phase 1: a decision the transport would not send
                        frame_attempts = frame_answers = 0
                        frame_reason = reason_code(frame.code)
                        if not authority.valid():
                            revoked = True
                            return
                        log.warning("not sent a WebSocket turn: %s", frame.code)
                        told = stopped(f"Latet could not send this turn as it was decided ({frame.code})", tutorial)
                        turn_over()
                        let_go(pending)
                        error = {"type": "error", "error": {"type": "latet_error", "message": told}}
                        await websocket.send_text(json.dumps(error))
                        continue
                    provisional = None
                    if creates:
                        seen = ResponseCapture("responses")
                        seen.metadata(response_headers)
                        owner = ResponseFinalizer(
                            seen, executor, recorder if recording else None, exchange, 101, terminal=False
                        )
                        outcome = (
                            Outcome(
                                lowered,
                                upstream.name,
                                warning_path,
                                "ws",
                                started,
                                prepared,
                                101,
                                time.time(),
                                request_headers=kept,
                                body_bytes=len(data),
                                payload=data,
                                # Its coverage and what it was let through by, from its own decision, as over HTTP: a
                                # WebSocket takes no request exception in 0.11.0 (F-2), so none is used.
                                history_code=history_coverage(decision),
                                passes=decision_passes(decision),
                                profile=profile_id,
                                removed_headers=admission.removed,  # what its profile removed at the handshake (D4)
                            )
                            if recording
                            else None
                        )
                        provisional = active = _ResponseTurn(owner, outcome)
                        del seen, owner, outcome
                        provisional.transported.set()
                    if exchange:
                        exchange.ws("up", data)
                    answered[0] = 0  # a turn asked: its answer counted from here
                    # Keep both generations protected while send_frame awaits. Consolidation happens only after a
                    # successful send, without a further await: a refused replacement cannot orphan the old answer.
                    if pending:
                        sent_generation += 1  # reserve before awaiting: the provider may complete during send
                    if rewriter is not None and prepared is not None and prepared.protocol != "ws-cancel":
                        rewriter.observe(prepared)
                    try:
                        unsent = await request_transport.send_frame(up, frame)
                    except BaseException:
                        if provisional is not None:
                            provisional.finalizer.stop()
                            take(provisional, "not-sent")
                        raise
                    finally:
                        if provisional is not None:
                            provisional.sent.set()
                    if unsent is not None:  # never in Phase 1
                        if provisional is not None:
                            provisional.finalizer.stop()
                            take(provisional, "not-sent")
                        provisional = None
                        frame_attempts = frame_answers = 0
                        frame_reason = reason_code(unsent.code)
                        if not authority.valid():
                            revoked = True
                            return
                        log.warning("not sent a WebSocket turn: %s", unsent.code)
                        if pending:
                            sent_generation -= 1
                        if completed_generation == sent_generation:
                            let_go(answering)  # rollback can reveal an old completion received during send
                        let_go(pending)
                        turn_over()
                        told = stopped(f"Latet could not send this turn as it was decided ({unsent.code})", tutorial)
                        error = {"type": "error", "error": {"type": "latet_error", "message": told}}
                        await barrier()
                        await websocket.send_text(json.dumps(error))
                        continue
                    provisional = None
                    frame_attempts = 1  # successful send_frame return only; no correlated answer/status
                    if pending and completed_generation == sent_generation:
                        let_go(pending)  # this very answer completed while send_frame awaited
                    if pending:
                        heir = pending.pop().transfer()
                        let_go(answering)  # the new hold covers all session values, including old answers' values
                        answering.append(heir)
                        let_go(pending)
                    if mediator is not None and prepared is not None:  # it reached the upstream: what it carried left
                        mediator.left(prepared)
                finally:
                    try:
                        emit_decision(
                            request_audit,
                            decision,
                            source=3,
                            method=9,
                            route=handshake_route,
                            attempts=frame_attempts,
                            answered=frame_answers,
                            reason=frame_reason,
                        )
                    finally:
                        try:
                            if prepared is not None:
                                prepared.close_receipt()
                        finally:
                            request_scope.reset(selection_token)
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        finally:
            if provisional is not None and not provisional.sent.is_set():
                provisional.finalizer.stop()
                take(provisional, "not-sent")
                provisional.sent.set()
            if prepared is not None:
                prepared.close_receipt()

    async def upstream_to_client(conn: ClientConnection) -> None:
        nonlocal completed_generation
        try:
            async for data in conn:
                answered[0] += 1
                if answered[0] > sse.MAX_EVENTS:  # stopped: an error frame, and both connections close
                    flooded.append(True)
                    log.warning("stopped a WebSocket turn's answer: %s", TooManyEvents.__name__)
                    take(active, "response-budget")
                    told = marked(STOPPED_ANSWER.format(too_many(sse.MAX_EVENTS)))
                    error = {"type": "error", "error": {"type": "latet_blocked", "message": told}}
                    with contextlib.suppress(Exception):
                        await websocket.send_text(json.dumps(error))
                    conn.transport.abort()  # dropped at once: no closing handshake waits behind what it still sends
                    return
                owner = active
                terminal_owner = None
                if owner is not None:
                    owner.finalizer.capture.frame(data)
                    if isinstance(data, str) and _ends_turn(data):
                        terminal_owner = take(owner, terminal=True)
                try:
                    if isinstance(data, str):
                        if counted and _ends_turn(data):
                            turn_over()
                        frames = await rewriter.restored(data) if rewriter is not None else [data]
                        for frame in frames:
                            await websocket.send_text(frame)
                        if terminal_owner is not None:  # its last event relayed: recorded whole whatever comes next
                            terminal_owner.finalizer.relayed()
                        if (
                            (answering or pending)
                            and completed_generation < sent_generation
                            and _ends_turn(data, _ANSWER_ENDS)
                        ):
                            completed_generation += 1
                            if completed_generation == sent_generation:
                                let_go(answering)
                    else:
                        await websocket.send_bytes(data)
                except BaseException:
                    if owner is not None:
                        owner.finalizer.stop()
                        take(owner, "response-transport")
                    raise
                finally:
                    if terminal_owner is not None:
                        terminal_owner.transported.set()
                    owner = None
                if terminal_owner is not None:
                    terminal_owner = None
                    await barrier()
        except ConnectionClosed as closed:
            if closed.sent is not None and closed.sent.code == CloseCode.MESSAGE_TOO_BIG:  # refused here: said so
                too_big.append(True)
                log.warning("stopped a WebSocket turn's answer: %s", ResponseTooLarge.__name__)
                told = marked(STOPPED_ANSWER.format(too_large(MAX_DECODED)))
                error = {"type": "error", "error": {"type": "latet_blocked", "message": told}}
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(error))

    legs = {
        asyncio.create_task(client_to_upstream(), name="latet-ws-client"),
        asyncio.create_task(upstream_to_client(up), name="latet-ws-upstream"),
    }
    try:
        await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping = True
        take(active, "closed")  # a turn cut off mid-answer: its answer omitted, as it was
        if closing is not None:
            closing.finalizer.stop()  # nothing to one relayed whole (``ResponseFinalizer.relayed``), recorded whole
        if rewriter is not None:
            rewriter.close()
        # Both legs end with the relay, whatever ends it: one of them, or its own task cancelled (on a server, its space
        # closing: ``spaces.Space.end_long``), so that neither reads its client nor its upstream any more. One that
        # failed (a turn or an answer the engine's thread, gone, could not read) closes the socket with 1011, and is
        # said by its type; nothing of what it was given was sent. What needs no wait is done first: a task cancelled
        # again at each of its waits (a cancel scope's) still counts its turn over and sends both close frames.
        for leg in legs:
            leg.cancel()  # each stops at its next wait: nothing more of it read or sent
        failed = [
            error
            for leg in legs
            if leg.done() and not leg.cancelled() and (error := leg.exception()) is not None
            if not isinstance(error, (WebSocketDisconnect, ConnectionClosed))
        ]
        if failed:
            log.warning("ended a WebSocket %s: %s", warning_path, type(failed[0]).__name__)
        code = (
            CloseCode.MESSAGE_TOO_BIG
            if too_big
            else CloseCode.POLICY_VIOLATION
            if flooded or revoked
            else CloseCode.INTERNAL_ERROR
            if failed
            else (up.close_code or 1000)
        )
        turn_over()
        let_go(pending)  # what its turns hold, whatever ends it (each lease once)
        let_go(answering)

        captured = exchange

        def captured_end() -> None:
            if captured:
                captured.response_end(error="response-error" if failed else None)

        def captured_end_later() -> None:  # once a turn settling alone has ended, nobody there to be told of a failure
            with contextlib.suppress(Exception):
                captured_end()

        async def finish_connection() -> None:
            await asyncio.gather(*legs, return_exceptions=True)
            # F2 of the P2 baseline: the turn last answered whole settles alone (bounded by its engine's
            # ``ALONE_PER_ENGINE``), recorded whole, so that the close waits for none of its screening; the raw capture
            # ends after its publication. A turn cut off, or one past the bound, is waited for as before.
            last = closing
            alone = last is not None and last.finalizer.leave(captured_end_later)
            if not alone:
                await barrier(propagate=False)
            with contextlib.suppress(Exception):
                await up.close()
            with contextlib.suppress(Exception):
                if failed:
                    await websocket.close(code=code, reason=_CANNOT_GO_ON)
                else:
                    await websocket.close(code=code if 1000 <= code < 5000 else 1000)
            if not alone:
                captured_end()

        def stop_closing() -> None:
            if closing is not None:
                closing.finalizer.stop()

        await drain_task(asyncio.create_task(finish_connection(), name="latet-ws-close"), stop_closing, propagate=False)
