"""One physically owned authenticated launch-control connection at a time."""

from __future__ import annotations

import contextlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from latet.launch_session import LaunchControlUnavailable

_ANSWER_MOST = 1024 * 1024  # existing wrappers control-answer ceiling
_BODY_MOST = 8192  # existing launch-ingress body ceiling
_ACK_MOST = 1024  # existing fixed launch identifier envelope
_CONTROL = re.compile(r"/_latet/engine/v1/launches(?:/(?:prepare|recover|[0-9a-f]{32}/(?:hold|end|exit)))?")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, value in items:
        if name in found:
            raise LaunchControlUnavailable()
        found[name] = value
    return found


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise LaunchControlUnavailable() from None
    if type(value) is not dict:
        raise LaunchControlUnavailable()
    return value


class _Exchange:
    def __init__(self, owner: LaunchWire, connection: http.client.HTTPConnection) -> None:
        self.owner, self.connection = owner, connection
        self.response: http.client.HTTPResponse | None = None
        self.socket: socket.socket | None = None
        self.lock = threading.Lock()
        self.cancelled = False
        self.finished = False
        self.settled = threading.Event()
        self.deadline = time.monotonic() + owner._deadline
        self._timed = True
        self._deadline_stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def begin(self) -> None:
        self._watcher = threading.Thread(target=self._watch, name="latet-launch-deadline", daemon=False)
        self._watcher.start()

    def _watch(self) -> None:
        if self._deadline_stop.wait(max(0.0, self.deadline - time.monotonic())):
            return
        with self.lock:
            if not self._timed:
                return
            self.cancelled = True
            held = self.socket
        if held is not None:
            with contextlib.suppress(OSError):
                held.shutdown(socket.SHUT_RDWR)

    def _join_deadline(self) -> bool:
        self._deadline_stop.set()
        watcher = self._watcher
        interrupted = False
        if watcher is not None and watcher.ident is not None:
            while True:
                try:
                    watcher.join()
                    break
                except KeyboardInterrupt:
                    interrupted = True
        return interrupted

    def promote_hold(self) -> None:
        with self.lock:
            if self.cancelled or time.monotonic() >= self.deadline:
                raise LaunchControlUnavailable()
            self._timed = False
        if self._join_deadline():
            raise KeyboardInterrupt
        self.check()

    def connected(self) -> None:
        with self.lock:
            self.socket = self.connection.sock
        self.check()

    def check(self) -> None:
        with self.lock:
            expired = self._timed and time.monotonic() >= self.deadline
            if expired:
                self.cancelled = True
            cancelled = self.cancelled
        if cancelled:
            self.cancel()
            raise LaunchControlUnavailable()

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            held = self.socket
        if held is not None:
            with contextlib.suppress(OSError):
                held.shutdown(socket.SHUT_RDWR)
        # Never close a buffered reader on a second thread or detach this owner.
        # If native connect has not returned, connected() refuses before request.

    def finish(self) -> None:
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self._timed = False
        interrupted = self._join_deadline()
        try:
            if self.response is not None:
                self.response.close()
        finally:
            try:
                self.connection.close()
            finally:
                with self.owner._lock:
                    if self.owner._active is self:
                        self.owner._active = None
                self.settled.set()
        if interrupted:
            raise KeyboardInterrupt


class _Held:
    def __init__(self, exchange: _Exchange, generation: int) -> None:
        self.generation = generation
        self._exchange = exchange
        self._lock = threading.Lock()
        self._closed = False
        self._reading = False

    def wait(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._reading:
                raise LaunchControlUnavailable()
            self._reading = True
        try:
            with contextlib.suppress(OSError, http.client.HTTPException):
                answer = self._exchange.response
                assert answer is not None
                while answer.read(1):
                    self._exchange.check()
        finally:
            with self._lock:
                self._closed = True
            self._exchange.finish()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            reading = self._reading
        self._exchange.cancel()
        if not reading:
            self._exchange.finish()

    def drain(self) -> None:
        self._exchange.settled.wait()


class LaunchWire:
    """No retry, proxy, redirect, decompression or detached worker.

    A stalled native connect stays the ONE owner. Interrupt marks it cancelled;
    when it returns no request can be sent. Replacement waits actual finish.
    One joined deadline watcher interrupts Python header/ACK/body reads. Native
    DNS/connect is not a hard-wall claim; late completion cannot send bytes.
    """

    def __init__(
        self,
        base: str,
        token: str,
        context: ssl.SSLContext | None,
        selected: Callable[[], None],
        *,
        _deadline: float = 30.0,
    ) -> None:
        if not 0 < _deadline <= 30.0:
            raise LaunchControlUnavailable()
        self._deadline = _deadline
        parsed = urlsplit(base)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname != "127.0.0.1")
            or (parsed.scheme == "https" and context is None)
        ):
            raise LaunchControlUnavailable()
        self._base, self._token, self._context, self._selected = parsed, token, context, selected
        self._lock = threading.Lock()
        self._active: _Exchange | None = None

    def interrupt(self) -> None:
        with self._lock:
            active = self._active
        if active is not None:
            active.cancel()

    def _start(self, method: str, path: str, body: bytes | None, query: str = "") -> _Exchange:
        if _CONTROL.fullmatch(path) is None or method not in {"GET", "POST"}:
            raise LaunchControlUnavailable()
        self._selected()
        base = self._base
        connection = (
            http.client.HTTPSConnection(base.hostname or "", base.port or 443, timeout=30, context=self._context)
            if base.scheme == "https"
            else http.client.HTTPConnection(base.hostname or "", base.port or 80, timeout=30)
        )
        exchange = _Exchange(self, connection)
        with self._lock:
            if self._active is not None:
                raise LaunchControlUnavailable()
            self._active = exchange
        try:
            exchange.begin()
            exchange.check()
            connection.connect()
            exchange.connected()
            self._selected()
            exchange.check()
            connection.request(
                method,
                base.path.rstrip("/") + path + query,
                body=body,
                headers={
                    "x-latet-token": self._token,
                    "content-type": "application/json",
                    "accept-encoding": "identity",
                },
            )
            exchange.check()
            exchange.response = connection.getresponse()
            exchange.check()
            if exchange.response.getheader("content-encoding", "identity").lower() != "identity":
                raise LaunchControlUnavailable()
            return exchange
        except BaseException:
            exchange.finish()
            raise

    def call(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        exchange = None
        raw = b""
        sent = None
        try:
            sent = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if sent is not None and len(sent) > _BODY_MOST:
                raise LaunchControlUnavailable("launch-invalid")
            exchange = self._start(method, path, sent)
            answer = exchange.response
            assert answer is not None
            raw = answer.read(_ANSWER_MOST + 1)
            exchange.check()
            if len(raw) > _ANSWER_MOST:
                raise LaunchControlUnavailable()
            found = _object(raw)
            exchange.check()
            if answer.status != 200:
                code = found.get("error")
                raise LaunchControlUnavailable(code if type(code) is str else "launch-unavailable")
            return found
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise LaunchControlUnavailable() from None
        finally:
            raw, sent = b"", None
            if exchange is not None:
                exchange.finish()

    def hold(self, launch_id: str, generation: int) -> _Held:
        exchange = None
        try:
            if (
                re.fullmatch(r"[0-9a-f]{32}", launch_id) is None
                or type(generation) is not int
                or not 0 <= generation < (1 << 64) - 1
            ):
                raise LaunchControlUnavailable()
            exchange = self._start(
                "GET", f"/_latet/engine/v1/launches/{launch_id}/hold", None, f"?generation={generation}"
            )
            answer = exchange.response
            assert answer is not None
            if answer.status != 200 or answer.getheader("content-type", "").split(";")[0] != "application/x-ndjson":
                raise LaunchControlUnavailable()
            raw = answer.readline(_ACK_MOST + 1)
            exchange.check()
            if len(raw) > _ACK_MOST or not raw.endswith(b"\n"):
                raise LaunchControlUnavailable()
            ack = _object(raw)
            if (
                set(ack) != {"held", "generation"}
                or ack["held"] is not True
                or type(ack["generation"]) is not int
                or ack["generation"] != generation + 1
            ):
                raise LaunchControlUnavailable()
            held_socket = exchange.socket
            if held_socket is None:
                raise LaunchControlUnavailable()
            exchange.promote_hold()  # atomically disarm and JOIN the handshake deadline watcher
            held_socket.settimeout(None)  # suspension is not an offline expiry policy
            return _Held(exchange, ack["generation"])
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            if exchange is not None:
                exchange.finish()
            raise LaunchControlUnavailable() from None
        except BaseException:
            if exchange is not None:
                exchange.finish()
            raise
