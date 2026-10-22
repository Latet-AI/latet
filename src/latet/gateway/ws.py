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
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import truststore
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import CloseCode

from latet.gateway.capture import Capture, Exchange
from latet.gateway.forward import REDIRECTED, error_shape, marked, redirected, stopped, upstream_error
from latet.gateway.headers import DROP_PREFIXES, WS_HANDSHAKE_DROP, forwardable
from latet.gateway.mediate import STOPPED_ANSWER, Mediator, Prepared, WSResponseRewriter
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture, agent_of
from latet.gateway.routes import RouteTable, wire_path
from latet.protocols import sse
from latet.protocols.base import dumps_compact
from latet.protocols.sse import MAX_DECODED, ResponseTooLarge, TooManyEvents, too_large, too_many
from latet.server.egress import EgressRefused

_SSL = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
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
# The events that end a turn's answer, as ``ResponseCapture`` judges a turn done.
_TURN_ENDS = frozenset({"response.completed", "response.done", "response.incomplete", "response.failed", "error"})


log = logging.getLogger(__name__)


def _ends_turn(data: str) -> bool:
    """Whether an upstream's text frame ends the turn it answers: read only when it names such an event (most frames are
    deltas, and name none)."""
    if not any(end in data for end in _TURN_ENDS):
        return False
    try:
        found = json.loads(data)
    except ValueError:
        return False
    return isinstance(found, dict) and found.get("type") in _TURN_ENDS


async def _pinned(pin: Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]], url: str) -> tuple[str, int]:
    """The address a WebSocket to a destination of the identity's own connects to: checked now, as an HTTP request's
    is (``pin``, given the destination's own form of ``url``). Raises EgressRefused: nothing is sent."""
    parts = urlsplit(url)
    checked, _, _ = await pin(parts._replace(scheme="https" if parts.scheme == "wss" else "http").geturl())
    address = urlsplit(checked)
    if address.hostname is None:
        raise EgressRefused("its address cannot be used")
    return address.hostname, address.port or (443 if address.scheme == "https" else 80)


class _NoRedirect(connect):
    """websockets' ``connect``, following no redirect, on a computer as on a server, as Latet's HTTP requests follow
    none: whether it leads to another origin (into a server's own network) or to the same one (a path or a query the
    identity chooses could make an open redirect of it send Latet on). A redirect is raised as the handshake's refusal
    (``InvalidStatus``), which ``relay`` answers with a clear error, handing the client no address to go to."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


async def _upstream(url: str, headers: list[tuple[str, str]], pinned: tuple[str, int] | None) -> ClientConnection:
    """The upstream's WebSocket, following no redirect (``_NoRedirect``). ``pinned``: on a Latet server, a destination
    of the identity's own, reached at the address checked (``_pinned``) while the Host header and TLS's name stay the
    destination's (``url``'s), taking no proxy of the server's environment."""
    options: dict[str, Any] = {
        "additional_headers": headers,
        "user_agent_header": None,
        "compression": "deflate",
        "max_size": MAX_DECODED,  # one message of the upstream's, inflated: no more than Latet reads of an answer
        "open_timeout": 30,
        "ping_interval": 20,
        "ping_timeout": 60,
        "ssl": _SSL if url.startswith("wss://") else None,
    }
    if pinned is not None:
        options |= {"host": pinned[0], "port": pinned[1], "proxy": None}
    return await _NoRedirect(url, **options)


async def relay(
    websocket: WebSocket,
    routes: RouteTable,
    capture: Capture,
    mediator: Mediator | None = None,
    recorder: Recorder | None = None,
    tutorial: bool = False,  # the tutorial's sandbox: a stopped turn's error points to its own pane
    pin: Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]] | None = None,
    personal: Callable[[], set[str]] | None = None,
) -> None:
    """``pin`` and ``personal``: on a Latet server, how a destination of the identity's own is reached (the address
    checked when the server connects) and the names of those destinations, as the forwarder's."""
    path = websocket.url.path
    wire = wire_path(websocket.scope)
    upstream = routes.match(wire)
    if upstream is None or not upstream.websocket:
        await websocket.close(code=4404)
        return
    if upstream.refuses_unread:  # Google's Live API: frames Latet does not read, never relayed as they came
        await websocket.close(code=4400, reason=_LIVE_UNREAD)
        return

    url = upstream.ws_target(wire, websocket.url.query)
    pinned: tuple[str, int] | None = None
    if pin is not None and (upstream.own or upstream.name in (personal() if personal is not None else ())):
        try:
            pinned = await _pinned(pin, url)
        except EgressRefused:  # its words name the identity's own destination: never logged
            log.warning("not connected %s: egress refused", path)
            await websocket.close(code=4400, reason=_NOT_REACHED)
            return
    incoming = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in websocket.headers.raw]
    lowered = {k.lower(): v for k, v in incoming}
    headers = forwardable(incoming, WS_HANDSHAKE_DROP, DROP_PREFIXES)
    exchange = capture.exchange("ws", upstream.name, "GET", url)
    if exchange:
        exchange.request(incoming, b"", None)

    session = None
    rewriter = None
    if mediator is not None:
        key = next((f"{h}:{lowered[h]}" for h in _SESSION_HEADERS if lowered.get(h)), None)
        try:
            # In the engine's thread, as an HTTP request's (``Mediator.prepare``): on a server it is a round trip to
            # Valkey, and the values another instance showed the conversation are loaded from PostgreSQL.
            session = await mediator.run(mediator.engine.session, key or f"ws:{id(websocket)}")
        except Exception as exc:  # a store that did not answer, or the engine's thread gone (its space closes)
            log.warning("not connected %s: its conversation could not be read (%s)", path, type(exc).__name__)
            if exchange:
                exchange.response_end(error=type(exc).__name__)
            await websocket.close(code=CloseCode.INTERNAL_ERROR, reason=_UNREAD_CONVERSATION)
            return
        rewriter = WSResponseRewriter(mediator, session)

    try:
        up = await _upstream(url, headers, pinned)
    except InvalidStatus as exc:
        status = exc.response.status_code
        if redirected(status, exc.response.headers):  # a client that followed it would take its frames there unmasked
            log.warning("not connected %s: the upstream answered a redirect, which is not followed", path)
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

    assert up.response is not None  # set once the handshake succeeded
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
    turn: list[Outcome] = []
    response_headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in accept_headers]
    too_big: list[bool] = []  # the upstream sent a message larger than Latet reads
    flooded: list[bool] = []  # the upstream sent more frames for one of the client's than a stream has events

    def end_turn(error: str | None = None) -> None:
        if turn and recorder is not None:
            outcome = turn.pop()
            outcome.error = outcome.error or error
            recorder.finish(outcome)

    # On a server: the turn in flight, counted in its identity's requests in flight (the module's words). One at a time
    # for a socket: a frame sent while one is answered belongs to it.
    turns = websocket.scope.get("latet.turns")
    counted: list[bool] = []
    # The upstream's frames since the client last sent one: at most a stream's events (the module's words), each one
    # restored on the event loop. Counted afresh only when the client asks: an end of a turn the upstream writes
    # itself, as often as it likes, counts for nothing.
    answered = [0]

    def turn_over() -> None:
        if counted and turns is not None:
            counted.clear()
            turns.end()

    async def client_to_upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if turns is not None and not counted:  # a turn asked: counted, or refused with nothing of it sent
                    if (refused := turns.begin()) is not None:
                        error = {"type": "error", "error": {"type": "latet_busy", "message": refused}}
                        await websocket.send_text(json.dumps(error))
                        continue
                    counted.append(True)
                data = message.get("text")
                prepared: Prepared | None = None  # this frame's, once masked (or refused)
                started = time.time()
                if data is None:
                    data = message.get("bytes") or b""
                    if mediator is not None and session is not None:  # a server reads none: refused there
                        prepared = mediator.unread_frame("a binary frame", session)
                elif mediator is not None and session is not None:
                    data, prepared = await mediator.run(mediator.mask_ws_frame, data, session)
                    if prepared is not None and not prepared.block_reason:
                        masked = prepared.masked_count
                        if prepared.reviews:  # the local reviewer reads it first
                            prepared = await mediator.review_prepared(prepared)
                        if not prepared.block_reason:  # then you, when the Live Reviewer is on
                            prepared = await mediator.live_prepared(prepared, agent_of(lowered, path)[0])
                        # Written anew on any change they made, an image withheld or covered with no text masked as
                        # well: as a body over HTTP, which leaves as ``prepared.body`` holds it.
                        if not prepared.block_reason and (prepared.amended or prepared.masked_count != masked):
                            data = dumps_compact(prepared.document)
                if prepared is not None and prepared.block_reason:
                    log.warning("blocked a WebSocket turn: %s", prepared.block_reason)
                    if recording and recorder is not None:  # on the dashboard as a request stopped over HTTP is
                        # Over at once, never in `turn`: the turn answering goes on, this frame never left.
                        recorder.finish(
                            Outcome(
                                lowered,
                                upstream.name,
                                path,
                                "ws",
                                started,
                                prepared,
                                400,
                                request_headers=headers,
                                body_bytes=len(data),
                            )
                        )
                    told = stopped(prepared.block_reason, tutorial)  # as a request over HTTP is told
                    error = {"type": "error", "error": {"type": "latet_blocked", "message": told}}
                    turn_over()  # nothing will answer it
                    await websocket.send_text(json.dumps(error))
                    continue
                if recording and prepared is not None:  # a turn the mediator read, sent
                    end_turn(error="superseded")  # a new turn before the previous one ended
                    turn.append(
                        Outcome(
                            lowered,
                            upstream.name,
                            path,
                            "ws",
                            started,
                            prepared,
                            101,
                            time.time(),
                            capture=ResponseCapture("responses"),
                            request_headers=headers,
                            response_headers=response_headers,
                            body_bytes=len(data),
                        )
                    )
                if exchange:
                    exchange.ws("up", data)
                answered[0] = 0  # a turn asked: its answer counted from here
                await up.send(data)
                if mediator is not None and prepared is not None:  # it reached the upstream: what it carried left
                    mediator.left(prepared)
        except (WebSocketDisconnect, ConnectionClosed):
            pass

    async def upstream_to_client(conn: ClientConnection, ex: Exchange | None) -> None:
        try:
            async for data in conn:
                answered[0] += 1
                if answered[0] > sse.MAX_EVENTS:  # stopped: an error frame, and both connections close
                    flooded.append(True)
                    log.warning("stopped a WebSocket turn's answer: %s", TooManyEvents.__name__)
                    end_turn(error=TooManyEvents.__name__)
                    told = marked(STOPPED_ANSWER.format(too_many(sse.MAX_EVENTS)))
                    error = {"type": "error", "error": {"type": "latet_blocked", "message": told}}
                    with contextlib.suppress(Exception):
                        await websocket.send_text(json.dumps(error))
                    conn.transport.abort()  # dropped at once: no closing handshake waits behind what it still sends
                    return
                if ex:
                    ex.ws("down", data)
                if isinstance(data, str):
                    if turn and turn[-1].capture is not None:
                        seen = turn[-1].capture
                        seen.event(data)
                        if seen.done:
                            end_turn(error="failed" if seen.failed else None)
                    if counted and _ends_turn(data):  # its place given back before its client reads the end
                        turn_over()
                    frames = await rewriter.restored(data) if rewriter is not None else [data]
                    for frame in frames:
                        await websocket.send_text(frame)
                else:
                    await websocket.send_bytes(data)
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
        asyncio.create_task(upstream_to_client(up, exchange), name="latet-ws-upstream"),
    }
    try:
        await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
    finally:
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
            log.warning("ended a WebSocket %s: %s", path, type(failed[0]).__name__)
        code = (
            CloseCode.MESSAGE_TOO_BIG
            if too_big
            else CloseCode.POLICY_VIOLATION
            if flooded
            else CloseCode.INTERNAL_ERROR
            if failed
            else (up.close_code or 1000)
        )
        if exchange:
            exchange.response_end(error=type(failed[0]).__name__ if failed else None)
        end_turn(error="closed")  # the connection ended mid-turn
        turn_over()
        with contextlib.suppress(Exception):
            await up.close()  # its close frame written before its first wait
        with contextlib.suppress(Exception):
            if failed:
                await websocket.close(code=code, reason=_CANNOT_GO_ON)
            else:
                await websocket.close(code=code if 1000 <= code < 5000 else 1000)
        await asyncio.gather(*legs, return_exceptions=True)  # both ended before the relay is
