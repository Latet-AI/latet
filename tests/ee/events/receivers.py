"""Receivers of a SIEM on 127.0.0.1, with a certificate of their own: an HTTPS endpoint (a webhook, Splunk's HEC) that
keeps what it takes and answers ``status``, and a syslog listener over TLS (RFC 5425 framing). Each can stop and start
again on the same port: a pause in the network."""

import datetime
import ipaddress
import json
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def certificate(folder: Path, name: str = "siem") -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    usage = x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
    )  # fmt: skip
    names = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(names, critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = folder / f"{name}.crt", folder / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = serialization.PrivateFormat.PKCS8
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, private, serialization.NoEncryption()))
    return cert_path, key_path


def trusting(cert: Path) -> ssl.SSLContext:
    """A client's context that trusts ``cert`` and nothing else."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cert)
    return context


def until(predicate, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to hold: senders and receivers work on threads of their own."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serving(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


class HttpsReceiver:
    """An HTTPS endpoint (``tls=False``: plain HTTP on 127.0.0.1) that keeps each body it takes, with its headers."""

    def __init__(self, cert: Path, key: Path, *, tls: bool = True) -> None:
        self.cert, self.key, self.tls = cert, key, tls
        self.port = _free_port()
        self.status = 200
        self.answer = b'{"text":"Success","code":0}'
        self.encoding: str | None = None  # the content-encoding its answer says it is in (C1's tests)
        self.drip = 0.0  # seconds between the bytes of its answer: a receiver that drips it (C1's tests)
        self.received: list[tuple[dict[str, str], bytes]] = []
        self.paths: list[str] = []  # the path of each request, its query with it
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"{'https' if self.tls else 'http'}://127.0.0.1:{self.port}/collect"

    def start(self) -> "HttpsReceiver":
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                receiver.paths.append(self.path)
                if 200 <= receiver.status < 300:
                    receiver.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
                answer = receiver.answer
                self.send_response(receiver.status)
                self.send_header("content-type", "application/json")
                if receiver.encoding:
                    self.send_header("content-encoding", receiver.encoding)
                self.send_header("content-length", str(len(answer)))
                self.end_headers()
                try:
                    if receiver.drip:
                        for byte in answer:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(receiver.drip)
                    else:
                        self.wfile.write(answer)
                except OSError:  # the sender gave up (a drip cut at its deadline)
                    pass

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        if self.tls:
            httpd.socket = _serving(self.cert, self.key).wrap_socket(httpd.socket, server_side=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._httpd = httpd
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def events(self, kind: str = "json") -> list[dict]:
        """What it took, in order: JSON arrays, NDJSON lines, or Splunk's envelopes one after the other."""
        out: list[dict] = []
        for _, body in self.received:
            text = body.decode()
            if kind == "json":
                out += json.loads(text)
            elif kind == "ndjson":
                out += [json.loads(line) for line in text.splitlines() if line]
            else:
                decoder, at = json.JSONDecoder(), 0
                while at < len(text):
                    envelope, at = decoder.raw_decode(text, at)
                    out.append(envelope)
        return out


class SyslogReceiver:
    """A syslog listener over TLS: each message of the RFC 5425 framing (its length, a space, the message)."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.cert, self.key = cert, key
        self.port = _free_port()
        self.messages: list[str] = []
        self._server: socketserver.ThreadingTCPServer | None = None

    @property
    def url(self) -> str:
        return f"tls://127.0.0.1:{self.port}"

    def start(self) -> "SyslogReceiver":
        receiver = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                while chunk := self.request.recv(65536):
                    data += chunk
                while data:
                    size, _, rest = data.partition(b" ")
                    receiver.messages.append(rest[: int(size)].decode())
                    data = rest[int(size) :]

        server = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), Handler, bind_and_activate=False)
        server.allow_reuse_address = True
        server.daemon_threads = True
        server.server_bind()
        server.server_activate()
        server.socket = _serving(self.cert, self.key).wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
