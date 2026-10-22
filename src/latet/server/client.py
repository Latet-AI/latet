"""What a computer asks its Latet server: the machines' calls at /_latet/server/v1/ (``ServerClient``), over HTTPS with
the certificate the computer pinned (never an authority) or the system's authorities.

What it never does, written out. It follows no redirect: one would carry the token elsewhere, so a 3xx is the refusal
it is. It reads nothing of the environment (``trust_env=False``): no proxy, no ``.netrc``, not httpx's own reading of
SSL_CERT_FILE and SSL_CERT_DIR; on Linux the system's authorities are OpenSSL's default paths, which truststore loads
and which read SSL_CERT_FILE and SSL_CERT_DIR, the system's own trust as its tools read it. It sends no ``Origin`` and
no ``sec-fetch-*``, which the server refuses as a web page's. It reaches nothing by an address a server answers
(``url``, ``base``, ``anthropic``, ``openai``), and answers none: a computer's addresses come from its connection's and
``route_key(token)`` alone, and the dashboard's sign-in link is made here, from the connection's address and a code of
the server's shape (a link the server wrote would be started by the operating system on every computer that opens it).

It sends this computer's key (``x-latet-computer-key``, R16) on the two calls that ask for it, a sign-in link to the
dashboard and the person's identity, and on no other: not on ``whoami``, the token's revocation, the space's calls or
the helper's bridge exchange, which the token alone makes.

An answer is read within bounds and by its shape: 1 MiB at most (a caller may allow more, up to 32 MiB), the whole
exchange within a deadline (30 s: the connection, the handshake and the headers, which a server may send a byte at a
time, each byte within httpx's timeout for a read), never compressed, parsed so that no nesting raises out of it.
Nothing leaves after the deadline: a connection that comes late, its handshake done once the caller was told the server
did not answer in time, carries nothing (a one-time code would be spent, the token it brings lost). A refusal's words
and details are made printable where they are read: control characters escaped (C0, DEL, C1, U+2028, U+2029, the
bidirectional controls), 500 characters each, 20 details. A 503 the server gives of its own with a Retry-After of 5 s or
less is asked again once, after it (nothing was done). Raises ServerError when no usable answer comes."""

from __future__ import annotations

import contextlib
import itertools
import json
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import truststore

from latet.config import data_dir
from latet.engine.reviewer import certificate as certificates
from latet.logline import described
from latet.server.certificate import AUTHORITY, is_authority
from latet.server.connection import (
    COMPUTER_KEY,
    NAME_CHARS,
    PERSONAL_TOKEN,
    USER_ID,
    _kept_as_is,
    _server_pem,
    fingerprint_form,
)

if TYPE_CHECKING:
    from latet.server.connection import _PreparedConnection

SERVER_API = "/_latet/server/v1"  # E0b's latet.server.settings.SERVER_API
SPACE = "/u/"  # where an identity's space is, below which a computer makes its own calls (`/u/<route key>/_latet/...`)
UI = "/_latet/ui"  # the dashboard's path, as E0b's sign-in link has it (latet.api.rest.UI, which machines.py imports)
COMPUTER_KEY_HEADER = "x-latet-computer-key"  # machines.COMPUTER_KEY_HEADER: sent on login-code and identity alone
ANSWER_BYTES = 1024 * 1024  # an answer's body read at most…
ANSWER_MOST = 32 * 1024 * 1024  # …unless a caller allows more, up to this (Task 13's pages of the log)
DEADLINE = 30.0  # seconds a call takes at most, its one retry included
RETRY_MOST = 5  # seconds of a 503's Retry-After waited at most before its one retry
SAID_CHARS = 500  # characters of the server's words, and of each detail, kept
DETAILS_MOST = 20
CODE = re.compile(r"[A-Za-z0-9_-]{16,128}")  # a sign-in code (E0b's: secrets.token_urlsafe(24), 32 of them)
_ADDRESSES = ("url", "base", "anthropic", "openai")  # what a server answers of addresses: never used
_DIGITS = re.compile(r"[0-9]{1,9}")
# The steps of an exchange (httpcore's trace events) after which bytes of the request would leave: a new connection's
# handshake done, and its request's headers about to be written (the first on a kept connection, or over http).
_BEFORE_SENDING = frozenset({"connection.start_tls.complete", "http11.send_request_headers.started"})
# What a terminal would act on, or show in another order: C0, DEL, C1, the line and paragraph separators, and the
# bidirectional controls (ALM, LRM, RLM, LRE to RLO, LRI to PDI).
_UNPRINTABLE = frozenset(
    (
        *range(0x20),
        *range(0x7F, 0xA0),
        0x2028,
        0x2029,
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)


class ServerError(Exception):
    """The server refused (``status``, its reason, the ``details`` it gave, the seconds its ``Retry-After`` asked), or
    nothing usable answered (status 0)."""

    def __init__(
        self, status: int, message: str, details: tuple[str, ...] = (), retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = tuple(details)
        self.retry_after = retry_after


class ReceiveBudget:
    """Raw response-body accounting shared by explicitly opted-in, sequential calls."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.received = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.received)

    def _charge(self, size: int) -> None:
        self.received += size
        if self.received > self.limit:
            raise ServerError(0, "the response-body receive budget was exceeded")


def printable(text: str, limit: int = SAID_CHARS) -> str:
    """``text`` as a terminal may show it: what it would act on escaped (``\\x1b``, ``\\u202e``), ``limit`` characters
    at most (the last one an ellipsis when it was cut)."""
    escaped = "".join(ascii(char)[1:-1] if ord(char) in _UNPRINTABLE else char for char in text[:limit])
    if len(escaped) > limit or len(text) > limit:
        return escaped[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return escaped


def _object(content: bytes) -> dict[str, Any] | None:
    """``content`` when it is a JSON object (however deep, it raises nothing); None for anything else."""
    try:
        found = json.loads(content)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def answer_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON object a call answered. Raises ServerError (status 0) for anything else."""
    found = _object(response.content)
    if found is None:
        raise ServerError(0, "the server's answer is not a JSON object")
    return found


def _retry_after(headers: httpx.Headers) -> int | None:
    """The seconds of a ``Retry-After``: a whole number, digits alone (a date or a fraction is not read)."""
    given = headers.get("retry-after", "").strip()
    return int(given) if _DIGITS.fullmatch(given) else None


def _details(given: Any) -> tuple[str, ...]:
    """A refusal's ``details``: each a text, or an object's ``msg``; printable, ``DETAILS_MOST`` at most."""
    if not isinstance(given, list):
        return ()
    texts = (item.get("msg") if isinstance(item, dict) else item for item in given)
    return tuple(itertools.islice((printable(text) for text in texts if isinstance(text, str)), DETAILS_MOST))


def _refusal(answer: httpx.Response) -> ServerError:
    found = _object(answer.content) or {}
    said = found.get("error")
    message = printable(said) if isinstance(said, str) and said else f"HTTP {answer.status_code}"
    return ServerError(answer.status_code, message, _details(found.get("details")), _retry_after(answer.headers))


class _Gate:
    """Whether an exchange may still send: not once its caller left it at its deadline, nor past that deadline. The
    exchange's worker asks at each step of ``_BEFORE_SENDING`` (httpcore calls ``trace`` at every step of a request),
    and is stopped there when it may no longer send, the connection a handshake had just made closed. The caller's
    leaving and the worker's passing are decided under one lock: a request starts before its caller gives up, or
    never."""

    def __init__(self, url: str, end: float, clock: Callable[[], float]) -> None:
        self._url, self._end, self._clock = url, end, clock
        self._lock = threading.Lock()
        self._left = False

    def leave(self) -> None:
        with self._lock:
            self._left = True

    def trace(self, event: str, info: Mapping[str, Any]) -> None:
        if event not in _BEFORE_SENDING:
            return
        with self._lock:
            self._left = self._left or self._clock() > self._end
            late = self._left
        if late:
            if event == "connection.start_tls.complete":
                with contextlib.suppress(Exception):
                    info["return_value"].close()
            raise ServerError(0, f"{self._url} did not answer in time")


def _pinned(fingerprint: str, *, _data: Path | None = None) -> tuple[str | None, str | None]:
    """The certificate pinned with ``fingerprint``, as it was pinned (its content hashed), and why it is not used when
    it is not: ``missing``, or ``authority`` (a certificate kept before, by `latet llm trust` say, that is one)."""
    pem = _server_pem(_data if _data is not None else data_dir(), fingerprint)
    if pem is None:
        return None, "missing"
    if is_authority(pem):
        return None, "authority"
    return pem, None


def tls_context(fingerprint: str | None) -> ssl.SSLContext:
    """The certificate this computer pinned for its server (none is accepted when it went missing, or is an authority),
    or the system's authorities, the host name checked."""
    return _tls_context(fingerprint)


def _tls_context(fingerprint: str | None, *, _data: Path | None = None) -> ssl.SSLContext:
    if fingerprint:
        pin = _pinned(fingerprint) if _data is None else _pinned(fingerprint, _data=_data)
        return certificates.context(pin[0])
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _http(
    url: str, fingerprint: str | None, timeout: float, *, _verify: ssl.SSLContext | None = None
) -> tuple[httpx.Client, bool]:
    """The client a call to ``url`` goes out with, and whether its caller closes it. Tests put the server's app here,
    in-process (a TestClient they keep open)."""
    verify: ssl.SSLContext | bool = (_verify or tls_context(fingerprint)) if url.startswith("https://") else True
    client = httpx.Client(
        verify=verify, timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False, follow_redirects=False
    )
    return client, True


def _as_kept(url: str) -> str:
    """``url`` when it is an address as ``server_url`` keeps it. Raises ValueError otherwise: a caller that skipped it
    would send a user's password as Basic credentials, or ask under a path."""
    if not _kept_as_is(url):
        raise ValueError("a server's address as `server_url` keeps it, such as https://latet.acme.lan")
    return url


def pin_certificate(url: str, fingerprint: str) -> str:
    """The certificate ``url`` shows, kept when its fingerprint is ``fingerprint`` (``sha256:…`` or ``AB:CD:…``) and it
    is a server's own, not an authority; the fingerprint as Latet writes it. Compared before anything is sent: nothing
    is kept and nothing is sent otherwise. Raises ServerError (status 0) when it cannot be reached, shows another
    certificate (said without the fingerprint it showed: only the administrator's counts), or is an authority."""
    _as_kept(url)
    try:
        wanted = fingerprint_form(fingerprint)
    except ValueError as exc:
        raise ServerError(0, f"--fingerprint: {exc}") from None
    kept = certificates.load(data_dir(), wanted)
    if kept is not None:  # pinned already (or trusted with `latet llm trust`)
        if is_authority(kept):
            raise ServerError(0, f"the certificate kept for {url} {AUTHORITY}")
        return wanted
    try:
        pem, found = certificates.fetch(url)
    except (OSError, ValueError) as exc:
        raise ServerError(0, f"{url} could not be reached ({described(exc)})") from None
    if found != wanted:
        raise ServerError(
            0,
            f"{url} shows another certificate than the fingerprint you were given: nothing was sent and nothing was "
            "kept. Only your administrator's fingerprint counts, read on the server's own machine; if it is the one "
            "you gave, another machine may be answering in your server's place: do not connect from this network",
        )
    if is_authority(pem):
        raise ServerError(0, f"the certificate {url} shows {AUTHORITY}; nothing was sent and nothing was kept")
    certificates.save(data_dir(), pem)
    return wanted


def _connection_pin(prepared: _PreparedConnection) -> str | None:
    """A checked PEM snapshot, also available to diagnostics when the matched credential cannot be read."""
    from latet.server.connection import NotConnected, _require_selection, _server_pem

    _require_selection(prepared)
    connection = prepared.connection
    if connection is None:
        raise NotConnected("this computer is not connected to a Latet server")
    pem = None
    if connection.fingerprint:
        pem = _server_pem(prepared.selected.data, connection.fingerprint, strict=True)
        if pem is None or is_authority(pem):
            raise ServerError(
                0,
                "the certificate this computer pinned for its server is unavailable: `latet disconnect`, then "
                "`latet connect` with the server's fingerprint and a new code from an administrator",
            )
    return pem


def _connection_credential(prepared: _PreparedConnection, *, key: bool = False) -> _PreparedConnection:
    """The connection with its token, and its computer key, read from their record: the one way every CLI call takes
    its credential. ``key``: the call asks for the computer key too (a sign-in link, the identity), and is refused,
    nothing sent, when the record holds none (``key_missing``). Raises NotConnected."""
    from latet.server.connection import (
        NotConnected,
        _prepare_key,
        _prepare_token,
        _require_selection,
        key_missing,
        token_missing,
    )

    _require_selection(prepared)
    prepared = _prepare_token(prepared)
    if not prepared.token:
        assert prepared.connection is not None  # _prepare_token answers a token for a connection alone
        raise NotConnected(token_missing(prepared.connection))  # R7: what the person does, never an administrator
    if key:
        prepared = _prepare_key(prepared)
        if not prepared.key:
            assert prepared.connection is not None
            raise NotConnected(key_missing(prepared.connection))
    _require_selection(prepared)
    return prepared


def _connection_material(prepared: _PreparedConnection, *, key: bool = False) -> tuple[_PreparedConnection, str | None]:
    """Admit a captured connection's pin before reading its matched credential; never select another data root."""
    pem = _connection_pin(prepared)
    return _connection_credential(prepared, key=key), pem


class ServerClient:
    """The machines' calls to the server at ``url`` (an address as ``server_url`` keeps it), with ``token`` (a person's
    token, an API key) when it is given, over HTTPS with the certificate pinned as ``fingerprint`` or the system's
    authorities; ``key``, this computer's key, sent on the calls that ask for it alone (``login_code``,
    ``put_identity``). ``clock`` and ``sleep``: the time a call's deadline is read with, and the wait before a 503's
    retry."""

    def __init__(
        self,
        url: str,
        token: str | None,
        fingerprint: str | None = None,
        *,
        key: str | None = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        _prepared: _PreparedConnection | None = None,
        _pem: str | None = None,
    ) -> None:
        self.url = _as_kept(url)
        self.token = token
        self.key = key
        self.fingerprint = fingerprint
        self.timeout = timeout
        self._clock, self._sleep = clock, sleep
        self._verify: ssl.SSLContext | None = None  # a captured connection's: kept for the client that replaces one
        self._pem = _pem
        if _prepared is None:
            self._http, self._owned = _http(self.url, fingerprint, timeout)
        else:
            from latet.server.connection import _require_selection

            _require_selection(_prepared)
            selected = _prepared.connection
            if selected is None or selected.url != self.url or selected.fingerprint != fingerprint:
                raise ServerError(0, "this computer's connection changed")
            import hmac

            if not token or not _prepared.token or not hmac.compare_digest(token, _prepared.token):
                raise ServerError(0, "this computer's server token changed")
            if key is not None and (not _prepared.key or not hmac.compare_digest(key, _prepared.key)):
                raise ServerError(0, "this computer's key changed")
            if fingerprint and (_pem is None or certificates.fingerprint_of(_pem) != fingerprint or is_authority(_pem)):
                raise ServerError(0, "the server certificate is unavailable")
            verify = certificates.context(_pem) if fingerprint else _tls_context(None)
            self._verify = verify
            self._http, self._owned = _http(self.url, fingerprint, timeout, _verify=verify)

    def __repr__(self) -> str:
        return f"ServerClient({self.url!r})"  # its address alone: never its token, nor its key

    def _keyed(self) -> dict[str, str]:
        """The header of this computer's key, for a call that asks for it. Raises ServerError (status 0), nothing
        sent, when this client has none: the CLI says what to do before it gets here (``key_missing``)."""
        if not self.key or not COMPUTER_KEY.fullmatch(self.key):
            raise ServerError(
                0,
                "this computer keeps no computer key for its server: `latet disconnect`, then `latet connect` with a "
                "new code from an administrator",
            )
        return {COMPUTER_KEY_HEADER: self.key}

    def __enter__(self) -> ServerClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        limit: int = ANSWER_BYTES,
        deadline: float = DEADLINE,
        receive: ReceiveBudget | None = None,
    ) -> httpx.Response:
        """A call to ``/_latet/server/v1/<path>`` (``path`` is ``"whoami"``, ``"admin/status"``, or the whole path), or
        to the identity's space when ``path`` starts with ``/u/`` (this computer's own calls there, its route key from
        its token), with this computer's token or key, its answer read up to ``limit`` bytes within ``deadline``
        seconds. A 503 with a Retry-After of 5 s or less is asked again once. Raises ServerError for an answer that is
        not 2xx (with its details and Retry-After); status 0 when nothing usable answered."""
        if limit > ANSWER_MOST:
            raise ValueError(f"an answer is read up to {ANSWER_MOST} bytes")
        given = dict(headers or {})
        if any(name.lower() == "origin" or name.lower().startswith("sec-fetch-") for name in given):
            raise ValueError("a computer's call is no web page's: no Origin, no sec-fetch-* header")
        sent = {"accept-encoding": "identity", **({"x-latet-token": self.token} if self.token else {}), **given}
        if path.startswith(SPACE):
            address = f"{self.url}{path}"
        else:
            address = f"{self.url}{SERVER_API}/{path.removeprefix(SERVER_API).lstrip('/')}"
        end = self._clock() + deadline
        answer = self._once(method, address, sent, body, limit, end, receive)
        wait = _retry_after(answer.headers) if answer.status_code == 503 else None
        if wait is not None and wait <= RETRY_MOST and self._clock() + wait < end:
            self._sleep(wait)
            answer = self._once(method, address, sent, body, limit, end, receive)
        if not 200 <= answer.status_code < 300:
            raise _refusal(answer)
        return answer

    def _once(
        self,
        method: str,
        address: str,
        sent: dict[str, str],
        body: Any,
        limit: int,
        end: float,
        receive: ReceiveBudget | None = None,
    ) -> httpx.Response:
        """One exchange, before ``end`` whole: the connection, the handshake and the headers too, which a server may
        send a byte at a time, each in httpx's time for a read. A worker makes it; at the deadline it is left, its
        client closed (its connection ends, or fails at its next read), and a new client made for what follows. Left,
        it sends nothing more: a connection still being made when it was left carries no request (``_Gate``)."""
        if receive is not None:
            limit = min(limit, receive.remaining)
            if limit <= 0:
                raise ServerError(0, "the response-body receive budget is exhausted")
        left = end - self._clock()
        if left <= 0:
            raise ServerError(0, f"{self.url} did not answer in time")
        done: dict[str, Any] = {}
        gate = _Gate(self.url, end, self._clock)

        def exchange() -> None:
            try:
                done["answer"] = self._exchange(method, address, sent, body, limit, end, left, gate, receive)
            except BaseException as exc:  # handed to the caller as it is
                done["error"] = exc

        worker = threading.Thread(target=exchange, name="latet-server-call", daemon=True)
        worker.start()
        try:
            worker.join(left)
        finally:  # however the wait ended (a Ctrl+C in it too), whatever the worker is doing, it sends nothing more
            gate.leave()
        if worker.is_alive():
            self._leave()
            raise ServerError(0, f"{self.url} did not answer in time")
        if "error" in done:
            raise done["error"]
        return done["answer"]

    def _leave(self) -> None:
        """The client of an exchange left at its deadline closed, and a new one made (a client a test keeps open, not
        this one's to close, is left as it is). A client made from a captured connection keeps its verify context: its
        pin is never read again from the data folder, which may be another one by then (T6.20, X-M4)."""
        if self._owned:
            with contextlib.suppress(Exception):
                self._http.close()
            self._http, self._owned = _http(self.url, self.fingerprint, self.timeout, _verify=self._verify)

    def _exchange(
        self,
        method: str,
        address: str,
        sent: dict[str, str],
        body: Any,
        limit: int,
        end: float,
        left: float,
        gate: _Gate,
        receive: ReceiveBudget | None = None,
    ) -> httpx.Response:
        """One exchange, its answer read whole within ``limit`` bytes, httpx's reads each within the time left, its
        request sent only while ``gate`` lets it."""
        timeout = httpx.Timeout(min(self.timeout, left), connect=min(5.0, left))
        try:
            with self._http.stream(
                method, address, json=body, headers=sent, timeout=timeout, extensions={"trace": gate.trace}
            ) as streamed:
                encoding = streamed.headers.get("content-encoding", "").strip().lower()
                if encoding not in ("", "identity"):
                    raise ServerError(0, f"{self.url} answered compressed, which this computer did not ask for")
                declared = streamed.headers.get("content-length", "").strip()
                too_long = f"{self.url} answered more than {limit} bytes: nothing of it was read"
                if _DIGITS.fullmatch(declared) and int(declared) > limit:
                    raise ServerError(0, too_long)
                chunks: list[bytes] = []
                size = 0
                for chunk in streamed.iter_raw():
                    if receive is not None:
                        receive._charge(len(chunk))
                    size += len(chunk)
                    if size > limit:
                        raise ServerError(0, too_long)
                    if self._clock() > end:
                        raise ServerError(0, f"{self.url} did not answer in time")
                    chunks.append(chunk)
                return httpx.Response(
                    streamed.status_code, headers=streamed.headers, content=b"".join(chunks), request=streamed.request
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ServerError(0, self._unreachable(exc)) from None

    def _unreachable(self, exc: BaseException) -> str:
        if isinstance(exc, httpx.InvalidURL):
            return f"{self.url} cannot be asked that: the call's address is not one httpx takes"
        if not certificates.untrusted(exc):
            return f"{self.url} does not answer ({type(exc).__name__})"
        if self.fingerprint:
            pem, why = (self._pem, None) if self._pem is not None else _pinned(self.fingerprint)
            again = "`latet disconnect`, then `latet connect` with the new fingerprint and a new code"
            if why == "missing":
                return (
                    f"the certificate this computer pinned for {self.url} is missing: `latet disconnect`, then "
                    "`latet connect` with the server's fingerprint and a new code"
                )
            if why == "authority":
                return f"the certificate kept for {self.url} {AUTHORITY}"
            refusal = certificates._refusal(exc)
            code = refusal.verify_code if refusal is not None else None
            if pem is not None and code == certificates._EXPIRED:  # C-M1: the pinned one itself, out of date
                day = certificates.expires(pem).date().isoformat()
                return (
                    f"the certificate this computer pinned for {self.url} expired on {day}: once its administrator "
                    f"made a new one (`latet server certificate --make`), {again}"
                )
            if code == certificates._NOT_YET_VALID:
                return (
                    f"the certificate this computer pinned for {self.url} is not valid yet: check the clock of this "
                    "computer and the server's"
                )
            return (
                f"{self.url} shows a certificate other than the one this computer pinned: if its administrator made a "
                f"new one, {again}; if not, something may be posing as your server"
            )
        return (
            f"{self.url} shows a certificate this computer does not trust: if it is self-signed, ask an administrator "
            "for its fingerprint (`latet server certificate`) and add --fingerprint"
        )

    def enrol(self, code: str, label: str) -> dict[str, Any]:
        """``{"token", "computer_key", "identity": {"id", "name"}}``: this computer's token and key and whose they are,
        when the server answered an enrolment of their shapes (nothing else of its answer, its addresses least of all).
        Raises ServerError (status 0) otherwise; a token answered with no computer key (a server older than this
        computer's Latet) is revoked again first, so that nothing is left of it on the server."""
        answer = answer_of(self.request("POST", "enrol", body={"code": code, "label": label}))
        token, identity, key = answer.get("token"), answer.get("identity"), answer.get("computer_key")
        user = identity.get("id") if isinstance(identity, dict) else None
        name = identity.get("name") if isinstance(identity, dict) else None
        if not (
            isinstance(token, str)
            and PERSONAL_TOKEN.fullmatch(token)
            and isinstance(user, str)
            and USER_ID.fullmatch(user)
            and isinstance(name, str)
            and len(name) <= NAME_CHARS
        ):
            raise ServerError(0, "the server's answer is not an enrollment")
        if not (isinstance(key, str) and COMPUTER_KEY.fullmatch(key)):
            with contextlib.suppress(ServerError):
                self.request("DELETE", "token", headers={"x-latet-token": token})
            raise ServerError(
                0,
                "the server gave this computer no computer key: it runs an earlier Latet than this computer, and the "
                "token it gave was revoked again. Ask your administrator to upgrade the server, and for a new code",
            )
        return {"token": token, "computer_key": key, "identity": {"id": user, "name": name}}

    def login_code(self) -> str:
        """The one-time link to the identity's own dashboard on the server, made here: this computer's address for the
        server, the dashboard's path and the code the server gave, when it is of a code's shape. The link the server
        answers is never used."""
        code = answer_of(self.request("POST", "login-code", headers=self._keyed())).get("code")
        if not (isinstance(code, str) and CODE.fullmatch(code)):
            raise ServerError(0, "the server's answer is not a sign-in code")
        return f"{self.url}{UI}/login?code={code}"

    def whoami(self) -> dict[str, Any]:
        """Who this computer's token is, as the server answers it, but its addresses."""
        return {
            name: value for name, value in answer_of(self.request("GET", "whoami")).items() if name not in _ADDRESSES
        }

    def put_identity(self, identity: Mapping[str, Any]) -> None:
        """The person's identity, as this computer finds it, added to what their other computers sent (with this
        computer's key)."""
        self.request("PUT", "identity", body=dict(identity), headers=self._keyed())

    def revoke(self) -> None:
        """This computer's token, revoked on the server (`latet disconnect`)."""
        self.request("DELETE", "token")


class _BridgeOnceClient(ServerClient):
    """A descriptor-bound helper's sole exchange; leaving never creates another client or rereads a pin."""

    def __init__(self, prepared: _PreparedConnection, end: float) -> None:
        connection = prepared.connection
        if connection is None or prepared.token is None or PERSONAL_TOKEN.fullmatch(prepared.token) is None:
            raise ServerError(0, "the helper binding is unavailable")
        self.url = _as_kept(connection.url)
        self.token, self.fingerprint = prepared.token, connection.fingerprint
        self._clock = time.monotonic
        self.timeout = end - self._clock()
        if self.timeout <= 0:
            raise ServerError(0, "the helper deadline has passed")
        verify = _tls_context(self.fingerprint, _data=prepared.selected.data)
        verify.check_hostname = True
        left = end - self._clock()
        if left <= 0:
            raise ServerError(0, "the helper deadline has passed")
        self._http = httpx.Client(
            verify=verify,
            timeout=httpx.Timeout(left, connect=min(5.0, left)),
            trust_env=False,
            follow_redirects=False,
        )
        self._owned = True

    def _leave(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _unreachable(self, exc: BaseException) -> str:
        return "the helper exchange is unavailable"


def _bridge_once(prepared: _PreparedConnection, operation: str, body: Any, end: float, receive: ReceiveBudget) -> bytes:
    """One fixed engine operation under the bootstrap's original deadline, without public request's retry."""
    from latet.server.tokens import route_key

    methods = {
        "ping": "GET",
        "route-check": "POST",
        "mask": "POST",
        "rehydrate": "POST",
        "scope": "POST",
        "verify": "POST",
        "instructions": "POST",
        "approvals/wait": "POST",
        "outbound": "POST",
    }
    if operation not in methods or prepared.token is None or PERSONAL_TOKEN.fullmatch(prepared.token) is None:
        raise ServerError(0, "the helper operation is unavailable")
    if operation == "approvals/wait" and end - time.monotonic() <= 20.0:
        raise TimeoutError  # Refuse before constructing even a network client when the hold cannot fit.
    with _BridgeOnceClient(prepared, end) as client:
        address = f"{client.url}/u/{route_key(prepared.token)}/_latet/engine/v1/{operation}"
        sent = {"accept-encoding": "identity", "x-latet-token": prepared.token}
        if operation == "approvals/wait" and end - client._clock() <= 20.0:
            raise TimeoutError  # The server's fixed20s hold must fit after binding/TLS/client preparation.
        answer = client._once(methods[operation], address, sent, body, 4 * 1024 * 1024 - 1, end, receive)
        if not 200 <= answer.status_code < 300:  # the status alone: the plugin says a refused token apart (R6, R7)
            raise ServerError(answer.status_code, "the helper exchange was refused")
        return answer.content
