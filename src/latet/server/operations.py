"""What E0c puts in front of a Latet server's app (E0b's ``create_server_app``): ``Operations``, and ``server_app``,
what `latet server run` serves and what later parts' tests serve.

It hands every request on as it came, HTTP, WebSocket and lifespan alike: its scope, its messages and the app's answer,
nothing added to them (``latet.space`` is the gate's, set where it hands a request to an identity's space), and counts
it in the server's metrics by its kind and its status, never its path (``request_kind``). Its state is the app's,
through which `latet server run` tells the app its server. The administration tokens are the administration's
(``latet.server.admin``).

The metrics (``GET /_latet/metrics``, Prometheus's text) are answered here alone, on a socket of their own that `latet
server run` opens beside the instance's when ``metrics_token_file`` is set (``metrics_host``, ``metrics_port``): a load
balancer in front of the instances' port never reaches them, and that port never answers them (the path is the server's
app's there, as any other). A request is told to have come to the metrics socket by the port of the socket that took
it (``scope["server"]``, which uvicorn sets and no proxy header rewrites); there anything but ``GET /_latet/metrics`` is
404, a WebSocket closed, and nothing reaches the server's app. They answer whatever the Host and over http too
(Prometheus asks each instance by its address; nothing in them is a value), with ``no-store`` and ``nosniff``, reading
no body; a browser's request is refused before its token is read. The token is read once, when the app is made (the
instance's start), and kept as a keyed hash under a key the process makes, never as it is: a scrape's one
``Authorization`` header (``Bearer``, in any case, a space, the token) is hashed and compared in constant time. A scrape
refused for its token takes a slot of its address's budget of the metrics' own, held in the instance's memory
(``LOOKUP_LIMIT`` a ``LOOKUP_WINDOW``; an IPv6 address's /48 counted as one too, as every credential's misses are):
past it, and while the shared limiter has the address past its budget, a scrape is refused (429) on the event loop, its
token not compared, no store asked. A refused scrape is said in the log once a minute with how many, never with what it
carried."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, unquote_plus, urlsplit

from latet.fileio import FileReadError, read_regular_bytes
from latet.logline import Paced, described
from latet.server.metrics import code_label
from latet.server.settings import SERVER_API
from latet.server.tokens import ADMIN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX, TOKEN_PREFIX

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import Message, Receive, Scope, Send

    from latet.server.app import Server
    from latet.server.metrics import Metrics

log = logging.getLogger(__name__)

METRICS = "/_latet/metrics"
ADMIN = f"{SERVER_API}/admin"
_SPACE = re.compile(r"/u/([^/]+)")  # an identity's address, whatever its key: the gate judges it
TOKEN_FILE_BYTES = 4096  # what is read of metrics_token_file at most: a token is one line
TOKEN_SHORTEST, TOKEN_LONGEST = 32, 1024
# The settings that name a file holding a secret of their own (reviewer_key_file: E0c's Task 21): the metrics token is
# never one of them, since a scrape over http carries it in clear.
_OTHER_SECRETS = ("master_key_file", "database_password_file", "valkey_password_file", "reviewer_key_file")
_JSON = b"application/json"
_ANSWER_HEADERS = [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]


def server_app(server: Server) -> Operations:
    """The server's app with E0c's operations in front: what `latet server run` serves."""
    from latet.server.app import create_server_app  # here: the app needs the server's extra, this module does not

    return Operations(create_server_app(server), server)


def request_kind(path: str) -> str:
    """What a request to the instance's own socket is, for the metrics: never its path, which may hold a route key. A
    scrape is counted as one by the metrics socket alone; ``/_latet/metrics`` here is any other path."""
    if (found := _SPACE.match(path)) is not None:
        rest = path[found.end(1) :]
        if rest.startswith("/_latet/engine/"):
            return "plugin"
        return "computer" if rest == "/_latet" or rest.startswith("/_latet/") else "agent"
    if path in ("/_latet/health", "/_latet/ready"):
        return "health"
    if path == ADMIN or path.startswith(f"{ADMIN}/"):
        return "admin"
    if path.startswith(f"{SERVER_API}/"):
        return "computer"
    if path == "/_latet/ui" or path.startswith(("/_latet/ui/", "/_latet/api/")):
        return "dashboard"
    return "other"


def _port_of(scope: Mapping[str, Any]) -> int | None:
    """The port of the socket that took the request (``scope["server"]``, uvicorn's: no proxy header rewrites it)."""
    server = scope.get("server")
    if isinstance(server, list | tuple) and len(server) >= 2:
        port = server[1]
        if isinstance(port, int) and not isinstance(port, bool):
            return port
    return None


def _same_file(path: Path, other: object) -> bool:
    try:
        return isinstance(other, str | Path) and Path(other).exists() and path.samefile(other)
    except OSError:
        return False


def _password_of(address: object) -> list[str]:
    """The password an address of the settings carries, as its driver reads it: after the user's name, or as the
    ``password`` parameter, which libpq and redis-py both read (each way the two read an escape); none for what is no
    address."""
    if not isinstance(address, str):
        return []
    try:
        parts = urlsplit(address)
        found = [unquote(parts.password)] if parts.password else []
    except ValueError:
        return []
    for pair in parts.query.split("&"):
        name, _, value = pair.partition("=")
        if name == "password" and value:
            found += [unquote(value), unquote_plus(value)]
    return found


def _other_secrets(server: Server) -> list[tuple[str, str]]:
    """The other secrets a server holds as text, each with the words that name it by its setting: PostgreSQL's and
    Valkey's passwords (their file's, put into the address, or the address's own), and the shared reviewer's key once
    Task 21 reads it at the start (``Server.reviewer_key``, read by its name until then)."""
    settings = server.settings
    found = [
        (f"{whose} password ({name})", password)
        for name, whose in (("database_url", "PostgreSQL's"), ("valkey_url", "Valkey's"))
        for password in _password_of(getattr(settings, name, None))
    ]
    reviewer_key = getattr(server, "reviewer_key", None)
    if isinstance(reviewer_key, str) and reviewer_key:
        found.append(("the shared reviewer's key (reviewer_key_file)", reviewer_key))
    return found


def _metrics_token(server: Server) -> tuple[bytes, bytes] | None:
    """The metrics token of ``metrics_token_file``, read once, as a key made for this process and the token's keyed hash
    under it: never the token itself. None when the setting is not set: metrics off. Raises ValueError, in words that
    name the setting and never what the file holds, for a file that cannot be read, is more than ``TOKEN_FILE_BYTES``,
    not UTF-8 text, empty, more than one line, other than ``TOKEN_SHORTEST`` to ``TOKEN_LONGEST`` printable ASCII
    characters with no space, or another secret: the file of another secret setting, a token, an API key or an
    administration token of the server's, a version of its master key, PostgreSQL's or Valkey's password, or the shared
    reviewer's key (``_other_secrets``, each compared in constant time). Nothing is raised while the error of a read or
    of a decoding is handled: they hold the file's name or its bytes."""
    settings = server.settings
    path = settings.metrics_token_file
    if path is None:
        return None
    path = Path(path)
    for name in _OTHER_SECRETS:
        if _same_file(path, getattr(settings, name, None)):
            raise ValueError(
                f"metrics_token_file names the file of {name}: the metrics token is a secret of its own, which a "
                "scrape over http carries in clear (make one with `openssl rand -hex 32`)"
            )
    raw, problem = b"", ""
    try:  # a regular file, as every other secret's: a device or a pipe is refused before it is read, never waited on
        raw = read_regular_bytes(path, limit=TOKEN_FILE_BYTES, follow=True)
    except FileReadError as exc:
        too_large = exc.reason == "too_large"
        problem = f"is more than {TOKEN_FILE_BYTES} bytes: a token is one line" if too_large else str(exc)
    if problem:
        raise ValueError(f"metrics_token_file names a file that {problem}")
    text: str | None = None
    try:
        text = raw.decode("utf-8-sig").strip()  # without the mark that some editors and shells put first
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise ValueError("metrics_token_file names a file that is not UTF-8 text")
    if not text:
        raise ValueError("metrics_token_file names a file that holds no token")
    if "\n" in text or "\r" in text:
        raise ValueError("metrics_token_file names a file that holds more than one line: a token is one line")
    if any(not "!" <= char <= "~" for char in text):
        raise ValueError(
            "metrics_token_file names a file whose token holds a space, a control character or a character that is "
            "not ASCII: a token is printable ASCII, such as `openssl rand -hex 32` makes"
        )
    if not TOKEN_SHORTEST <= len(text) <= TOKEN_LONGEST:
        raise ValueError(
            f"metrics_token_file names a file whose token is not {TOKEN_SHORTEST} to {TOKEN_LONGEST} characters long "
            "(make one with `openssl rand -hex 32`)"
        )
    if any(prefix in text for prefix in (ADMIN_PREFIX, TOKEN_PREFIX, COMPUTER_PREFIX, KEY_PREFIX)):
        raise ValueError(
            "metrics_token_file names a file that holds a token, an API key or an administration token of the "
            "server's: a scrape over http would carry it in clear (make a token of its own with `openssl rand -hex 32`)"
        )
    if server.master.is_version(text):
        raise ValueError(
            "metrics_token_file names a file that holds the master key: a scrape over http would carry it in clear "
            "(make a token of its own with `openssl rand -hex 32`)"
        )
    given = text.encode()
    named = next(
        (
            words
            for words, other in _other_secrets(server)
            if hmac.compare_digest(given, other.encode("utf-8", "surrogatepass"))
        ),
        None,
    )
    if named is not None:
        raise ValueError(
            f"metrics_token_file names a file that holds {named}: a scrape over http would carry it in clear (make a "
            "token of its own with `openssl rand -hex 32`)"
        )
    key = secrets.token_bytes(32)
    return key, hmac.digest(key, text.encode("ascii"), hashlib.sha256)


def _bearer(scope: Mapping[str, Any]) -> bytes | None:
    """The token of a scrape's one ``Authorization`` header (``Bearer`` in any case, one space, the token), as the bytes
    it came in: None when there is none, there are two, or it is written otherwise. Nothing of it is decoded, logged or
    kept."""
    found = [value for name, value in scope.get("headers") or [] if name == b"authorization"]
    if len(found) != 1:
        return None
    scheme, space, token = found[0].partition(b" ")
    if not space or scheme.lower() != b"bearer" or not token:
        return None
    return token


class _Misses:
    """The scrapes refused for their token, by the bucket of their client's address, and for an IPv6 bucket by its /48
    too (``counting``: ``machines._counting``, which counts every other credential's misses so), in this instance's
    memory: ``limit`` a ``window`` each, ``kept`` at most, the one remembered longest forgotten first. Safe across
    threads."""

    def __init__(
        self,
        limit: int,
        window: float,
        kept: int,
        *,
        clock: Callable[[], float],
        counting: Callable[[str], tuple[str, ...]],
    ) -> None:
        self._limit, self._window, self._kept, self._clock = limit, window, kept, clock
        self._counting = counting
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, tuple[float, int]] = OrderedDict()  # its key: its window's start, its misses

    def miss(self, bucket: str) -> None:
        now = self._clock()
        with self._lock:
            for key in self._counting(f"metrics:{bucket}"):
                start, count = self._buckets.pop(key, (now, 0))
                if now - start >= self._window:
                    start, count = now, 0
                self._buckets[key] = (start, count + 1)
            while len(self._buckets) > self._kept:
                self._buckets.popitem(last=False)

    def refuses(self, bucket: str) -> bool:
        """Whether the bucket's scrapes are refused now: ``limit`` misses in its window already, or in its /48's."""
        now = self._clock()
        with self._lock:
            return any(self._past(key, now) for key in self._counting(f"metrics:{bucket}"))

    def _past(self, key: str, now: float) -> bool:
        """Under the lock: whether ``key`` holds ``limit`` misses in its window (one whose window ended is
        forgotten)."""
        found = self._buckets.get(key)
        if found is None:
            return False
        if now - found[0] >= self._window:
            del self._buckets[key]
            return False
        return found[1] >= self._limit


class _Answer:
    """The status of what a request was answered, as the metrics count it (``code_label``): a WebSocket's is 101 once
    accepted, its refused handshake's, or 403 when it was closed before (uvicorn answers so)."""

    __slots__ = ("status", "websocket")

    def __init__(self, websocket: bool) -> None:
        self.websocket, self.status = websocket, None

    def heard(self, message: Message) -> None:
        if self.status is not None:
            return
        kind = message.get("type")
        if kind in ("http.response.start", "websocket.http.response.start"):
            status = message.get("status")
            self.status = status if isinstance(status, int) and not isinstance(status, bool) else -1
        elif kind == "websocket.accept":
            self.status = 101
        elif kind == "websocket.close":
            self.status = 403


async def _send(
    send: Send, status: int, body: bytes, content_type: bytes = _JSON, headers: tuple[tuple[bytes, bytes], ...] = ()
) -> None:
    head = [(b"content-type", content_type), (b"content-length", str(len(body)).encode()), *_ANSWER_HEADERS, *headers]
    await send({"type": "http.response.start", "status": status, "headers": head})
    await send({"type": "http.response.body", "body": body})


async def _error(send: Send, status: int, message: str, *headers: tuple[bytes, bytes]) -> None:
    await _send(send, status, json.dumps({"error": message}).encode(), headers=headers)


class Operations:
    """In front of the server's app (``app``), for ``server``: every request handed on as it came, and counted; the
    metrics answered on their own socket (the module's words)."""

    def __init__(self, app: Starlette, server: Server) -> None:
        from latet.server import machines  # here: a server is being made, and has the server's extra

        self.app, self.server = app, server
        self.metrics: Metrics = server.metrics  # the server's registry: later parts add theirs to it
        token = _metrics_token(server)  # once, at the instance's start: a change takes a restart
        self._key, self._hashed = token if token is not None else (b"", b"")
        self._port = server.settings.metrics_port if token is not None else None  # None: no metrics socket
        self._machines = machines
        self._misses = _Misses(
            machines.LOOKUP_LIMIT, machines.LOOKUP_WINDOW, machines.BUCKETS_KEPT, clock=self.metrics.clock,
            counting=machines._counting,
        )  # fmt: skip
        self._refused = Paced()  # scrapes refused for their token: said once a minute
        self._limited = Paced()  # scrapes refused while their address waits: said once a minute
        self._uncounted = Paced()  # requests the metrics could not count: said once a minute
        self.metrics.watch(server)

    @property
    def state(self) -> Any:
        """The app's state: what `latet server run` tells the app its server through (``cli.main.uvicorn_server``)."""
        return self.app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        on_metrics = self._port is not None and _port_of(scope) == self._port
        kind = "metrics" if on_metrics else request_kind(scope.get("path", ""))
        answer, started = _Answer(scope["type"] == "websocket"), self.metrics.clock()

        async def counted(message: Message) -> None:
            answer.heard(message)
            await send(message)

        try:
            if on_metrics:
                await self._metrics(scope, counted)
            else:
                await self.app(scope, receive, counted)
        finally:  # answered, refused, failed, its client gone, cancelled
            self._count(kind, answer, started, scope["type"] == "http")

    def _count(self, kind: str, answer: _Answer, started: float, http: bool) -> None:
        """A request counted by its kind and status, and an HTTP request's time: never failing the request it counts."""
        try:
            self.metrics.requests.inc(kind, code_label(answer.status, websocket=answer.websocket))
            if http:
                self.metrics.request_seconds.observe(self.metrics.clock() - started, kind)
        except Exception as exc:
            if count := self._uncounted.due():
                log.warning(
                    "a request could not be counted in the metrics (%s)%s",
                    described(exc),
                    f" ({count} times since the line before)" if count > 1 else "",
                )

    async def _metrics(self, scope: Scope, send: Send) -> None:
        """The metrics socket: ``GET /_latet/metrics`` with the metrics token, nothing else, nothing of a body read and
        nothing handed to the server's app. A browser's request is refused first; then an address past its refused
        scrapes; then a token that is not the metrics token, which takes a slot of its address's budget."""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})
            return
        if scope.get("path") != METRICS or scope.get("method") != "GET":
            await _error(send, 404, "there is no such route")
            return
        machines = self._machines
        if machines.from_a_page(scope):
            await _error(send, 403, "browser requests are not accepted here")
            return
        bucket = machines.bucket_of(machines.client_of(scope))
        if self._misses.refuses(bucket) or self.server.limits.refuses(machines.miss_key(bucket)):
            if count := self._limited.due():
                log.warning(
                    "scrapes of the metrics were refused from an address past its refused tokens: it waits a minute%s",
                    f" ({count} since the line before)" if count > 1 else "",
                )
            await _error(send, 429, "too many refused scrapes from this address: wait a minute", self._retry_after())
            return
        token = _bearer(scope)
        if token is None or not hmac.compare_digest(hmac.digest(self._key, token, hashlib.sha256), self._hashed):
            self._misses.miss(bucket)
            if count := self._refused.due():
                log.warning(
                    "a scrape of the metrics was refused: it did not carry the metrics token%s",
                    f" ({count} since the line before)" if count > 1 else "",
                )
            await _error(send, 401, "unauthorized", (b"www-authenticate", b"Bearer"))
            return
        try:
            text = await self.metrics.scrape()
        except RuntimeError:  # the instance stops: its metrics' threads are let go
            await _error(send, 503, "the metrics cannot be read now: try again in a moment", (b"retry-after", b"1"))
            return
        await _send(send, 200, text.encode(), b"text/plain; version=0.0.4; charset=utf-8")

    def _retry_after(self) -> tuple[bytes, bytes]:
        return b"retry-after", self._machines.RETRY_AFTER.encode()
