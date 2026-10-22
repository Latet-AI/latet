"""A certificate you trust for the local reviewer's server on another machine of yours.

A server on your network has no certificate a public authority signed, unless you give it one. You
can trust its own instead, self-signed: you compare its SHA-256 fingerprint with the one the server
shows once, and from then on it is the only certificate accepted for the reviewer. It is checked
during the handshake, before any text is sent. No authority is added to the operating system.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import ssl
from pathlib import Path
from urllib.parse import urlparse

FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_FOLDER = "certificates"
# OpenSSL's reasons for refusing a certificate that bear on one you trusted.
_EXPIRED, _NOT_YET_VALID = 10, 9  # X509_V_ERR_CERT_HAS_EXPIRED, X509_V_ERR_CERT_NOT_YET_VALID
_TRUST = "`latet llm trust`, or Show its certificate in the dashboard"
SOON = datetime.timedelta(days=30)  # `latet doctor` warns this long before a certificate expires


def fingerprint_of(pem: str) -> str:
    """``sha256:<hex>`` of a certificate, as `openssl x509 -fingerprint -sha256` computes it."""
    return "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def readable(fingerprint: str) -> str:
    """``AB:CD:...``, the way servers and openssl print a fingerprint."""
    hexa = fingerprint.removeprefix("sha256:").upper()
    return ":".join(hexa[i : i + 2] for i in range(0, len(hexa), 2))


def fetch(url: str, timeout: float = 5.0) -> tuple[str, str]:
    """The certificate the server at ``url`` shows now, not checked (it is shown to you to compare),
    and its fingerprint. The connection takes ``timeout`` at most, and so does the whole handshake,
    however slowly the server sends it (Python's ssl takes a socket's timeout as the handshake's
    total). Raises OSError when it cannot be reached, ValueError for an http address."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("only an https:// server has a certificate")
    pem = ssl.get_server_certificate((parsed.hostname, parsed.port or 443), timeout=timeout)
    return pem, fingerprint_of(pem)


def is_authority(pem: str) -> bool:
    """Whether OpenSSL could take ``pem`` as an authority, so that it is never pinned for a Latet
    server: anything but one X.509 version 3 certificate whose BasicConstraints says CA:FALSE. A
    text of more than one certificate is one (a pinned context trusts every certificate in it), as
    is one without BasicConstraints (OpenSSL may take it as an authority), of version 1, CA:TRUE,
    or one that cannot be read."""
    from cryptography import x509

    try:
        found = x509.load_pem_x509_certificates(pem.encode())
        if len(found) != 1 or found[0].version is not x509.Version.v3:
            return True
        return found[0].extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except Exception:  # unreadable, no BasicConstraints, an extension twice: an authority as far as a pin goes
        return True


def save(data_dir: Path, pem: str) -> str:
    """Keep a certificate you trust; returns its fingerprint. Kept in a folder that is yours alone, made so whatever the
    umask (a computer's pin for its Latet server is read only from such a folder), the file written whole and private:
    nobody else can put another certificate in its place."""
    from latet.config import write_atomic
    from latet.storage.folders import make_private_folder

    fingerprint = fingerprint_of(pem)
    folder = make_private_folder(data_dir / _FOLDER)
    write_atomic(folder / f"{fingerprint.removeprefix('sha256:')}.pem", pem, private=True)
    return fingerprint


def forget(data_dir: Path, fingerprint: str) -> None:
    """The certificate kept with this fingerprint, removed (nothing to do when none is). Raises OSError."""
    if FINGERPRINT.match(fingerprint):
        (data_dir / _FOLDER / f"{fingerprint.removeprefix('sha256:')}.pem").unlink(missing_ok=True)


def load(data_dir: Path, fingerprint: str) -> str | None:
    """The certificate you trusted with this fingerprint, or None when it is not there (or not it)."""
    if not FINGERPRINT.match(fingerprint):
        return None
    try:
        pem = (data_dir / _FOLDER / f"{fingerprint.removeprefix('sha256:')}.pem").read_text("utf-8")
        return pem if fingerprint_of(pem) == fingerprint else None
    except (OSError, ValueError):
        return None


def context(pem: str | None) -> ssl.SSLContext:
    """A TLS context that accepts this certificate only. The name in it is not checked: the
    certificate itself is, and a self-signed one often names no address. ``None`` (the certificate
    you trusted is missing) accepts nothing: the reviewer is not reached until you trust it again."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # the certificate itself is the authority
    if pem is not None:
        ctx.load_verify_locations(cadata=pem)
    return ctx


def expires(pem: str) -> datetime.datetime:
    """When a certificate stops being valid (UTC)."""
    from cryptography import x509

    return x509.load_pem_x509_certificate(pem.encode()).not_valid_after_utc


def _refusal(exc: BaseException) -> ssl.SSLCertVerificationError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def untrusted(exc: BaseException) -> bool:
    """Whether a connection failed because the server's certificate is not trusted."""
    return _refusal(exc) is not None


def why_refused(exc: BaseException, pinned: str | None, pem: str | None) -> str | None:
    """Why the server's certificate was refused, told apart: the one you trust expired (or is not
    valid yet), the server shows another one, or none is trusted. None: it was not refused."""
    error = _refusal(exc)
    if error is None:
        return None
    code = error.verify_code
    if pinned and pem is None:
        return f"the certificate you trusted for it is missing: compare its fingerprint and trust it again ({_TRUST})"
    if pinned and pem is not None:
        if code == _EXPIRED:
            day = expires(pem).date().isoformat()
            return (
                f"the certificate you trust for it expired on {day}: renew it on the server, then trust the new "
                f"one ({_TRUST})"
            )
        if code == _NOT_YET_VALID:
            return (
                "the certificate you trust for it is not valid yet: check the clock of this computer and the server's"
            )
        return (
            "the server shows a certificate other than the one you trust: if you renewed it, compare the new "
            f"fingerprint and trust it ({_TRUST}); if you did not, something may be posing as your server"
        )
    if code == _EXPIRED:
        return "its certificate expired: renew it on the server"
    return (
        "its certificate is not one you trust: if the server is yours and its certificate self-signed, "
        f"compare its fingerprint and trust it ({_TRUST})"
    )


def check(data_dir: Path, fingerprint: str, now: datetime.datetime | None = None) -> tuple[str, str] | None:
    """The certificate you trusted, for `latet doctor`: ``("fail", why)`` when it is missing or
    expired, ``("warn", why)`` when it expires within 30 days, None when all is well."""
    pem = load(data_dir, fingerprint)
    if pem is None:
        return "fail", f"the certificate you trusted for it is missing: trust it again ({_TRUST})"
    now = now or datetime.datetime.now(datetime.UTC)
    until = expires(pem)
    day = until.date().isoformat()
    if until <= now:
        return (
            "fail",
            f"the certificate you trust for it expired on {day}: renew it on the server, then trust the new one",
        )
    if until - now <= SOON:
        days = (until - now).days
        return "warn", (
            f"the certificate you trust for it expires on {day}, in {days} day{'s' if days != 1 else ''}: renew it on "
            "the server, then trust the new one"
        )
    return None
