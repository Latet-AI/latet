"""A self-signed certificate you trust for the local reviewer's server: that one only, checked before
any text is sent."""

import datetime
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from latet.config import LlmReview
from latet.engine.reviewer import certificate as certificates
from latet.engine.reviewer.client import ReviewClient, ReviewError


def _self_signed(tmp_path, name, days=(-1, 365)):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=days[0]))
        .not_valid_after(now + datetime.timedelta(days=days[1]))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / f"{name}.crt").write_text(pem)
    (tmp_path / f"{name}.key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return pem


class _Models(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data": [{"id": "fake-model"}]}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(tmp_path, name, days=(-1, 365)):
    """An https server on 127.0.0.1 with a self-signed certificate, as on a machine of yours."""
    pem = _self_signed(tmp_path, name, days)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / f"{name}.crt", tmp_path / f"{name}.key")
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"https://127.0.0.1:{httpd.server_address[1]}/v1", pem


@pytest.fixture
def server(tmp_path):
    httpd, url, pem = _serve(tmp_path, "gpu-box")
    yield url, pem
    httpd.shutdown()


def test_the_fingerprint_is_the_one_openssl_prints(server):
    url, pem = server
    shown, fingerprint = certificates.fetch(url)
    assert shown.strip() == pem.strip() and certificates.fingerprint_of(pem) == fingerprint
    assert certificates.readable(fingerprint).count(":") == 31
    with pytest.raises(ValueError, match="https"):
        certificates.fetch("http://127.0.0.1:8080/v1")


def test_kept_and_found_again_only_by_its_fingerprint(tmp_path, server):
    _, pem = server
    fingerprint = certificates.save(tmp_path, pem)
    assert certificates.load(tmp_path, fingerprint) == pem
    assert certificates.load(tmp_path, "sha256:" + "0" * 64) is None
    assert certificates.load(tmp_path, "../../etc") is None


async def test_the_trusted_certificate_is_accepted_and_no_other(tmp_path, server):
    url, pem = server
    fingerprint = certificates.fingerprint_of(pem)
    trusted = ReviewClient(url, pinned=fingerprint, certificate=pem)
    assert await trusted.models() == ["fake-model"]
    await trusted.aclose()
    other = ReviewClient(url, pinned=fingerprint, certificate=_self_signed(tmp_path, "someone-else"))
    with pytest.raises(ReviewError, match="other than the one you trust"):
        await other.models()
    await other.aclose()
    missing = ReviewClient(url, pinned=fingerprint, certificate=None)  # it went missing: nothing is accepted
    with pytest.raises(ReviewError, match="missing"):
        await missing.models()
    await missing.aclose()
    system = ReviewClient(url)  # without one trusted: the operating system's authorities refuse it
    with pytest.raises(ReviewError, match="not one you trust"):
        await system.models()
    await system.aclose()


def test_settings():
    remote = {"url": "https://gpu-box.lan:8443/v1", "remote": True}
    assert LlmReview(**remote, certificate="sha256:" + "a" * 64).certificate
    with pytest.raises(ValueError, match="https"):
        LlmReview(url="http://gpu-box.lan:8080/v1", remote=True, certificate="sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="certificate"):
        LlmReview(**remote, certificate="AB:CD")
    assert LlmReview(url="http://gpu-box.lan:8080/v1", remote=True).in_clear()
    assert not LlmReview(**remote).in_clear() and not LlmReview().in_clear()


async def test_the_certificate_you_trust_expired(tmp_path):
    httpd, url, pem = _serve(tmp_path, "old-box", days=(-400, -2))
    try:
        client = ReviewClient(url, pinned=certificates.fingerprint_of(pem), certificate=pem)
        with pytest.raises(ReviewError, match=r"expired on .*renew it on the server"):
            await client.models()
        await client.aclose()
    finally:
        httpd.shutdown()


def test_doctor_warns_before_it_expires(tmp_path):
    now = datetime.datetime.now(datetime.UTC)
    fine = certificates.save(tmp_path, _self_signed(tmp_path, "a", days=(-1, 365)))
    soon = certificates.save(tmp_path, _self_signed(tmp_path, "b", days=(-1, 12)))
    gone = certificates.save(tmp_path, _self_signed(tmp_path, "c", days=(-30, -1)))
    assert certificates.check(tmp_path, fine, now) is None
    level, why = certificates.check(tmp_path, soon, now) or ("", "")
    assert level == "warn" and ("in 11 days" in why or "in 12 days" in why)
    assert (certificates.check(tmp_path, gone, now) or ("",))[0] == "fail"
    assert (certificates.check(tmp_path, "sha256:" + "0" * 64, now) or ("",))[0] == "fail"  # missing
