"""Access checks: a Host allowlist (DNS rebinding; on a Latet server, its own name), the refusal of requests made by
a web page on proxy routes, and on a server the bound of a request's body (``BodyLimit``)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_BROWSER_SITES = (b"cross-site", b"same-site")


def local_hosts(port: int) -> frozenset[str]:
    """The Host of a request to this machine on ``port``, with the port or without."""
    return frozenset({*_LOCAL_HOSTS, *(f"{host}:{port}" for host in _LOCAL_HOSTS)})


def url_hosts(url: str) -> frozenset[str]:
    """The Host of a request to ``url``: its host, with its port (the default one too) or without."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    host = f"[{host}]" if ":" in host else host
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return frozenset({host, f"{host}:{port}"})


def host_of(scope: Scope) -> str:
    return dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1").lower()


def from_browser(scope: Scope) -> bool:
    """Made by a web page: coding agents never send Origin, and browsers always do on cross-origin requests and
    WebSocket handshakes."""
    headers = dict(scope.get("headers") or [])
    return b"origin" in headers or headers.get(b"sec-fetch-site", b"") in _BROWSER_SITES


async def refuse(
    scope: Scope, send: Send, status: int, reason: str, *, headers: Sequence[tuple[bytes, bytes]] = ()
) -> None:
    """Answer ``status`` with Latet's reason before any app sees the request, with ``headers`` beside its own (a
    WebSocket is closed)."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4000 + status, "reason": reason})
        return
    body = json.dumps({"error": f"latet: {reason}"}, separators=(",", ":")).encode()
    answered = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *headers]
    await send({"type": "http.response.start", "status": status, "headers": answered})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Refuses a request whose body is larger than ``limit`` bytes before more of it than ``limit`` is buffered (on a
    server, ``max_request_mb``: Ruling 29, R-4). A ``content-length`` over the limit answers 413 at once, before the app
    sees the request. A body that grows past it while the app reads it is answered 413 here, when the app has sent
    nothing yet (else the connection ends with what was sent), and the app is told that its client is gone
    (``http.disconnect``, from then on): the chunk that passed the limit is never handed over, what the app sends of a
    body cut short goes nowhere, and what it raises of it is swallowed (the app's own error answer, a 400 or a 500, must
    not take the place of the 413). Nothing of the body is kept here. A WebSocket passes untouched: its messages are
    bounded by the server's ``ws_max_size``. ``narrower``: the paths under which a lower limit applies, each with it and
    the setting that names it (on a server, the applications API's ``apps_request_mb``)."""

    def __init__(self, app: ASGIApp, limit: int, narrower: tuple[tuple[str, int, str], ...] = ()) -> None:
        self.app, self.limit, self.narrower = app, limit, narrower

    def _bound(self, path: str) -> tuple[int, str]:
        """The limit of a request to ``path``, and the setting that names it."""
        limit, named = self.limit, "max_request_mb"
        for prefix, lower, setting in self.narrower:
            if (path == prefix or path.startswith(f"{prefix}/")) and lower < limit:
                limit, named = lower, setting
        return limit, named

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, named = self._bound(scope.get("path") or "")
        reason = f"the request body is larger than {limit} bytes, the most this server takes ({named})"
        if _declared(scope) > limit:
            await refuse(scope, send, 413, reason)
            return
        seen, started, refused = 0, False, False

        async def limited() -> Message:
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:  # answered here, and the app told its client is gone: it reads no more
                    refused = True
                    if not started:
                        await refuse(scope, send, 413, reason)
                    return {"type": "http.disconnect"}
            return message

        async def sending(message: Message) -> None:
            nonlocal started
            if refused:
                return  # answered already: what the app says of a body cut short goes nowhere
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited, sending)
        except Exception:
            if not refused:
                raise


def _declared(scope: Scope) -> int:
    """What a request's ``content-length`` says, 0 when it says nothing that is a number: what comes is counted all
    the same."""
    try:
        return int(dict(scope.get("headers") or []).get(b"content-length") or 0)
    except ValueError:
        return 0


class LocalOnlyMiddleware:
    """Rejects requests whose Host is not this machine (on a Latet server: the server's, ``hosts``), and proxy requests
    made by a web page. The dashboard (``/_latet``) has its own session and CSRF checks."""

    def __init__(self, app: ASGIApp, port: int, hosts: frozenset[str] | None = None) -> None:
        self.app = app
        self.hosts = hosts if hosts is not None else local_hosts(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
        elif host_of(scope) not in self.hosts:
            await refuse(scope, send, 403, "host not allowed")
        elif not scope["path"].startswith("/_latet") and from_browser(scope):
            await refuse(scope, send, 403, "browser requests are not accepted on proxy routes")
        else:
            await self.app(scope, receive, send)
