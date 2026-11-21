"""What the tests of Enterprise's own fetches share (the identity provider, the SIEM, a term source's address and its
directory): each reaches a name at the next address its lookup checked when the TCP connection to the one before
failed, and never once a connection was made.

The name these tests reach is ``localhost``, resolved to the three addresses of ``LOCALHOST``, as egress checks them:
``::1``, where nothing listens (Windows gives it first, and a provider in Docker listens on 127.0.0.1 alone); 127.0.0.1,
where the test's server is; and ``PAST`` (127.0.0.2), which no fetch may reach once its connection to 127.0.0.1 was
made. pytest-socket lets a test connect to 127.0.0.1 and ``::1`` alone: the recorders below refuse ``PAST`` at once, as
a connection refused, without the network, and ``::1`` too when asked to."""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import httpcore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from latet.server import egress

LOCALHOST = ("::1", "127.0.0.1", "127.0.0.2")
PAST = "127.0.0.2"
METADATA = "169.254.169.254"  # the cloud's metadata address: never reached, even for an administrator's own service
OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"


def answering(body: bytes) -> bytes:
    """An HTTP/1.1 answer 200 of ``body``, as JSON."""
    head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body


def resolving(monkeypatch, *addresses: str) -> list[str]:
    """egress resolves every name to ``addresses``, in their order; the names it was asked, kept."""
    asked: list[str] = []

    def resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        asked.append(host)
        return [ipaddress.ip_address(address) for address in addresses]

    monkeypatch.setattr(egress, "resolve", resolve)
    return asked


def certificate(folder: Path, name: str = "localhost") -> tuple[Path, Path]:
    """A self-signed certificate for the DNS name ``name`` alone (no address: TLS checked against an address the fetch
    connected to fails), and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / f"{name}.crt", folder / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = serialization.PrivateFormat.PKCS8
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, private, serialization.NoEncryption()))
    return cert_path, key_path


def trusting(cert: Path) -> ssl.SSLContext:
    """A client's context that trusts ``cert`` and nothing else, the name checked."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cert)
    return context


def serving(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


class Server:
    """A server on 127.0.0.1 that reads each request whole (its head, and the body its Content-Length says), keeps it,
    and answers ``answer``, each connection in a thread of its own; with ``answer`` None it closes the connection once
    it has read the request, answering nothing. ``tls``: it speaks TLS with that context (a handshake the client
    refuses ends the connection)."""

    def __init__(self, answer: bytes | None = OK, *, tls: ssl.SSLContext | None = None) -> None:
        self.answer, self.tls = answer, tls
        self.requests: list[bytes] = []
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._kept, args=(conn,), daemon=True).start()

    def _kept(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            conn.settimeout(5)
            if self.tls is not None:
                conn = self.tls.wrap_socket(conn, server_side=True)
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        data = b""
        while True:
            while b"\r\n\r\n" not in data:
                if not (chunk := conn.recv(65536)):
                    return
                data += chunk
            head, _, data = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            length = next(
                (int(line.split(b":", 1)[1]) for line in lines if line.lower().startswith(b"content-length:")), 0
            )
            while len(data) < length:
                if not (chunk := conn.recv(65536)):
                    return
                data += chunk
            self.requests.append(head + b"\r\n\r\n" + data[:length])
            data = data[length:]
            if self.answer is None:
                return
            conn.sendall(self.answer)

    def close(self) -> None:
        self.sock.close()


def held_on_v6(port: int) -> socket.socket | None:
    """A socket bound to ``[::1]:port`` that does not listen, so that a connection there is refused, as a provider in
    Docker that listens on 127.0.0.1 alone refuses Windows's ``localhost``; None where that port is another program's
    on ``::1``, or this computer has no IPv6 loopback (a connection to ``::1`` fails all the same then)."""
    try:
        held = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        held.bind(("::1", port))
    except OSError:
        held.close()
        return None
    return held


@contextlib.contextmanager
def refused_on_v6(port: int):
    """``held_on_v6`` for the length of a block: ``::1`` refuses ``port`` there."""
    held = held_on_v6(port)
    try:
        yield held
    finally:
        if held is not None:
            held.close()


def async_connects(monkeypatch, refused: Iterable[str] = (PAST,)) -> list[str]:
    """Every TCP connection an asynchronous client asks httpcore's backend for, by its address; one to an address of
    ``refused`` fails at once, as a connection refused, the rest are made as asked."""
    asked: list[str] = []
    refusing = frozenset(refused)
    connect = httpcore.AnyIOBackend.connect_tcp

    async def recorded(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109 - httpcore's interface
        asked.append(host)
        if host in refusing:
            raise httpcore.ConnectError(f"[{host}]:{port} refused the connection")
        return await connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", recorded)
    return asked


def sync_connects(monkeypatch, refused: Iterable[str] = (PAST,)) -> list[str]:
    """``async_connects`` for a synchronous client: httpcore's ``SyncBackend``."""
    asked: list[str] = []
    refusing = frozenset(refused)
    connect = httpcore.SyncBackend.connect_tcp

    def recorded(self, host, port, timeout=None, local_address=None, socket_options=None):
        asked.append(host)
        if host in refusing:
            raise httpcore.ConnectError(f"[{host}]:{port} refused the connection")
        return connect(self, host, port, timeout, local_address, socket_options)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", recorded)
    return asked


def socket_connects(monkeypatch, refused: Iterable[str] = (PAST,)) -> list[str]:
    """Every TCP connection asked of ``socket.create_connection``, by its address; one to an address of ``refused``
    refused at once."""
    asked: list[str] = []
    refusing = frozenset(refused)
    connect: Callable[..., socket.socket] = socket.create_connection

    def recorded(address, *args, **kwargs):
        asked.append(address[0])
        if address[0] in refusing:
            raise ConnectionRefusedError(f"[{address[0]}]:{address[1]} refused the connection")
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", recorded)
    return asked
