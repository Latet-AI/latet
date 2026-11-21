"""Reading a source's records: a file inside the server's term-sync folder, or an address, each request sent to the
address E0b's egress resolved and checked at that moment. Microsoft Entra ID and LDAP have modules of their own;
``readers`` gives each kind's reader."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Iterable
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote, urlsplit

import httpcore
import httpx
import truststore
from httpcore import SOCKET_OPTION, NetworkStream

from latet.ee.settings import FileSource, UrlSource, mapped_fields
from latet.ee.sync.records import (
    MAX_BYTES,
    MAX_YAML_BYTES,
    SecretOf,
    SocketCut,
    SourceError,
    bound_words,
    bounds,
    cut_at_deadline,
    no_secret,
    parse_records,
    read_inside,
)
from latet.fileio import FileReadError
from latet.gateway import request_transport
from latet.server import egress

Reader = Callable[[Any, SecretOf], Iterable[dict[str, Any]]]  # a CSV file's rows read as they are iterated
TIMEOUT = 30.0  # each phase of a request (connect, write, read, pool), at most: less when the run has less left
FLOOR = 1.0  # a phase's timeout is never shorter: the run's deadline, checked before, ends it
_FORMATS = {".csv": "csv", ".json": "json", ".yaml": "yaml", ".yml": "yaml"}
_NAMES = {"csv": "CSV", "json": "JSON", "yaml": "YAML"}
_KEPT_IN_ADDRESS = "-._~"  # what a secret keeps as it is in an address: the rest percent-encoded
_IDENTITY = ("", "identity")  # the content encodings read: none


def holder(source: Any) -> str:
    """Whose lookups egress counts for ``source``: its own."""
    return f"term-sync:{source.name}"


class _Sockets(httpcore.SyncBackend):
    """httpcore's own network backend, keeping a duplicate of each connection's socket it opens (``records.SocketCut``,
    as a directory's reader keeps its own), so that ``cut`` can shut them down from another thread
    (``records.cut_at_deadline``). Once cut, a connection opened after is shut down at once. ``release`` closes the
    duplicates."""

    def __init__(self) -> None:
        super().__init__()
        self._duplicates = SocketCut()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> NetworkStream:
        stream = super().connect_tcp(host, port, timeout, local_address, socket_options)
        try:
            kept = stream.get_extra_info("socket").dup()
        except BaseException:  # no descriptor left: a connection no cut could reach is never left open
            stream.close()
            raise
        self._duplicates.hold(kept)
        return stream

    def cut(self) -> None:
        self._duplicates.cut()

    def release(self) -> None:
        self._duplicates.release()


class _Transport(httpx.HTTPTransport):
    """httpx's own transport, its connections opened through ``sockets``: httpx gives no way to name the network
    backend, so its pool is made again with one (the attribute every httpx 0.2x reads, the trickling-answer test's).
    That pool is ``request_transport.reaching_sync``'s, with httpcore's own limits and no retry, as before: a name with
    several addresses is reached at the next one the same lookup checked when the TCP connection to the one before
    failed, before any byte of the request left, TLS checked against the name written at each, and a connection kept
    serves only its address and TLS name."""

    def __init__(self, sockets: _Sockets, context: ssl.SSLContext) -> None:
        super().__init__(verify=context, retries=0)
        self._pool = request_transport.reaching_sync(context, sockets)


class _Pinned(httpx.BaseTransport):
    """Each request sent to the address egress resolves and checks for it now, its ``Host`` and TLS's name the ones
    written, with no proxy and no retry of httpx's own (E4's PinnedTransport, synchronous), the addresses that lookup
    checked handed with it (``egress.ADDRESSES``) to the pool, which tries the next only when the TCP connection to the
    one before failed. ``cut`` ends every connection open now, from any thread: those of its own sockets, else (a
    transport given, the tests') a close."""

    def __init__(
        self, holder: str, inside: bool, transport: httpx.BaseTransport, sockets: _Sockets | None = None
    ) -> None:
        self._holder, self._inside, self._sent, self._sockets = holder, inside, transport, sockets

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            url, host, extensions = asyncio.run(egress.pin(str(request.url), holder=self._holder, inside=self._inside))
        except egress.EgressRefused:  # its words name the host: the status shows a reason to whoever reads it (B-M4)
            raise SourceError("unreachable", "the server does not connect to this source's address") from None
        request.url = httpx.URL(url)
        request.headers["host"] = host
        request.extensions = {**request.extensions, **extensions}
        return self._sent.handle_request(request)

    def cut(self) -> None:
        if self._sockets is not None:
            self._sockets.cut()
        else:
            self._sent.close()

    def close(self) -> None:
        try:
            self._sent.close()
        finally:
            if self._sockets is not None:
                self._sockets.release()


class PinnedClient(httpx.Client):
    """``pinned_client``'s: an httpx client whose ``cut()`` ends every connection it has open now, from any thread. A
    client that was cut shuts down every connection it opens after, so it is never reused across runs: one client for
    each read (as ``read_url`` makes one), closed with it."""

    def __init__(self, pinned: _Pinned) -> None:
        super().__init__(transport=pinned, follow_redirects=False, timeout=TIMEOUT, trust_env=False)
        self._pinned = pinned

    def cut(self) -> None:
        self._pinned.cut()


def pinned_client(holder: str, *, inside: bool, transport: httpx.BaseTransport | None = None) -> PinnedClient:
    """A client whose every request goes where egress checked, following no redirect, within ``TIMEOUT`` seconds; a
    reader reads with it within ``records.cut_at_deadline(client.cut)``, so that no answer trickles in past the run's
    deadline."""
    if transport is not None:
        return PinnedClient(_Pinned(holder, inside, transport))
    sockets = _Sockets()
    sent = _Transport(sockets, truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    return PinnedClient(_Pinned(holder, inside, sent, sockets))


def cut_of(client: httpx.Client) -> Callable[[], None]:
    """How to end ``client``'s reads from another thread: a pinned client's own ``cut``, else (a client given, the
    tests') its close."""
    return client.cut if isinstance(client, PinnedClient) else client.close


def _format(path: str) -> str:
    """A document's format by the extension of its file or of its address's path, JSON by default: never by what an
    address answers (the final security review's L1), so that whoever sets the sources chooses the parser."""
    return _FORMATS.get(PurePath(path).suffix.lower(), "json")


def _answered(content_type: str) -> str | None:
    """The format an answer's content type names, if it names one: only to refuse an answer of another format than the
    source reads, never to choose how it is read."""
    kind = content_type.split(";")[0].strip().lower()
    return "csv" if kind == "text/csv" else "yaml" if "yaml" in kind else "json" if "json" in kind else None


def _most(fmt: str) -> int:
    """The bytes a document of ``fmt`` may have (the final security review's L1): YAML's own bound, else
    ``MAX_BYTES``."""
    return MAX_YAML_BYTES if fmt == "yaml" else MAX_BYTES


def _parse(data: bytes, fmt: str, source: FileSource | UrlSource) -> Iterable[dict[str, Any]]:
    """The records of a file's or an address's document, each holding the fields ``source``'s mapping reads alone."""
    return parse_records(
        data, fmt, records=source.records, delimiter=source.delimiter, encoding=source.encoding,
        fields=mapped_fields(source),
    )  # fmt: skip


def read_file(source: FileSource, secret: SecretOf, *, folder: Path) -> Iterable[dict[str, Any]]:
    """The records of the file ``source`` names inside ``folder`` (the server's term-sync folder), read as it stands
    now: one that resolves outside the folder (a link out of it) is refused (``records.read_inside``, a directory's
    certificate's check too). A file name with no folder of its own (B-M8): no folder between the check and the open to
    swap for a link. Its reasons name no path (B-M4)."""
    bounds().check()
    fmt = source.format or _format(source.path)
    most = _most(fmt)
    try:  # opened without following a link put in its place since it was resolved; a regular file alone
        data = read_inside(folder, source.path, limit=most, what="file")
    except FileReadError as exc:  # fixed words that hold no path (latet.fileio)
        if exc.reason == "too_large":  # the bound as it is set (fileio's words say the limit it was given)
            said = f"the source's file holds more than {bound_words(fmt, most)}: not read"
            raise SourceError("too-large", said) from None
        raise SourceError("unreachable", f"the source's file {exc}") from None
    except OSError as exc:
        raise SourceError("unreachable", f"the source's file cannot be read ({type(exc).__name__})") from None
    bounds().take(len(data))
    return _parse(data, fmt, source)


def _filled(text: str, secret: str | None, source: str, *, address: bool = False) -> str:
    """``text`` with the source's secret where ``{secret}`` stands, percent-encoded in an ``address``; a secret that
    cannot go in a header (not visible ASCII) refused, saying to set it again."""
    if "{secret}" not in text:
        return text
    if secret is None:
        raise no_secret(source)
    filled = text.replace("{secret}", quote(secret, safe=_KEPT_IN_ADDRESS) if address else secret)
    if not address and not all(32 <= ord(c) <= 126 for c in filled):
        raise SourceError(
            "no-secret", f"its secret cannot go in a header: set it again (`latet server terms secret {source}`)"
        )
    return filled


def read_url(source: UrlSource, secret: SecretOf, *, client: httpx.Client | None = None) -> Iterable[dict[str, Any]]:
    """The records an address gives, read once, up to ``MAX_BYTES`` (YAML's own bound for YAML); any answer but 200 a
    failure, a redirect included. Nothing compressed is asked for or read (as E3's identity provider): a few compressed
    bytes can stand for gigabytes, which a bound counted on what is decoded would only see once they were in memory.
    The format is the source's, else its path's extension's, else JSON: an answer whose content type names another is
    refused before its body is read (the final security review's L1)."""
    fmt = source.format or _format(urlsplit(source.url).path)
    most = _most(fmt)
    kept = secret() if "{secret}" in source.url + "".join(source.headers.values()) else None
    url = _filled(source.url, kept, source.name, address=True)
    headers = {name: _filled(value, kept, source.name) for name, value in source.headers.items()}
    headers["accept-encoding"] = "identity"  # a source's headers never set it (settings._RESERVED)
    http = client or pinned_client(holder(source), inside=True)
    limits = bounds()  # the run's deadline and bytes, checked at each chunk (B-I1, B-M5)
    limits.check()
    # Fix round 1's 6: no phase waits past the run's deadline, so none outlives the turn by a socket's timeout.
    timeout = min(TIMEOUT, max(limits.left(), FLOOR))
    data = bytearray()
    try:
        # Task 7's fix round: an answer that trickles in (its headers a byte at a time) passes no socket's timeout, and
        # the deadline is checked between two chunks of its body alone: the connection is cut at the deadline.
        with (
            cut_at_deadline(cut_of(http)),
            http.stream("GET", url, headers=headers, follow_redirects=False, timeout=timeout) as response,
        ):
            if response.status_code != 200:
                code = "refused" if response.status_code in (401, 403) else "unreachable"
                raise SourceError(code, f"the address answered {response.status_code}")
            if response.headers.get("content-encoding", "").strip().lower() not in _IDENTITY:
                raise SourceError(
                    "unreadable", "the address answered compressed, though Latet asked for no compression"
                )
            answered = _answered(response.headers.get("content-type", ""))
            if answered is not None and answered != fmt:
                said = f"the address answered {_NAMES[answered]} where the source reads {_NAMES[fmt]}: set `format`"
                raise SourceError("unreadable", said)
            for chunk in response.iter_bytes():
                limits.check()
                limits.take(len(chunk))
                data += chunk
                if len(data) > most:
                    raise SourceError("too-large", f"larger than {bound_words(fmt, most)}: not read")
    except httpx.HTTPError as exc:
        raise SourceError("unreachable", f"no answer ({type(exc).__name__})") from None
    finally:
        if client is None:
            http.close()
    return _parse(bytes(data), fmt, source)


def readers(folder: Path) -> dict[str, Reader]:
    """Each kind's reader, a file source's and a directory's reading inside ``folder`` (the server's term-sync folder:
    a directory's certificate authority's file)."""
    from latet.ee.sync.graph import read_graph  # here: graph.py reads this module's pinned client
    from latet.ee.sync.ldap import read_ldap  # ldap.py imports ldap3 (the `ldap` extra) only when it reads

    return {
        "file": lambda source, secret: read_file(source, secret, folder=folder),
        "url": read_url,
        "graph": read_graph,
        "ldap": lambda source, secret: read_ldap(source, secret, folder=folder),
    }
