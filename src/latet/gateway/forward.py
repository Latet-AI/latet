"""HTTP forwarding to the upstream provider, streaming the response back unbuffered (an answer restored once it is
whole is read first, no further than Latet reads it decoded)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

import httpx
import truststore
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from latet.engine.vault import MaskingError
from latet.gateway.aws import AwsCredentialStore, AwsError, profile_tag
from latet.gateway.capture import Capture, Exchange
from latet.gateway.cloud import Signing
from latet.gateway.headers import DROP_PREFIXES, REQUEST_DROP, RESPONSE_DROP, forwardable
from latet.gateway.mediate import REWRITE, STOPPED_ANSWER, Mediator, Prepared, ResponseTransform
from latet.gateway.recorder import Outcome, Recorder, ResponseCapture, agent_of
from latet.gateway.routes import RouteTable, Upstream, readable_path, wire_path
from latet.gateway.sigv4 import AwsCredentials, sign
from latet.protocols.codec import DecodedTooLarge
from latet.protocols.sse import ResponseError
from latet.server.egress import EgressRefused

log = logging.getLogger(__name__)

# Headers httpx adds on its own; dropped again unless the client sent them, so the
# upstream sees the client's request as the client made it.
_CLIENT_DEFAULTS = ("accept", "accept-encoding", "user-agent")
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


@dataclass(slots=True)
class _Whole:
    """An answer restored once it is whole, and what it is recorded with as it is read."""

    response: httpx.Response
    exchange: Exchange | None
    seen: ResponseCapture | None
    headers: list[tuple[str, str]]


def make_client(*, trust_env: bool = True) -> httpx.AsyncClient:
    """One pooled client for every upstream, trusting the OS certificate store. It follows no redirect (httpx's default,
    kept on purpose): Latet never goes on to the address an answer names, inside a server's network say, and the agent
    is answered a clear error in place of the redirect, which it would follow with its request unmasked
    (``Forwarder._redirected``). ``trust_env`` False: no proxy and no credential of the environment's (a destination of
    an identity's own, which a server reaches at the address it checked)."""
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.AsyncClient(
        verify=context,
        timeout=httpx.Timeout(connect=20.0, read=600.0, write=120.0, pool=60.0),
        follow_redirects=False,
        trust_env=trust_env,
    )


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


def error_shape(upstream: Upstream, prepared: Prepared | None) -> ErrorShape:
    """The shape this request's client reads: Anthropic's for the Messages API, whatever the route, with
    Bedrock's own field for a Bedrock call; Google's for the Gemini API and for Google's calls on Vertex (a request
    Latet read as another's than Claude's: Claude Code reads Anthropic's)."""
    if upstream.name == "bedrock" or (prepared is not None and prepared.shape in ("bedrock", "bedrock-count")):
        return "bedrock"
    protocol = prepared.protocol if prepared is not None else None
    if upstream.name == "gemini" or protocol == "gemini":
        return "google"
    if upstream.name == "vertex" and prepared is not None and protocol != "anthropic":
        return "google"
    return "anthropic" if protocol == "anthropic" or upstream.name in ("anthropic", "vertex") else "openai"


_RECORDED = ("anthropic", "chat", "responses", "gemini")  # protocols whose responses the dashboard shows
# How the error of a stopped request ends: where to see it. In the tutorial's sandbox, its own page: `latet
# dashboard` would open the user's own dashboard, where the sandbox's requests are not.
_SEE = ". Nothing was sent; run `latet dashboard` to see where it was"
_SEE_IN_TUTORIAL = ". Nothing was sent; see where it was in Latet's pane"


def stopped(reason: str, tutorial: bool = False) -> str:
    """What an agent is told of a request Latet stopped, over HTTP or a WebSocket: Latet named once, why, that
    nothing was sent, and where to see it (in the tutorial's sandbox, its own pane)."""
    return marked(reason + (_SEE_IN_TUTORIAL if tutorial else _SEE))


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
    ) -> None:
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
    ) -> Prepared | Response:
        """The request masked (``Mediator.prepare``); or the answer that stops it, when the vault is at its limit or no
        fake is free and ``prepare`` lets it through, or when its body would decode past a server's ``max_request_mb``:
        never sent unmasked, nor decoded further."""
        assert self.mediator is not None
        try:
            return await self.mediator.run(self.mediator.prepare, upstream, request.method, read, lowered, body)
        except DecodedTooLarge as error:  # a few KiB that would inflate past the limit: stopped (its words, the limit)
            log.warning("stopped %s %s: %s", request.method, path, type(error).__name__)
            self._record(outcome, status=413, error=type(error).__name__)
            reason = f"{error}, the most this server takes (max_request_mb)"
            return upstream_error(stopped(reason, self.tutorial), 413, shape=error_shape(upstream, None))
        except MaskingError as error:  # VaultFull, or no fake free: stopped, and said so (its words name no value)
            log.warning("stopped %s %s: %s", request.method, path, type(error).__name__)
            self._record(outcome, status=400, error=type(error).__name__)
            return upstream_error(
                stopped(f"masking failed: {error}", self.tutorial), 400, shape=error_shape(upstream, None)
            )

    def _outgoing(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
        signer: tuple[AwsCredentials, str] | None,
        host: str | None = None,
        extensions: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Request:
        """The request as it goes out, built by the client that sends it. ``host``: the Host header to send (a
        destination's name, while ``url`` carries the address checked), with ``extensions`` for httpx (TLS's name)."""
        outgoing = (client or self.client).build_request(
            method, url, headers=headers, content=body, extensions=extensions or {}
        )
        if host is not None:
            outgoing.headers["host"] = host
        sent = {k.lower() for k, _ in headers}
        for name in _CLIENT_DEFAULTS:
            if name not in sent and name in outgoing.headers:
                del outgoing.headers[name]
        if signer is not None:  # the URL as it goes out, percent-escapes kept, and the final body
            at = f"{outgoing.url.scheme}://{outgoing.url.netloc.decode('ascii')}{outgoing.url.raw_path.decode('ascii')}"
            for name, value in sign(method, at, outgoing.content, signer[0], signer[1]):
                outgoing.headers[name] = value
        return outgoing

    async def handle(self, request: Request) -> Response:
        wire = wire_path(request.scope)
        upstream = self.routes.match(wire)
        if upstream is None:
            return JSONResponse({"error": "no upstream for this path"}, status_code=404)

        resolved = upstream.resolve(request.method, wire, request.url.query)
        if resolved is None:  # a cloud's call Latet cannot read (Bedrock's Converse API, another publisher)
            text = "Latet does not read this kind of request, so it does not send it"
            return upstream_error(text, 404, shape=error_shape(upstream, None))
        signing: Signing | None = resolved.signing
        if signing is not None and not self._may_sign(signing):
            return upstream_error("there is no such route", 404, shape="bedrock")
        url = resolved.url
        path = resolved.path or request.url.path  # what records and logs show: never a signing key
        # Before anything of the request is read: nothing is done, or recorded, of one that will not leave.
        route = await self._route(upstream, url, request, path, None)
        if isinstance(route, Response):
            return route
        url, host_header, extensions = route
        client = self.direct if host_header is not None and self.direct is not None else self.client
        incoming = _raw_headers(request)
        lowered = {k.lower(): v for k, v in incoming}
        started = time.time()
        body = await request.body()
        outcome = Outcome(lowered, upstream.name, path, "http", started) if self.recorder else None
        if outcome is not None:
            outcome.host = resolved.host
            if signing is not None:
                outcome.signed = f"aws-profile:{signing.profile}" if signing.profile else "aws-session"

        prepared: Prepared | None = None
        if self.mediator is not None:
            read = readable_path(wire)  # what a server would make of it: `complet%69ons` is `completions`
            masked = await self._prepared(upstream, request, read, lowered, body, path, outcome)
            if isinstance(masked, Response):
                return masked
            prepared = masked
            if prepared.reviews and not prepared.block_reason:  # the local reviewer reads it first
                prepared = await self.mediator.review_prepared(prepared)
            if not prepared.block_reason:  # then you, when the Live Reviewer is on
                agent = agent_of(lowered, request.url.path)[0]
                prepared = await self.mediator.live_prepared(prepared, agent, request.is_disconnected)
            if outcome is not None:
                outcome.prepared = prepared
                outcome.body_bytes = len(body)
            if prepared.block_reason:
                log.warning("blocked %s %s: %s", request.method, path, prepared.block_reason)
                self._record(outcome, status=400)
                text = stopped(prepared.block_reason, self.tutorial)
                return upstream_error(text, 400, shape=error_shape(upstream, prepared))
            body = prepared.body

        signer: tuple[AwsCredentials, str] | None = None
        if signing is not None:  # read before anything is built: a login that ended sends nothing
            try:
                signer = (await self._credentials(signing), signing.region)
            except AwsError as error:
                log.warning("not signed %s %s: %s", request.method, path, error.code)
                if prepared is not None:  # the dashboard says why, in plain words
                    prepared.block_reason = str(error)
                self._record(outcome, status=400, error=error.code)
                return upstream_error(str(error) + ". Nothing was sent", 400, shape="bedrock")
        headers = forwardable(incoming, REQUEST_DROP, DROP_PREFIXES)
        if signer is not None:  # the client's own authorization goes: Latet signs instead
            headers = [
                (k, v) for k, v in headers if k.lower() != "authorization" and not k.lower().startswith("x-amz-")
            ]
        if outcome is not None:
            outcome.request_headers = headers
            outcome.body_bytes = len(body)
        exchange = self.capture.exchange("http", upstream.name, request.method, url)
        if exchange:
            exchange.request(headers, body, lowered.get("content-encoding"))

        outgoing = self._outgoing(request.method, url, headers, body, signer, host_header, extensions, client)

        if outcome is not None:
            outcome.forwarded = time.time()
        try:
            response = await client.send(outgoing, stream=True)
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
                    return upstream_error(str(error) + ". Nothing was sent", 400, shape="bedrock")
                again = self._outgoing(request.method, url, headers, body, signer, host_header, extensions, client)
                response = await client.send(again, stream=True)
        except httpx.HTTPError as exc:
            if exchange:
                exchange.response_end(error=repr(exc))
            self._record(outcome, status=502, error=type(exc).__name__)
            unreachable = f"upstream {upstream.name} unreachable ({type(exc).__name__})"
            return upstream_error(unreachable, 502, shape=error_shape(upstream, prepared))
        if self.mediator is not None and prepared is not None:  # it reached its provider: what it carried left
            self.mediator.left(prepared)
        if redirected(response.status_code, response.headers):  # neither followed nor passed on
            return await self._redirected(response, exchange, request.method, path, upstream, prepared, outcome)

        transform = None
        if prepared is not None and prepared.mode == REWRITE and response.status_code < 400 and self.mediator:
            transform = self.mediator.response_transform(
                prepared, response.headers.get("content-type", ""), response.headers.get("content-encoding")
            )
        # What the model writes in an answer passed on as it came (Claude Code's), read before each chunk goes on.
        written: Callable[[bytes | None], Awaitable[None]] | None = None
        if prepared is not None and prepared.session is not None and response.status_code < 400 and self.mediator:
            capture = self.mediator.written(
                prepared, response.headers.get("content-type", ""), response.headers.get("content-encoding")
            )
            if capture is not None:
                written = partial(self.mediator.read_written, capture, prepared.session)
        drop = RESPONSE_DROP | ({"content-encoding"} if transform is not None else frozenset())
        response_headers = forwardable(response.headers.multi_items(), drop)
        if exchange:
            exchange.response_start(response.status_code, forwardable(response.headers.multi_items(), RESPONSE_DROP))
        seen: ResponseCapture | None = None
        if outcome is not None:
            outcome.status = response.status_code
            outcome.response_headers = forwardable(response.headers.multi_items(), RESPONSE_DROP)
            if prepared is not None and prepared.protocol in _RECORDED:
                seen = outcome.capture = ResponseCapture(
                    prepared.protocol,
                    response.headers.get("content-type", ""),
                    response.headers.get("content-encoding"),
                )
        if transform is not None and not transform.streams:
            whole = _Whole(response, exchange, seen, response_headers)
            return await self._restored_whole(whole, transform, request.method, path, upstream, prepared, outcome)

        async def relay() -> AsyncIterator[bytes]:
            error: str | None = None
            try:
                async for chunk in response.aiter_raw():
                    if exchange:
                        exchange.response_chunk(chunk)
                    if seen is not None:
                        seen.feed(chunk)
                    if written is not None:
                        await written(chunk)  # before it is passed on
                    if transform is None:
                        yield chunk
                        continue
                    async for out in transform.restored(chunk):
                        if out:
                            yield out
                        await asyncio.sleep(0)  # what one chunk decodes to is restored in turns with the others' work
                    if transform.stopped is not None:
                        break
                if written is not None:
                    await written(None)
                if transform is not None:
                    tail = await transform.restored_end()
                    if tail:
                        yield tail
                    if (
                        transform.stopped is not None
                    ):  # the answer, cut at what Latet reads: said, and not recorded whole
                        error = transform.stopped
                        log.warning("stopped the answer to %s %s: %s", request.method, path, error)
            except BaseException as exc:  # includes the client going away (cancellation)
                error = type(exc).__name__
                raise
            finally:
                if transform is not None:
                    transform.close()
                await response.aclose()
                if exchange:
                    exchange.response_end(error=error)
                if seen is not None:
                    seen.end()
                self._record(outcome, error=error)

        streamed = StreamingResponse(relay(), status_code=response.status_code)
        streamed.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response_headers]
        return streamed

    async def _redirected(
        self,
        response: httpx.Response,
        exchange: Exchange | None,
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
        await response.aclose()
        log.warning("stopped the answer to %s %s: a redirect (%d), which is not followed", method, path, status)
        if exchange:
            exchange.response_end(error="redirect")
        self._record(outcome, status=502, error="redirect")
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
        try:
            async for chunk in response.aiter_raw():
                if whole.exchange:
                    whole.exchange.response_chunk(chunk)
                if whole.seen is not None:
                    whole.seen.feed(chunk)
                async for _ in transform.restored(chunk):  # held: nothing goes out yet
                    await asyncio.sleep(0)  # what one chunk decodes to is held in turns with the others' work
            body = await transform.restored_end()
        except ResponseError as refused:
            error = type(refused).__name__
            log.warning("stopped the answer to %s %s: %s", method, path, error)
            text = STOPPED_ANSWER.format(refused)
            return upstream_error(text, 502, shape=error_shape(upstream, prepared))
        except BaseException as exc:  # includes the client going away (cancellation)
            error = type(exc).__name__
            raise
        finally:
            await response.aclose()
            if whole.exchange:
                whole.exchange.response_end(error=error)
            if whole.seen is not None:
                whole.seen.end()
            self._record(outcome, status=502 if error in _REFUSED else None, error=error)
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
