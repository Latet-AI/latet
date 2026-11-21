"""LDAP and Active Directory, read with ldap3 (LGPL-3.0-or-later, used as a separate, unmodified package), which Latet's
optional `ldap` extra installs. The directory is reached at the address egress resolved and checked (a directory inside
the company's network allowed), over LDAPS or STARTTLS, its certificate checked against the name written; referrals are
never followed. A name with several addresses is reached at the next one the same lookup checked when the TCP
connection to the one before failed: never once a connection was made, so never once TLS, STARTTLS or the sign-in
began. No value from a source or a request is ever put into the filter or a DN: they are the administrator's,
sent as written."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import truststore

from latet.ee.settings import LdapSource, default_port, mapped_fields
from latet.ee.sync.records import SecretOf, SocketCut, SourceError, bounds, cut_at_deadline, no_secret, read_inside
from latet.ee.sync.sources import holder
from latet.fileio import FileReadError
from latet.server import egress

PAGE = 500  # entries asked for at a time (Active Directory gives at most 1000)
CONNECT_SECONDS = 10.0  # the TCP connections to the directory's addresses, together (``egress.tries``)
MOST_ENTRIES = 200_000
MOST_PAGES = MOST_ENTRIES // PAGE + 1  # a directory that keeps sending a cookie is read this often at most (B-I1)
_PAGED = "1.2.840.113556.1.4.319"  # the simple paged results control
_AUTHORITY_BYTES = 1 << 20  # a certificate authority's file is read this far
_PEM = "-----BEGIN CERTIFICATE-----"
NOT_INSTALLED = "LDAP needs Latet's `ldap` extra, which the container image has: docs.latet.ai/enterprise/term-sync/"

Connect = Callable[[LdapSource, str | None, Path], Any]  # an ldap3 Connection to the source's directory, not signed in


def installed() -> bool:
    """Whether this instance's install has Latet's `ldap` extra (ldap3), found without importing it."""
    return importlib.util.find_spec("ldap3") is not None


class _Cut(SocketCut):
    """Ends a directory's reads from another thread (``records.cut_at_deadline``), as an address's reader does
    (``records.SocketCut``): a duplicate of the connection's socket is kept as it connects, before TLS takes it over,
    and ``cut`` shuts the duplicates down; never ``unbind()``, which sends and can wait itself. Once cut, a socket kept
    after makes its read fail at once. ``checked`` says whether the TLS of the connection ``_connect`` made came up by
    Latet's own handshake: None where Latet made no connection needing TLS (a loopback directory without it, or a
    connection given: the tests'), False until that handshake ran (the review's Minor 4: no sign-in before it).
    ``refused`` says that handshake failed (Latet's check refused the directory's certificate, or the handshake did not
    complete): nothing more is sent on that connection, not even ldap3's closing ``unbind()`` (``_close``).
    ``untrusted`` says it failed because Latet's check refused the certificate itself (its authority, its name, its
    dates): the source is then refused in those words (``_untrusted``), never as a directory that did not answer.
    ``connected`` says a TCP connection of the read was made (Latet's TLS handshake began on it, or ldap3 opened it):
    no other address of the directory's is tried after that (``_connect``)."""

    def __init__(self) -> None:
        super().__init__()
        self.checked: bool | None = None
        self.refused = False
        self.untrusted = False
        self.connected = False

    def keep(self, sock: Any) -> None:
        """Called in the reader's thread once ``sock`` is connected, before any read on it."""
        if not isinstance(sock, socket.socket) or isinstance(sock, ssl.SSLSocket):  # kept before TLS wrapped it
            return
        if self.hold(sock.dup()):
            raise ConnectionAbortedError("the run's deadline passed")


# The cut of the read under way in this thread, for `_connect`, which ldap3's own calls reach (the TLS handshake).
_CUT: ContextVar[_Cut | None] = ContextVar("latet_term_sync_ldap_cut", default=None)


@contextmanager
def _cutting() -> Iterator[_Cut]:
    """A read's cut, ``_connect``'s while it is open (in this thread); its duplicates closed after."""
    cut = _Cut()
    token = _CUT.set(cut)
    try:
        yield cut
    finally:
        _CUT.reset(token)
        cut.release()


def read_ldap(
    source: LdapSource, secret: SecretOf, *, folder: Path, connect: Connect | None = None
) -> list[dict[str, Any]]:
    """The entries ``source``'s search finds, with the attributes its mapping reads (those its secret is bound to,
    B-I3), page by page. The whole read, the connection's opening and TLS included, runs under the run's watchdog
    (``records.cut_at_deadline``), and each operation and each page checks the run's deadline and bytes itself (B-I1,
    B-M5). Every reason is in fixed words: no host, DN, filter, path, secret or term."""
    try:
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        raise SourceError("not-installed", NOT_INSTALLED) from None
    password = secret() if source.bind is not None else None
    if source.bind is not None and password is None:
        raise no_secret(source.name)
    attributes = mapped_fields(source)  # what the secret is bound to read (B-I3)
    # The cut's duplicates are closed once the watchdog's timer is cancelled (the inner block left first): no cut after.
    # Mapped inside the watchdog: once it has cut the connection, whatever the read raised is `timeout`.
    with _cutting() as cut, cut_at_deadline(cut.cut):
        try:
            return _read(connect or _connect, cut, source, password, folder, attributes)
        except ImportError:
            raise SourceError("not-installed", NOT_INSTALLED) from None
        except LDAPException as exc:
            raise (_untrusted(source) if cut.untrusted else _failed(exc)) from None
        except OSError as exc:  # a socket `_connect` could not keep for the cut: as ldap3 says it on the LDAPS path
            if cut.untrusted:
                raise _untrusted(source) from None
            raise SourceError("unreachable", f"the directory did not answer ({type(exc).__name__})") from None


def _untrusted(source: LdapSource) -> SourceError:
    """A directory whose certificate Latet's own check refused: what to check, in fixed words, never anything of the
    certificate or of the directory (its subject, its names, the host)."""
    if source.certificate is not None:
        where = "the certificate authority's file (`certificate`)"
    else:
        where = "that the server's certificate store holds the authority that signed it"
    return SourceError(
        "refused",
        f"the directory's certificate was not trusted: check {where}, and that the certificate names the host in `url`",
    )


def _failed(exc: Exception) -> SourceError:
    """ldap3's failure in fixed words, by its type: the network's or TLS's (`unreachable`), else ldap3's refusal of the
    source's own settings before anything was sent, such as a filter or a DN it does not take (`unreadable`)."""
    from ldap3.core.exceptions import (
        LDAPCommunicationError,
        LDAPMaximumRetriesError,
        LDAPResponseTimeoutError,
        LDAPStartTLSError,
    )

    kind = type(exc).__name__
    if isinstance(exc, (LDAPCommunicationError, LDAPStartTLSError, LDAPResponseTimeoutError, LDAPMaximumRetriesError)):
        return SourceError("unreachable", f"the directory did not answer ({kind})")
    return SourceError("unreadable", f"the source's filter, base or bind is not one LDAP takes ({kind})")


def _read(
    connect: Connect, cut: _Cut, source: LdapSource, password: str | None, folder: Path, attributes: list[str]
) -> list[dict[str, Any]]:
    bounds().check()
    conn = connect(source, password, folder)
    try:
        if (urlsplit(source.url).scheme == "ldaps" or source.start_tls) and not (conn.server.ssl or conn.tls_started):
            # STARTTLS can answer False without raising: no sign-in crosses without TLS (B-M7)
            raise SourceError("refused", "the directory did not start TLS: Latet does not sign in without it")
        if cut.checked is False:  # TLS came up by another check than Latet's (ldap3's also takes the pinned address)
            raise SourceError(
                "refused", "Latet did not check the directory's certificate: it does not sign in without it"
            )
        bounds().check()
        if not conn.bind():
            raise SourceError("refused", f"the directory refused the sign-in ({_described(conn)})")
        return _search(conn, source, attributes)
    finally:
        _close(conn, sign_out=not cut.refused)


def _close(conn: Any, *, sign_out: bool = True) -> None:
    """Signs out, as a courtesy; a connection cut, or dropped by the directory, cannot send it, and is closed all the
    same: its failure never takes the place of the read's own. ``sign_out`` False: a connection whose TLS handshake
    Latet's check refused (``_Cut.refused``), on which nothing more is sent, ldap3's ``unbind()`` included (it holds no
    DN or password, and the directory already saw the handshake): its socket is just closed."""
    if sign_out:
        with contextlib.suppress(Exception):
            conn.unbind()
    left = getattr(conn, "socket", None)  # ldap3 forgets the socket it closed; one it did not open fully stays
    if isinstance(left, socket.socket):
        with contextlib.suppress(OSError):
            left.close()


def _described(conn: Any) -> str:
    """ldap3's word for a result (`invalidCredentials`), never its message, which a directory may fill with a DN."""
    word = (conn.result or {}).get("description")
    return word if isinstance(word, str) and word.isidentifier() else "refused"


def _cookie(result: dict[str, Any]) -> bytes | None:
    """The paged results' cookie a page's result carries; None at the last page."""
    control = (result.get("controls") or {}).get(_PAGED)
    value = control.get("value") if isinstance(control, dict) else None
    cookie = value.get("cookie") if isinstance(value, dict) else None
    return cookie if isinstance(cookie, bytes) and cookie else None


def _size(attributes: dict[str, Any]) -> int:
    """An entry's attributes as the run's budget counts them: the text of each of their values."""
    return sum(
        len(str(value))
        for values in attributes.values()
        for value in (values if isinstance(values, list) else [values])
    )


def _search(conn: Any, source: LdapSource, attributes: list[str]) -> list[dict[str, Any]]:
    """Every entry the search finds, a page at a time: the attributes the mapping names, by their names in it. The
    run's deadline is checked before each page is asked and once it is read, each entry's attribute bytes are taken
    from the run's budget as it is read, and at most ``MOST_PAGES`` pages are asked (B-I1, B-M5). ldap3 holds each page
    whole before its bytes are counted (B-R4): the budget is counted per entry after that, never claimed as a bound on
    the page. A reference to another directory among the entries is left out (referrals are not followed)."""
    limits = bounds()
    records: list[dict[str, Any]] = []
    cookie: bytes | None = None
    for _ in range(MOST_PAGES):
        limits.check()
        conn.search(source.base, source.filter, attributes=attributes, paged_size=PAGE, paged_cookie=cookie)
        limits.check()
        result = conn.result or {}
        if result.get("result") not in (0, 4):  # success; a size limit gives what came, page by page
            raise SourceError("unreadable", f"the directory answered {_described(conn)}")
        for entry in conn.response or ():
            if entry.get("type") == "searchResEntry":
                found = entry.get("attributes") or {}
                limits.take(_size(found))
                records.append({name: found.get(name) for name in attributes})  # ldap3's names ignore case
        if len(records) > MOST_ENTRIES:
            raise SourceError("too-many", f"more than {MOST_ENTRIES:,} entries: narrow `base` or `filter`")
        cookie = _cookie(result)
        if cookie is None:
            return records
    raise SourceError("too-many", f"more than {MOST_PAGES:,} pages: narrow `base` or `filter`")


def pinned_addresses(source: LdapSource) -> tuple[tuple[str, ...], int]:
    """The addresses egress resolves and checks for the directory now (its lookups counted for the source), in the
    order its lookup gave them, eight at most, each written as a number (``egress.checked``); and the port: the
    scheme's when none is written."""
    parts = urlsplit(source.url)
    port = parts.port or default_port(parts.scheme)
    netloc = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    refused = SourceError("unreachable", "the server does not connect to this source's directory")
    try:  # egress reads an https address: the scheme only carries the host and the port written
        pinned, _, extensions = asyncio.run(egress.pin(f"https://{netloc}:{port}/", holder=holder(source), inside=True))
    except egress.EgressRefused:  # its words name the host (B-M4)
        raise refused from None
    addresses = egress.checked(extensions, urlsplit(pinned).hostname or "")
    if not addresses:  # never a name, which ldap3 would look up
        raise refused
    return addresses, port


def _authority(source: LdapSource, folder: Path) -> str | None:
    """The certificate authority's file the source names, read as ``sources.read_file`` reads (B-M8,
    ``records.read_inside``): a file name alone, directly inside the term-sync folder, no link followed; its reasons
    name no path (B-M4)."""
    if source.certificate is None:
        return None
    try:
        return read_inside(folder, source.certificate, limit=_AUTHORITY_BYTES, what="certificate file").decode("ascii")
    except (FileReadError, OSError, UnicodeDecodeError) as exc:  # said by its type alone: an OSError's words name it
        said = f"the source's certificate file cannot be read ({type(exc).__name__})"
        raise SourceError("unreachable", said) from None


def _context(authority: str | None) -> ssl.SSLContext:
    """The context of the directory's TLS: the source's authority alone when it names one, else the system's (a
    company's own authority is in it, as for E4's outputs); the name checked in the handshake."""
    if authority is None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if _PEM not in authority:  # an empty file would trust the system's authorities, not the one named
        raise SourceError("unreadable", "the source's certificate file holds no PEM certificate")
    try:
        return ssl.create_default_context(cadata=authority)
    except (ssl.SSLError, ValueError):
        raise SourceError("unreadable", "the source's certificate file holds no PEM certificate") from None


def _tls(name: str, authority: str | None, cut: _Cut) -> Any:
    """ldap3's Tls for the name written (``sni``, ``valid_names``), its handshake Python's own: the certificate checked
    in it against ``name`` alone. ldap3's own check also accepts the address it connects to, egress's and not the name
    written, so a name that led elsewhere could get the password across with a certificate for that address. Before
    the handshake, the connection's socket is kept for the cut."""
    from ldap3 import Tls

    context = _context(authority)

    class _Checked(Tls):
        def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
            cut.connected = True  # the TCP connection made: whatever fails from here tries no other address
            cut.keep(connection.socket)  # connected, nothing read yet: from here the cut reaches it
            # ldap3 asks for the handshake on both its paths (LDAPS, STARTTLS): done here, the name checked in it
            try:
                connection.socket = context.wrap_socket(connection.socket, server_hostname=name)
            except BaseException as exc:  # refused or not completed: nothing more goes on this connection (_close)
                cut.refused = True
                cut.untrusted = isinstance(exc, ssl.SSLCertVerificationError)  # the certificate itself refused
                raise
            cut.checked = True

    return _Checked(validate=ssl.CERT_REQUIRED, ca_certs_data=authority, valid_names=[name], sni=name)


def _peered(conn: Any) -> bool:
    """Whether the socket ldap3 opened for ``conn`` is connected (it has a peer): a TCP connection was made."""
    sock = getattr(conn, "socket", None)
    if not isinstance(sock, socket.socket):
        return False
    try:
        sock.getpeername()
    except OSError:
        return False
    return True


def _connect(source: LdapSource, password: str | None, folder: Path) -> Any:
    """The source's directory, connected at the first of the addresses egress pinned that takes a TCP connection, in
    their order, and, but for a loopback directory without TLS, over TLS that Latet's own handshake set up; not signed
    in. The next address is tried only when the TCP connection to the one before failed (refused, unreachable, not made
    in its share of ``CONNECT_SECONDS``, ``egress.tries``): never once a connection was made, so never once TLS,
    STARTTLS or the sign-in began. Made within ``read_ldap`` alone, whose cut reaches every socket it opens (the
    review's Minor 5). ``conn`` is never formatted, logged or put in an exception's words: ldap3's ``repr`` of a
    connection holds the password."""
    from ldap3 import NONE, Connection, Server

    cut = _CUT.get()
    if cut is None:  # never a socket no read's cut reaches, nor duplicates no one closes
        raise RuntimeError("a directory's connection is made within read_ldap alone")
    parts = urlsplit(source.url)
    name = parts.hostname or ""
    addresses, port = pinned_addresses(source)
    tls = _tls(name, _authority(source, folder), cut)
    failed: BaseException | None = None
    for address, seconds in egress.tries(addresses, CONNECT_SECONDS):
        server = Server(
            address, port=port, use_ssl=parts.scheme == "ldaps", tls=tls, get_info=NONE, connect_timeout=seconds
        )
        conn = Connection(
            server, user=source.bind, password=password, receive_timeout=30, read_only=True, auto_referrals=False
        )
        if parts.scheme == "ldaps" or source.start_tls:
            cut.checked = False  # until Latet's handshake runs (_tls): `_read` signs in only after it
        try:
            conn.open(read_server_info=False)  # LDAPS: TLS's handshake in it, its socket kept just before (_tls)
        except BaseException as exc:  # never left open
            made = cut.connected or _peered(conn)
            _close(conn, sign_out=made and not cut.refused)
            if made or not isinstance(exc, Exception):  # a connection was made: no other address
                raise
            failed = exc  # the TCP connection failed: nothing was sent, the next address is tried
            continue
        cut.connected = True
        try:
            cut.keep(conn.socket)  # ldap://: kept now, before anything is read on it
            if parts.scheme == "ldap" and source.start_tls:
                conn.start_tls(read_server_info=False)
        except BaseException:  # never left open: a connection the cut could not reach, or one that failed
            _close(conn, sign_out=not cut.refused)
            raise
        return conn
    raise failed or OSError("no address of the directory was tried")
