"""The Latet gateway ASGI application."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from latet import __version__
from latet.gateway import ws
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.capture import Capture
from latet.gateway.cloud import CLOUD_UPSTREAMS
from latet.gateway.forward import Forwarder, make_client
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Recorder
from latet.gateway.routes import DEFAULT_UPSTREAMS, RouteTable, Upstream
from latet.gateway.security import LocalOnlyMiddleware, url_hosts
from latet.server.egress import EgressRefused
from latet.server.shared import personal_destination_problem

DEFAULT_PORT = 5283
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_OWN = "/_latet"  # Latet's own paths: no destination's (a destination's name begins with a letter or a digit)

Pin = Callable[[str], Awaitable[tuple[str, str, dict[str, Any]]]]


@dataclass(frozen=True)
class OnServer:
    """An identity's space on a Latet server: the server's address (the Host its requests name), and the identity, the
    only one whose requests the server hands to this app (``scope["latet.identity"]``)."""

    url: str
    identity: str


@dataclass
class GatewayConfig:
    port: int = DEFAULT_PORT
    upstreams: list[Upstream] = field(default_factory=lambda: [*DEFAULT_UPSTREAMS, *CLOUD_UPSTREAMS])
    capture_dir: Path | None = None
    mediator: Mediator | None = None
    bridge_token: str | None = None
    recorder: Recorder | None = None
    dashboard: Any = None  # latet.api.rest.Dashboard: its routes() are mounted under /_latet
    unreadable: int = 0  # vault values this root key cannot open (another key's): `latet doctor`
    destinations: list[Upstream] = field(default_factory=list)  # your settings' `upstreams`
    aws: AwsCredentialStore | None = None  # credentials for the Bedrock requests Latet signs
    # Makes the client the upstreams are reached with (None: make_client's); the tutorial's gives its pretend model.
    client: Callable[[], httpx.AsyncClient] | None = None
    # The only routes, whatever the settings say (no destination is added to them): the tutorial's pretend model.
    fixed_routes: list[Upstream] | None = None
    tutorial: bool = False  # the tutorial's sandbox: its health answer says so, and a stopped request's error
    routes: RouteTable | None = None  # set by create_app: the table the running app routes by
    on_server: OnServer | None = None  # an identity's space on a Latet server
    # On a server, the destinations of the identity's own, the shared ones being the administrator's: their address is
    # checked again when the server connects to them.
    personal_names: set[str] = field(default_factory=set)
    # How: ``latet.server.egress.pin`` on a server (the identity's lookups counted for it), None on a computer.
    pin: Pin | None = None
    # Where a computer's two calls do their work on a server (the user name's fake, a destination its agent names): the
    # server's work threads (``latet.server.machines.in_work``, which raises for whatever the work raised), handed over
    # by the server as it builds the space; None on a computer, whose daemon takes a worker thread.
    work: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None


def _checking(pin: Pin | None, on_server: OnServer | None) -> Pin | None:
    """``pin`` judging first what a destination's address says by itself (in clear, the server itself by its machine's
    names or by its own address, an address that cannot be used): what the forwarder and the WebSocket relay connect to
    for a destination of the identity's own. Raises EgressRefused: nothing is sent. None on a computer."""
    if pin is None:
        return None
    server = on_server.url if on_server is not None else None

    async def checked(url: str) -> tuple[str, str, dict[str, Any]]:
        if (problem := personal_destination_problem(url, server=server)) is not None:
            try:
                host = urlsplit(url).hostname or "its address"
            except ValueError:
                host = "its address"
            raise EgressRefused(f"{host}: {problem}")
        return await pin(url)

    return checked


def create_app(config: GatewayConfig | None = None) -> Starlette:
    config = config or GatewayConfig()
    on_server = config.on_server
    if config.mediator is not None and on_server is not None:  # what it does not read, it refuses (Mediator.on_server)
        config.mediator.on_server = True
    fixed = config.fixed_routes
    routes = config.routes = RouteTable(fixed if fixed is not None else [*config.upstreams, *config.destinations])
    capture = Capture(config.capture_dir)
    state: dict[str, Forwarder] = {}
    pin = _checking(config.pin, on_server)

    def personal() -> set[str]:  # read at each request: the settings change
        return config.personal_names

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if config.mediator is not None:  # known from the start: what a worker thread tells the dashboards goes there
            config.mediator.loop = asyncio.get_running_loop()
        async with AsyncExitStack() as clients:
            client = await clients.enter_async_context((config.client or make_client)())
            # A destination of the identity's own: a client of its own, which takes no proxy and no credential of the
            # server's environment, follows no redirect, and pools its connections apart from the shared destinations'
            # (one app, and so one client, for each identity: a connection never serves another identity).
            direct = None
            if pin is not None:
                own = config.client() if config.client is not None else make_client(trust_env=False)
                direct = await clients.enter_async_context(own)
            state["forwarder"] = Forwarder(
                routes, capture, client, config.mediator, config.recorder, config.aws,
                config.bridge_token if on_server is None else None,  # a server's AWS profiles are not its identities'
                tutorial=config.tutorial, pin=pin, personal=personal, direct=direct,
            )  # fmt: skip
            pruning: asyncio.Task[None] | None = None
            if config.recorder is not None and config.recorder.enabled:  # a history is also pruned by the clock
                pruning = asyncio.create_task(config.recorder.prune_when_due(), name="latet-prune")
            try:
                yield
            finally:
                if pruning is not None:
                    pruning.cancel()
                    with suppress(asyncio.CancelledError):
                        await pruning
        if config.mediator is not None:
            await config.mediator.close_retired()
            if config.mediator.reviewer is not None:
                await config.mediator.reviewer.aclose()
        if config.recorder is not None:
            if on_server is not None:  # one space of many closing: the event loop serves the others meanwhile
                await asyncio.to_thread(config.recorder.drain, 5)
            else:
                config.recorder.drain(timeout=5)

    async def health(_: Request) -> Response:
        answer: dict[str, Any] = {
            "name": "latet",
            "version": __version__,
            "capture": capture.enabled,
            "masking": config.mediator is not None,
            # The Live Reviewer's queue: how many batches wait, and how many dashboards watch them.
            "live": {
                "waiting": len(config.mediator.approvals.batches),
                "viewers": config.mediator.approvals.viewers,
            }
            if config.mediator is not None
            else None,
            "vault": {"unreadable": config.unreadable},
        }
        if config.tutorial:  # the sign-in page says to start `latet tutorial` again, not `latet dashboard`
            answer["tutorial"] = True
        # Whether this install serves its own documentation: the sign-in page, which cannot ask /me, opens it from here.
        answer["docs"] = config.dashboard is not None and config.dashboard.has_docs()
        return JSONResponse(answer)

    # Under /_latet only Latet's own routes answer: what reaches the catch-all routes there (a method or a WebSocket no
    # route of Latet's serves) is never proxied nor relayed, before anything of it is read, on a computer as on a
    # server.
    async def proxy(request: Request) -> Response:
        if request.scope["path"].startswith(_OWN):
            return JSONResponse({"error": "there is no such route"}, status_code=404)
        return await state["forwarder"].handle(request)

    async def proxy_ws(websocket: WebSocket) -> None:
        if websocket.scope["path"].startswith(_OWN):
            await websocket.close(code=4404)
            return
        await ws.relay(
            websocket, routes, capture, config.mediator, config.recorder, config.tutorial, pin=pin, personal=personal
        )

    bridge_routes: list[Route] = []
    if config.mediator is not None and config.bridge_token:
        from latet.bridge.api import Bridge

        bridge = Bridge(config.mediator, config.bridge_token, config.recorder, config.aws, on_server)
        bridge_routes = bridge.routes()

    async def shutdown(request: Request) -> Response:
        import hmac

        token = config.bridge_token or ""
        given = request.headers.get("x-latet-token", "")
        if not token or "origin" in request.headers or not hmac.compare_digest(given, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        server = getattr(request.app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return JSONResponse({"stopping": True})

    shutdown_route = [Route("/_latet/admin/shutdown", shutdown, methods=["POST"])] if on_server is None else []
    hosts = url_hosts(on_server.url) if on_server is not None else None
    return Starlette(
        routes=[
            Route("/_latet/health", health, methods=["GET"]),
            *bridge_routes,
            *(config.dashboard.routes() if config.dashboard is not None else []),
            *shutdown_route,
            WebSocketRoute("/{path:path}", proxy_ws),
            Route("/{path:path}", proxy, methods=_METHODS),
        ],
        middleware=[Middleware(LocalOnlyMiddleware, port=config.port, hosts=hosts)],
        lifespan=lifespan,
    )
