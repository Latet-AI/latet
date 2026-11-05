"""The runtime send guard of the e2e and agent suites (RC-REC-8, R-87; P1-3 of
``notes/server-request-coverage-plan.md``), behind the AST walk of ``tests/unit/test_request_send_paths.py``.

While a test of those suites runs, every outbound HTTP request of Latet's made through httpx's own transport
(``httpx.AsyncHTTPTransport.handle_async_request``) and every WebSocket handshake of Latet's made by websockets' client
must carry ``latet.gateway.request_transport``'s token: the very request ``validate_send`` authorized and the transport
is sending now (``request_transport.sending()``), never another made meanwhile. One made without it is refused before a
byte of it is written (``UnguardedSend``), and fails the test it ran in, whatever the test made of the error.

What is Latet's, decided two ways:
- by its client: a request through a transport of an ``httpx.AsyncClient`` made while a frame of Latet's was on the
  stack (``make_client``, the gateway's own clients, whatever a test hands the gateway to make them with) needs the
  token whoever runs it, a task of its own or ``gather`` included, where no frame of Latet's is left on the stack;
- by the stack, for any other request: the innermost frame of a module under ``latet.``.

A test's own clients (an SDK, a ``TestClient``, the test's httpx client) are not Latet's, and pass. So does the one
reviewed non-provider caller that reaches the network through these transports, the local reviewer
(``latet.engine.reviewer``), and its clients; a proxy's CONNECT, which httpcore writes before the request (P1-0 D-10),
is part of the transport. A WebSocket handshake of Latet's passes only as the transport's WebSocket adapter opens it
(S3, RC-REC-9): its connection carries the very handshake ``validate_connect`` authorized and the adapter is opening now
(``request_transport.connecting()``); no handshake is allowed by name. A daemon run in a process of its own is outside
this guard (the e2e daemons run in this one)."""

from __future__ import annotations

import contextlib
import sys
import threading
import weakref
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

import httpx
import pytest
from websockets.asyncio import client as ws_async
from websockets.sync import client as ws_sync

from latet.gateway import request_transport

_NOT_PROVIDERS = ("latet.engine.reviewer.",)  # the local reviewer: the owner's own model, by the owner's configuration


class UnguardedSend(AssertionError):
    """A connection Latet's code made without the transport's token."""


@dataclass
class SendGuard:
    """The guard's state: whether it checks now (while a test of the suites runs), the transports of the clients
    Latet's code made, and what it refused."""

    checking: bool = False
    refused: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    latets: weakref.WeakSet[Any] = field(default_factory=weakref.WeakSet)

    def made(self, client: httpx.AsyncClient, frame: FrameType | None) -> None:
        """A client just made: its transports Latet's when a frame of Latet's (not the local reviewer's) made it."""
        origin = _origin(frame)
        if origin is None or origin[0].startswith(_NOT_PROVIDERS):
            return
        transports = [client._transport, *client._mounts.values()]
        with self.lock:
            for transport in transports:
                if transport is not None:
                    self.latets.add(transport)

    def check(self, kind: str, request: Any, frame: FrameType | None, transport: Any = None) -> None:
        if not self.checking:
            return
        with self.lock:
            latets = transport is not None and transport in self.latets
        origin = _origin(frame)
        if not latets and (origin is None or origin[0].startswith(_NOT_PROVIDERS)):
            return
        if kind == "http" and request is not None and request_transport.sending() is request:
            return
        if kind == "ws" and request is not None:  # the connection whose handshake the adapter is opening, authorized
            token = request_transport.connecting()
            if token is not None and getattr(request, "authorized", None) is token:
                return
        if latets or origin is None:
            message = f"{kind} request of a client of Latet's without request_transport's token"
        else:
            message = f"{kind} connection from {origin[0]}.{origin[1]} without request_transport's token"
        with self.lock:
            self.refused.append(message)
        raise UnguardedSend(message)


def _origin(frame: FrameType | None) -> tuple[str, str] | None:
    """The innermost frame of Latet's code on the stack: its module and function; None when there is none."""
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if isinstance(module, str) and (module == "latet" or module.startswith("latet.")):
            return module, frame.f_code.co_name
        frame = frame.f_back
    return None


@contextlib.contextmanager
def installed(guard: SendGuard) -> Iterator[SendGuard]:
    """``guard`` wrapped around httpx's async client and transport and websockets' two clients' handshakes, while it is
    open."""
    init = httpx.AsyncClient.__init__
    http = httpx.AsyncHTTPTransport.handle_async_request
    shake_async = ws_async.ClientConnection.handshake
    shake_sync = ws_sync.ClientConnection.handshake

    def made(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        guard.made(self, sys._getframe(1))

    async def handle(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        guard.check("http", request, sys._getframe(1), self)
        return await http(self, request)

    async def handshake_async(self: Any, *args: Any, **kwargs: Any) -> None:
        guard.check("ws", self, sys._getframe(1))
        return await shake_async(self, *args, **kwargs)

    def handshake_sync(self: Any, *args: Any, **kwargs: Any) -> None:
        guard.check("ws", self, sys._getframe(1))
        return shake_sync(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = made  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = handle  # type: ignore[method-assign]
    ws_async.ClientConnection.handshake = handshake_async  # type: ignore[method-assign]
    ws_sync.ClientConnection.handshake = handshake_sync  # type: ignore[method-assign]
    try:
        yield guard
    finally:
        httpx.AsyncClient.__init__ = init  # type: ignore[method-assign]
        httpx.AsyncHTTPTransport.handle_async_request = http  # type: ignore[method-assign]
        ws_async.ClientConnection.handshake = shake_async  # type: ignore[method-assign]
        ws_sync.ClientConnection.handshake = shake_sync  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def send_guard() -> Iterator[SendGuard]:
    """The guard, installed once for the session (it checks only while a test of the suites runs)."""
    with installed(SendGuard()) as guard:
        yield guard


@pytest.fixture(autouse=True)
def no_unguarded_send(send_guard: SendGuard) -> Iterator[None]:
    """Each test of the suites: no connection of Latet's without the transport's token."""
    before = len(send_guard.refused)
    send_guard.checking = True
    try:
        yield
    finally:
        send_guard.checking = False
    refused = send_guard.refused[before:]
    assert refused == [], f"a send without request_transport's token: {refused}"
