"""A self-signed certificate for a Latet server, for a first try: each computer pins it by its fingerprint
(`latet connect --fingerprint`). A certificate of the company's authority, or a public one, needs none of this.

It names the host of the server's address, is no authority (CA:FALSE, no key usage), and is constrained to that host
(a critical name constraint), so that a certificate its key signs for another host is accepted by no client that
trusts it: OpenSSL, which Python and Node use, refuses a CA:FALSE certificate as an issuer; rustls, which Codex uses for
CODEX_CA_CERTIFICATE, takes from a certificate it trusts its subject, its key and its name constraints, not its
CA:FALSE, and the constraint is what holds there. Python's ssl accepts the certificate itself so constrained, pinned
(partial chains) or as one more authority; whether each agent does is checked with the spike's harness before an agent
is given it. So the server's key is kept as an authority's would be.

A certificate pinned for a server is never an authority (``is_authority``, kept with the other pinning helpers in
``latet.engine.reviewer.certificate`` and given here): as one more authority, a CA:TRUE one (what ``openssl req
-x509`` makes by default) would have every agent of every computer that pinned it trust whatever its key signs, for
any host."""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from latet.config import write_atomic
from latet.engine.reviewer.certificate import fingerprint_of, is_authority
from latet.server.connection import server_url
from latet.storage.folders import private_folder_problem

__all__ = ["AUTHORITY", "is_authority", "is_self_signed", "make_certificate"]

AUTHORITY = (
    "is an authority (CA:TRUE, no CA:FALSE, or more than one certificate): computers given it would trust whatever "
    "its key signs, for any address. A server's own certificate is one certificate that says CA:FALSE: `latet server "
    "certificate --make`, or openssl req -x509 ... -addext basicConstraints=critical,CA:FALSE"
)


def make_certificate(
    url: str, folder: Path, days: int = 825, now: datetime.datetime | None = None
) -> tuple[Path, Path, str]:
    """``server.crt`` and ``server.key`` (private) in ``folder``, for the host of ``url`` as ``server_url`` keeps it
    (a name's A-labels: certificates hold no other form); and the fingerprint. 825 days is the longest the systems that
    trust such a certificate accept. ``folder`` is made the person's alone when it is not there (0700 on POSIX, whatever
    the umask), and the key is written only in a folder that is theirs alone, on every system (on Windows a file there
    takes its folder's access; on POSIX another account that may write there could swap the key and the certificate
    before the fingerprint is read): raises PermissionError otherwise."""
    host = urlsplit(server_url(url)).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name: x509.GeneralName = x509.DNSName(host)
        # The name alone; an address only 0.0.0.0 and ::, which no server is: no address is permitted.
        permitted: list[x509.GeneralName] = [
            name,
            x509.IPAddress(ipaddress.ip_network("0.0.0.0/32")),
            x509.IPAddress(ipaddress.ip_network("::/128")),
        ]
    else:
        name = x509.IPAddress(address)
        # The address alone; a name only "invalid", which no server is (RFC 6761): no name is permitted.
        permitted = [
            x509.IPAddress(ipaddress.ip_network(f"{address}/{address.max_prefixlen}")),
            x509.DNSName("invalid"),
        ]
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    start = (now or datetime.datetime.now(datetime.UTC)) - datetime.timedelta(minutes=5)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([name]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (problem := private_folder_problem(folder)) is not None:
        raise PermissionError(f"the server's key is not written: {problem}")
    write_atomic(folder / "server.key", private, private=True)
    write_atomic(folder / "server.crt", pem)
    return folder / "server.crt", folder / "server.key", fingerprint_of(pem)


def is_self_signed(pem: str) -> bool:
    cert = x509.load_pem_x509_certificate(pem.encode())
    return cert.issuer == cert.subject
