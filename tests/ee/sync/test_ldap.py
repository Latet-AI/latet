"""LDAP and Active Directory, against ldap3's own in-process directory (MOCK_SYNC), and a directory of the test's own on
127.0.0.1 for TLS and the watchdog: no network."""

import contextlib
import datetime
import ipaddress
import logging
import socket
import ssl
import threading
import traceback
import types
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from latet.ee.settings import LdapSource
from latet.ee.sync import ldap as ldap_module
from latet.ee.sync.ldap import MOST_PAGES, pinned_addresses, read_ldap
from latet.ee.sync.records import SourceError, bounded, terms_of
from latet.ee.sync.sources import readers
from latet.server import egress

from .. import addresses

ldap3 = pytest.importorskip("ldap3")  # the `ldap` extra, in the development environment

BIND = "cn=latet,ou=service,dc=acme,dc=com"
PASSWORD = "bind-" + "secret-91"
SOURCE = LdapSource(name="directory", kind="ldap", url="ldaps://dc1.acme.com", bind=BIND,
                    base="ou=staff,dc=acme,dc=com", mapping={"{givenName} {sn}": "PERSON", "mail": "EMAIL"})  # fmt: skip


def directory(source, password, folder):
    """The account Latet signs in as, and two people whose display names are "Last, First", as Active Directory has
    them."""
    conn = ldap3.Connection(
        ldap3.Server("dc1.acme.com", use_ssl=True), user=source.bind, password=password, client_strategy=ldap3.MOCK_SYNC
    )  # fmt: skip  (use_ssl: an ldaps source signs in only so, B-M7)
    conn.strategy.add_entry(BIND, {"objectClass": ["person"], "sn": "latet", "userPassword": PASSWORD})
    conn.strategy.add_entry("cn=Emily Carter,ou=staff,dc=acme,dc=com", {
        "objectClass": ["person"], "sn": "Carter", "givenName": "Emily", "displayName": "Carter, Emily",
        "mail": "emily.carter@acme.com"})  # fmt: skip
    conn.strategy.add_entry("cn=Daniel Okafor,ou=staff,dc=acme,dc=com", {
        "objectClass": ["person"], "sn": "Okafor", "givenName": "Daniel", "displayName": "Okafor, Daniel",
        "mail": ["daniel.okafor@acme.com", "d.okafor@acme.com"]})  # fmt: skip
    return conn


def test_the_people_of_the_directory_become_terms(tmp_path):
    records = read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=directory)
    assert sorted(terms_of(SOURCE, records).terms) == [
        ("Daniel Okafor", "PERSON"), ("Emily Carter", "PERSON"), ("d.okafor@acme.com", "EMAIL"),
        ("daniel.okafor@acme.com", "EMAIL"), ("emily.carter@acme.com", "EMAIL"),
    ]  # fmt: skip


def test_a_refused_sign_in_fails_without_the_password(tmp_path):
    with pytest.raises(SourceError, match="refused the sign-in") as caught:
        read_ldap(SOURCE, lambda: "wrong-" + "password", folder=tmp_path, connect=directory)
    assert caught.value.code == "refused" and "wrong" not in caught.value.message


def test_a_secret_never_kept_names_the_command_that_keeps_it(tmp_path):
    with pytest.raises(SourceError, match="latet server terms secret directory") as caught:
        read_ldap(SOURCE, lambda: None, folder=tmp_path, connect=directory)
    assert caught.value.code == "no-secret"


def test_without_the_ldap_extra_the_source_says_how_to_have_it(tmp_path):
    def missing(source, password, folder):
        raise ImportError("No module named 'ldap3'")

    with pytest.raises(SourceError, match=r"`ldap` extra") as caught:
        read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=missing)
    assert caught.value.code == "not-installed"


def test_the_directory_is_reached_at_the_address_egress_checked(monkeypatch):
    seen = []

    async def pin(url, *, holder=None, inside=False):
        seen.append((url, holder, inside))
        return "https://10.0.4.10:636/", "dc1.acme.com:636", {}

    monkeypatch.setattr(ldap_module.egress, "pin", pin)
    assert pinned_addresses(SOURCE) == (("10.0.4.10",), 636)
    assert seen == [("https://dc1.acme.com:636/", "term-sync:directory", True)]


def test_no_sign_in_is_sent_where_tls_is_not_up(tmp_path):
    """The design security review's B-M7: an ldaps source without TLS, or STARTTLS that answered False without
    raising, never carries the password (`conn.server.ssl`, `conn.tls_started`, as ldap3 2.9.1 names them)."""
    binds = []

    class _NoTls:
        server = types.SimpleNamespace(ssl=False)
        tls_started = False
        result: ClassVar[dict] = {}

        def bind(self):
            binds.append(True)
            return True

        def unbind(self):
            return None

    starttls = SOURCE.model_copy(update={"url": "ldap://dc1.acme.com", "start_tls": True})
    for source in (SOURCE, starttls):
        with pytest.raises(SourceError, match="TLS") as caught:
            read_ldap(source, lambda: PASSWORD, folder=tmp_path, connect=lambda *_: _NoTls())
        assert caught.value.code == "refused" and PASSWORD not in caught.value.message
    assert binds == []


def test_a_directory_that_pages_without_end_stops_at_its_deadline(tmp_path, monkeypatch):
    """The design security review's B-I1: `_search` checks the run's deadline at each page, and reads at most
    `MOST_PAGES` pages."""
    clock = [0.0]
    real = directory

    def slow(source, password, folder):
        conn = real(source, password, folder)
        search = conn.search

        def paged(*args, **kwargs):
            clock[0] += 400
            return search(*args, **kwargs)

        conn.search = paged
        return conn

    with bounded(300.0, lambda: clock[0]), pytest.raises(SourceError) as caught:
        read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=slow)
    assert caught.value.code == "timeout"


# --- Pages, the run's bytes, the directory's answers ------------------------------------------------------------------


class _Answering:
    """A directory signed in to, whose every search answers ``result`` with ``entries``: no network."""

    server = types.SimpleNamespace(ssl=True)
    tls_started = False

    def __init__(self, result: dict, entries: list[dict]) -> None:
        self.answer, self.entries = result, entries
        self.result: dict = {}
        self.response: list[dict] = []
        self.searches = 0

    def bind(self):
        self.result = {"result": 0, "description": "success"}
        return True

    def search(self, *args, **kwargs):
        self.searches += 1
        self.result, self.response = dict(self.answer), list(self.entries)
        return self.result["result"] == 0

    def unbind(self):
        return None


def test_every_page_of_the_directory_is_read(tmp_path, monkeypatch):
    """One person a page: the paged results' cookie is followed until the directory gives none."""
    monkeypatch.setattr(ldap_module, "PAGE", 1)
    searches = []

    def counted(source, password, folder):
        conn = directory(source, password, folder)
        search = conn.search

        def paged(*args, **kwargs):
            searches.append(kwargs["paged_size"])
            return search(*args, **kwargs)

        conn.search = paged
        return conn

    records = read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=counted)
    assert len(searches) >= 2 and set(searches) == {1}
    assert sorted(value for value, _ in terms_of(SOURCE, records).terms if "@" not in value) == [
        "Daniel Okafor", "Emily Carter"
    ]  # fmt: skip


def test_a_directory_that_keeps_sending_a_cookie_stops_at_the_most_pages(tmp_path):
    """B-I1: a directory that answers each page with a cookie and no entry is read `MOST_PAGES` times at most."""
    paged = {ldap_module._PAGED: {"value": {"size": 0, "cookie": b"more"}}}
    endless = _Answering({"result": 0, "description": "success", "controls": paged}, [])
    with pytest.raises(SourceError, match="pages") as caught:
        read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=lambda *_: endless)
    assert caught.value.code == "too-many" and endless.searches == MOST_PAGES


def test_the_entries_read_are_taken_from_the_run_s_bytes(tmp_path):
    """B-M5: each entry's attributes counted against the run's budget, as it is read."""
    with bounded(1000.0, lambda: 0.0) as limits:
        limits.left_bytes = 10
        with pytest.raises(SourceError) as caught:
            read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=directory)
    assert caught.value.code == "too-large"


def test_a_referral_is_never_followed_and_said_in_fixed_words(tmp_path):
    """Referrals are refused: an answer that is one fails in ldap3's word for it, never its text, the DN or the address
    it points at; a reference among the entries is left out."""
    referral = {"result": 10, "description": "referral", "message": "see cn=Emily Carter,ou=staff,dc=acme,dc=com",
                "referrals": ["ldaps://elsewhere.example.org/ou=staff,dc=acme,dc=com"]}  # fmt: skip
    with pytest.raises(SourceError) as caught:
        read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=lambda *_: _Answering(referral, []))
    assert caught.value.code == "unreadable" and caught.value.message == "the directory answered referral"

    entries = [
        {"type": "searchResRef", "uri": ["ldaps://elsewhere.example.org/ou=staff,dc=acme,dc=com"]},
        {"type": "searchResEntry", "attributes": {"givenName": ["Emily"], "sn": ["Carter"], "mail": []}},
    ]
    found = _Answering({"result": 0, "description": "success"}, entries)
    records = read_ldap(SOURCE, lambda: PASSWORD, folder=tmp_path, connect=lambda *_: found)
    assert terms_of(SOURCE, records).terms == [("Emily Carter", "PERSON")]


def test_a_search_the_directory_refuses_says_its_word_not_the_base(tmp_path):
    elsewhere = SOURCE.model_copy(update={"base": "ou=nowhere,dc=acme,dc=com"})
    with pytest.raises(SourceError) as caught:
        read_ldap(elsewhere, lambda: PASSWORD, folder=tmp_path, connect=directory)
    assert caught.value.code == "unreadable" and caught.value.message == "the directory answered noSuchObject"


def test_a_directory_egress_refuses_is_said_without_its_host(monkeypatch):
    async def pin(url, *, holder=None, inside=False):
        raise egress.EgressRefused("dc1.acme.com does not resolve")

    monkeypatch.setattr(ldap_module.egress, "pin", pin)
    with pytest.raises(SourceError) as caught:
        pinned_addresses(SOURCE)
    assert caught.value.code == "unreachable" and "acme" not in caught.value.message


def test_the_readers_read_a_directory_with_the_term_sync_folder(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ldap_module, "read_ldap", lambda source, secret, *, folder: seen.append((source, folder)) or [])
    assert readers(tmp_path)["ldap"](SOURCE, lambda: PASSWORD) == []
    assert seen == [(SOURCE, tmp_path)]


# --- The connection: the address egress pinned, the name written, the authority's file ----------------------------------


def _pinned(monkeypatch, address: str) -> None:
    async def pin(url, *, holder=None, inside=False):
        return f"https://{address}:636/", "dc1.acme.com:636", {}

    monkeypatch.setattr(ldap_module.egress, "pin", pin)


def _connected(monkeypatch, source: LdapSource, folder: Path):
    """``_connect``'s connection for ``source``, made within a read's cut, ldap3's opening counted and not done (no
    network here)."""
    from ldap3.strategy.sync import SyncStrategy

    opened = []
    monkeypatch.setattr(SyncStrategy, "open", lambda self, *args, **kwargs: opened.append(True))
    with ldap_module._cutting():
        conn = ldap_module._connect(source, PASSWORD, folder)
    assert opened == [True]  # opened by `_connect` itself, LDAPS too: TLS up, by Latet's check, before a sign-in
    return conn


@pytest.mark.parametrize(
    ("url", "address", "host", "port"),
    [
        ("ldaps://dc1.acme.com", "10.0.4.10", "10.0.4.10", 636),
        ("ldaps://dc1.acme.com", "[fd00::4:10]", "fd00::4:10", 636),
        ("ldaps://dc1.acme.com:3269", "10.0.4.10", "10.0.4.10", 3269),  # Active Directory's global catalog
    ],
)
def test_the_connection_goes_to_the_address_egress_pinned_and_follows_no_referral(
    tmp_path, monkeypatch, url, address, host, port
):
    """The target built from the address egress pinned and the port written, never from the url; TLS's name and the
    name its certificate is checked against are the one written; no referral is followed."""
    _pinned(monkeypatch, address)
    conn = _connected(monkeypatch, SOURCE.model_copy(update={"url": url}), tmp_path)
    assert (conn.server.host, conn.server.port, conn.server.ssl) == (host, port, True)
    assert (conn.server.tls.sni, conn.server.tls.valid_names) == ("dc1.acme.com", ["dc1.acme.com"])
    assert conn.server.tls.validate == ssl.CERT_REQUIRED and conn.server.tls.ca_certs_data is None
    assert conn.auto_referrals is False and conn.read_only is True and conn.user == BIND


def test_a_connection_is_made_within_a_read_alone(tmp_path, monkeypatch):
    """The review's Minor 5: never a connection whose socket no read's cut reaches, nor duplicates no one closes."""
    _pinned(monkeypatch, "10.0.4.10")
    with pytest.raises(RuntimeError, match="read_ldap"):
        ldap_module._connect(SOURCE, PASSWORD, tmp_path)


def _authority() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A company's own certificate authority, its key and its certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Acme Directory CA")])
    now = datetime.datetime.now(datetime.UTC)
    usage = x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
    )  # fmt: skip
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _issued(authority, common_name: str, names: list[x509.GeneralName], folder: Path) -> tuple[Path, Path]:
    """A directory's certificate for ``names``, signed by ``authority``: its file and its key's, in ``folder``."""
    issuer_key, issuer = authority
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    usage = x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
        key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
    )  # fmt: skip
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / "directory.crt", folder / "directory.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = serialization.PrivateFormat.PKCS8
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, private, serialization.NoEncryption()))
    return cert_path, key_path


@pytest.fixture
def authority(tmp_path):
    """The company's authority, its certificate in the term-sync folder (the test's own) as `directory-ca.pem`."""
    made = _authority()
    (tmp_path / "directory-ca.pem").write_bytes(made[1].public_bytes(serialization.Encoding.PEM))
    return made


def test_the_certificate_authority_is_read_from_its_file_in_the_term_sync_folder(tmp_path, authority, monkeypatch):
    _pinned(monkeypatch, "10.0.4.10")
    conn = _connected(monkeypatch, SOURCE.model_copy(update={"certificate": "directory-ca.pem"}), tmp_path)
    assert conn.server.tls.ca_certs_data == (tmp_path / "directory-ca.pem").read_text("ascii")


_BROKEN = b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
_UNUSABLE = [
    ("absent.pem", None, "unreachable"),
    ("folder.pem", "folder", "unreachable"),
    ("latin.pem", "caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode("latin-1"), "unreachable"),
    ("empty.pem", b"", "unreadable"),
    ("other.pem", b"no certificate here\n", "unreadable"),
    ("broken.pem", _BROKEN, "unreadable"),
]


@pytest.mark.parametrize(("name", "content", "code"), _UNUSABLE)
def test_a_certificate_file_latet_cannot_use_is_said_in_fixed_words(tmp_path, monkeypatch, name, content, code):
    """No connection is made, and the reason names neither the file nor the folder (B-M4); an empty file never falls
    back to the system's authorities."""
    _pinned(monkeypatch, "10.0.4.10")
    if content == "folder":
        (tmp_path / name).mkdir()
    elif content is not None:
        (tmp_path / name).write_bytes(content)
    with pytest.raises(SourceError) as caught:
        read_ldap(SOURCE.model_copy(update={"certificate": name}), lambda: PASSWORD, folder=tmp_path)
    assert caught.value.code == code, caught.value.message
    assert name not in caught.value.message and str(tmp_path) not in caught.value.message


def test_a_certificate_file_named_out_of_the_folder_is_refused_by_the_reader_too(tmp_path, authority, monkeypatch):
    """The settings take a file name alone; the reader checks again where it resolves (B-M8)."""
    _pinned(monkeypatch, "10.0.4.10")
    folder = tmp_path / "term-sync"
    folder.mkdir()
    outside = SOURCE.model_copy(update={"certificate": "../directory-ca.pem"})  # past the settings' own check
    with pytest.raises(SourceError, match="inside") as caught:
        read_ldap(outside, lambda: PASSWORD, folder=folder)
    assert caught.value.code == "unreachable" and "directory-ca" not in caught.value.message


def test_a_certificate_file_that_leads_out_of_the_folder_is_refused(tmp_path, authority, monkeypatch):
    _pinned(monkeypatch, "10.0.4.10")
    folder = tmp_path / "term-sync"
    folder.mkdir()
    try:
        (folder / "link.pem").symlink_to(tmp_path / "directory-ca.pem")
    except OSError:
        pytest.skip("this system makes no symbolic link here")
    with pytest.raises(SourceError, match="inside") as caught:
        read_ldap(SOURCE.model_copy(update={"certificate": "link.pem"}), lambda: PASSWORD, folder=folder)
    assert caught.value.code == "unreachable" and "link" not in caught.value.message


# --- A directory over a real socket on 127.0.0.1: LDAPS, STARTTLS, the watchdog ------------------------------------------


def _exactly(sock, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("the client closed the connection")
        data += chunk
    return data


def _message(sock) -> bytes:
    """One LDAP message as a directory reads it: its tag, its length (short or long form), its content."""
    head = _exactly(sock, 2)
    size = head[1]
    if size & 0x80:
        extra = _exactly(sock, size & 0x7F)
        head, size = head + extra, int.from_bytes(extra, "big")
    return head + _exactly(sock, size)


def _answer(request: bytes, operation: int, code: int) -> bytes:
    """An LDAP result answering ``request`` (its message id): ``operation``'s tag, ``code``, no DN and no text."""
    at = 2 + (request[1] & 0x7F if request[1] & 0x80 else 0)  # past the message's own tag and length
    assert request[at] == 0x02  # the message id, an INTEGER
    message_id = request[at : at + 2 + request[at + 1]]
    result = bytes([0x0A, 1, code, 0x04, 0, 0x04, 0])
    body = message_id + bytes([operation, len(result)]) + result
    return bytes([0x30, len(body)]) + body


_EXTENDED, _BOUND = 0x78, 0x61  # ExtendedResponse, BindResponse


class _Directory:
    """A directory on 127.0.0.1 that speaks just enough LDAP over a real socket: TLS from the start (LDAPS) or after
    STARTTLS, then a sign-in answered ``bind`` (49, invalidCredentials; 0, and then nothing more); or, with ``bind``
    None, nothing at all; ``refuse_tls``: STARTTLS answered with that result code, and nothing more. ``plain`` holds what it read before TLS, ``inside`` what it read inside it, ``names`` the name
    each handshake asked for (SNI); ``left`` says the client ended the connection before the directory's own wait
    (``WAIT`` seconds, shorter than ldap3's 30) did. ``refused`` says the client refused the handshake, and ``after``
    holds every byte it sent on the connection after that, until it closed it (``AFTER`` seconds at most)."""

    WAIT = 20.0
    AFTER = 10.0

    def __init__(
        self, certificate: tuple[Path, Path], *, starttls: bool = False, bind: int | None = 49, refuse_tls: int = 0
    ) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(*certificate)
        self.names: list[str | None] = []
        self.context.sni_callback = lambda sock, name, context: self.names.append(name)
        self.starttls, self.bind, self.refuse_tls = starttls, bind, refuse_tls
        self.plain: list[bytes] = []
        self.inside: list[bytes] = []
        self.left = False
        self.refused = False
        self.after = b""
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(10)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        conn.settimeout(self.WAIT)
        with conn:
            try:
                if self.bind is None and not self.starttls:
                    self._wait(conn)
                    return
                if self.starttls:
                    request = _message(conn)
                    self.plain.append(request)
                    if self.bind is None:
                        self._wait(conn)
                        return
                    conn.sendall(_answer(request, _EXTENDED, self.refuse_tls))
                    if self.refuse_tls:  # STARTTLS refused with that result code: nothing more
                        self._wait(conn)
                        return
                raw = conn.dup()  # the connection itself, read past a handshake the client refused
                with raw, self.context.wrap_socket(conn, server_side=True, do_handshake_on_connect=False) as tls:
                    try:
                        tls.do_handshake()
                    except TimeoutError:
                        raise
                    except OSError:  # the client refused the handshake: what it sends after, until it closes
                        self.refused = self.left = True
                        self.after = self._rest(raw)
                        return
                    request = _message(tls)
                    self.inside.append(request)
                    tls.sendall(_answer(request, _BOUND, self.bind))
                    self._wait(tls)
            except TimeoutError:  # the directory's own wait passed first
                return
            except OSError:  # the client refused the handshake, or ended the connection mid-message
                self.left = True

    def _rest(self, sock) -> bytes:
        """Every byte the client sends on ``sock`` until it closes the connection (or ``AFTER`` seconds pass)."""
        sock.settimeout(self.AFTER)
        data = b""
        with contextlib.suppress(OSError):  # a reset, or the wait passed: what came so far
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def _wait(self, sock) -> None:
        try:
            while sock.recv(65536):
                pass
        except TimeoutError:  # the directory's own wait passed first
            return
        except OSError:  # a reset: the client ended it all the same
            pass
        self.left = True

    def close(self) -> None:
        self.thread.join(self.WAIT + 10)
        self.listener.close()


def _at(directory: _Directory, monkeypatch, source: LdapSource = SOURCE) -> LdapSource:
    """``source`` at the test's directory, its authority's file `directory-ca.pem`: egress pins the name written to
    127.0.0.1, at the directory's port."""

    async def pin(url, *, holder=None, inside=False):
        assert url == f"https://dc1.acme.com:{directory.port}/" and inside
        return f"https://127.0.0.1:{directory.port}/", f"dc1.acme.com:{directory.port}", {}

    monkeypatch.setattr(ldap_module.egress, "pin", pin)
    scheme = "ldap" if directory.starttls else "ldaps"
    url = f"{scheme}://dc1.acme.com:{directory.port}"
    return source.model_copy(update={"url": url, "start_tls": True, "certificate": "directory-ca.pem"})


def _named(authority, folder: Path) -> tuple[Path, Path]:
    return _issued(authority, "dc1.acme.com", [x509.DNSName("dc1.acme.com")], folder / "server")


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
def test_the_sign_in_crosses_inside_tls_to_the_name_written(tmp_path, authority, monkeypatch, starttls):
    """Over LDAPS, or STARTTLS first: TLS's name is the one written, the certificate checked against the company's
    authority; the password crosses only inside TLS."""
    found = _Directory(_named(authority, tmp_path), starttls=starttls)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "refused", caught.value.message
    assert caught.value.message == "the directory refused the sign-in (invalidCredentials)"
    assert found.names == ["dc1.acme.com"]
    assert BIND.encode() in found.inside[0] and PASSWORD.encode() in found.inside[0]
    if starttls:  # the request to start TLS alone crossed before it
        assert len(found.plain) == 1 and b"1.3.6.1.4.1.1466.20037" in found.plain[0]
        assert PASSWORD.encode() not in found.plain[0] and BIND.encode() not in found.plain[0]


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
def test_a_certificate_for_the_address_and_not_the_name_written_gets_no_sign_in(
    tmp_path, authority, monkeypatch, starttls
):
    """ldap3's own check also accepts the address it connects to (egress's): a directory whose name led elsewhere,
    with a certificate of the company's authority for that address alone, never receives the password."""
    address = [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    found = _Directory(_issued(authority, "127.0.0.1", address, tmp_path / "server"), starttls=starttls)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "refused", caught.value.message
    assert caught.value.message.startswith("the directory's certificate was not trusted")
    assert found.inside == [] and PASSWORD not in caught.value.message


class _KeptSocket:
    """A TLS context whose handshake runs on a duplicate of the connection's socket, so that a refused handshake leaves
    the connection's own socket open and usable (Python's ssl takes it away, which alone kept ldap3's closing unbind()
    off the wire): what goes on it after the refusal is then Latet's doing alone."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context

    def wrap_socket(self, sock, server_hostname=None):
        return self.context.wrap_socket(sock.dup(), server_hostname=server_hostname)


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
@pytest.mark.parametrize("socket_after", ["as-python-leaves-it", "kept-usable"])
@pytest.mark.parametrize("which", ["address", "authority"])
def test_nothing_is_sent_once_latet_refused_the_directory_s_certificate(
    tmp_path, authority, monkeypatch, starttls, socket_after, which
):
    """E6's deferred minor: once Latet's own TLS check refused the directory's certificate (one for the address alone,
    or of another authority), no closing unbind() is asked of ldap3 and no LDAP message goes on that connection: its
    socket is just closed, the directory receiving nothing after the handshake it refused, even where the connection's
    socket is left usable after the refusal."""
    if which == "address":
        address = [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        certificate = _issued(authority, "127.0.0.1", address, tmp_path / "server")
    else:
        certificate = _named(_authority(), tmp_path)
    if socket_after == "kept-usable":
        made = ldap_module._context
        monkeypatch.setattr(ldap_module, "_context", lambda given: _KeptSocket(made(given)))
    unbound, unbind = [], ldap3.Connection.unbind

    def spied(self, *args, **kwargs):  # recorded, then ldap3's own: what it would send reaches the directory
        unbound.append(self.closed)
        return unbind(self, *args, **kwargs)

    monkeypatch.setattr(ldap3.Connection, "unbind", spied)
    found = _Directory(certificate, starttls=starttls)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "refused", caught.value.message  # the certificate not trusted
    assert found.refused and found.inside == []
    assert found.after == b"" and unbound == []


def test_a_certificate_of_another_authority_gets_no_sign_in(tmp_path, authority, monkeypatch):
    found = _Directory(_named(_authority(), tmp_path))  # the right name, another authority than the file's
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "refused" and found.inside == []


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
@pytest.mark.parametrize("which", ["another-authority", "the-address-alone", "no-file"])
def test_a_certificate_latet_does_not_trust_is_said_so_and_points_at_the_authority(
    tmp_path, authority, monkeypatch, starttls, which
):
    """A directory that answers with a certificate Latet's own check refuses (signed by another authority than the
    source's file, for the address and not the name written, or one the server's store does not trust when the source
    names no file) is refused, saying its certificate was not trusted and what to check: never that it did not answer,
    and nothing of the certificate (its subject, its names) nor of the directory."""
    if which == "the-address-alone":
        address: list[x509.GeneralName] = [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        certificate = _issued(authority, "127.0.0.1", address, tmp_path / "server")
    elif which == "another-authority":
        certificate = _named(_authority(), tmp_path)
    else:
        certificate = _named(authority, tmp_path)  # the company's authority, which the system's store does not hold
    found = _Directory(certificate, starttls=starttls)
    source = _at(found, monkeypatch)
    if which == "no-file":
        source = source.model_copy(update={"certificate": None})
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(source, lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "refused", caught.value.message
    if which == "no-file":
        assert caught.value.message == (
            "the directory's certificate was not trusted: check that the server's certificate store holds the "
            "authority that signed it, and that the certificate names the host in `url`"
        )
    else:
        assert caught.value.message == (
            "the directory's certificate was not trusted: check the certificate authority's file (`certificate`), "
            "and that the certificate names the host in `url`"
        )
    assert found.inside == []  # no sign-in: nothing crossed inside TLS
    for said in ("dc1.acme.com", "127.0.0.1", "Acme", "directory-ca.pem", PASSWORD):
        assert said not in caught.value.message


@pytest.mark.parametrize(
    ("starttls", "bind", "seconds", "said"),
    [(False, None, 0.5, "1 second"), (True, None, 3.0, "3 seconds"), (False, 0, 3.0, "3 seconds")],
    ids=["handshake", "starttls", "search"],
)
def test_a_directory_that_stops_answering_is_cut_at_the_run_s_deadline(
    tmp_path, authority, monkeypatch, starttls, bind, seconds, said
):
    """Task 7's watchdog (B-I1): a directory that says nothing during the TLS handshake, the STARTTLS request or a
    search waits on no socket's timeout (ldap3's 30 seconds): its connection is shut down at the deadline, and the read
    fails `timeout` in fixed words."""
    found = _Directory(_named(authority, tmp_path), starttls=starttls, bind=bind)
    # A clock that stands still: only the watchdog can end the read. A later stage has 3 real seconds to be reached, so
    # that a loaded machine does not cut the read before it (as Task 8's fix).
    try:
        with bounded(100.0 + seconds, lambda: 100.0), pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "timeout", caught.value.message
    assert caught.value.message == f"the source took longer than the {said} its run had: narrow it"
    assert found.left  # cut by the client at its deadline, not ended by the directory's own wait


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
def test_a_connection_whose_socket_cannot_be_kept_for_the_cut_is_closed_at_once(
    tmp_path, authority, monkeypatch, starttls
):
    """As a url source's: a connection the watchdog could not cut (its socket not duplicated: no descriptor left) is
    never left open, and no sign-in is sent on it; on both paths it fails `unreachable`, said by its type (the review's
    Minor 3)."""
    found = _Directory(_named(authority, tmp_path), starttls=starttls, bind=None)
    given = []

    def keep(self, sock):
        given.append(sock)
        raise OSError("too many open files")

    monkeypatch.setattr(ldap_module._Cut, "keep", keep)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert len(given) == 1 and given[0].fileno() == -1
    assert found.inside == [] and not any(BIND.encode() in message for message in found.plain)
    assert caught.value.code == "unreachable"
    assert caught.value.message == f"the directory did not answer ({'OSError' if starttls else 'LDAPSocketOpenError'})"


def test_without_an_authority_s_file_the_system_s_authorities_check_the_name():
    import truststore

    context = ldap_module._context(None)
    assert isinstance(context, truststore.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
def test_tls_that_latet_did_not_check_gets_no_sign_in(tmp_path, authority, monkeypatch, starttls):
    """The review's Minor 4: should an ldap3 stop handing its handshake to Latet's (its own check also accepts the
    pinned address), TLS comes up all the same, by ldap3's check, and no sign-in is sent: Latet signs in only after a
    handshake of its own."""
    from ldap3.core.tls import Tls
    from ldap3.strategy.base import BaseStrategy

    opened = BaseStrategy._open_socket

    def ldap3s_own(self, address, use_ssl=False, unix_socket=False):  # ldap3's handshake, not the Tls given's
        opened(self, address, False, unix_socket)
        if use_ssl:
            Tls.wrap_socket(self.connection.server.tls, self.connection, do_handshake=True)

    def started(self, connection):
        Tls.wrap_socket(self, connection, do_handshake=True)
        connection.starting_tls, connection.tls_started = False, True
        return True

    monkeypatch.setattr(BaseStrategy, "_open_socket", ldap3s_own)
    monkeypatch.setattr(Tls, "_start_tls", started)
    found = _Directory(_named(authority, tmp_path), starttls=starttls)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert found.names == ["dc1.acme.com"]  # TLS was up, by ldap3's own check
    sent = found.plain + found.inside  # at most the unbind, as the connection closes
    assert not any(BIND.encode() in message or PASSWORD.encode() in message for message in sent)
    assert caught.value.code == "refused"
    assert caught.value.message == "Latet did not check the directory's certificate: it does not sign in without it"


@pytest.mark.parametrize(
    ("field", "value", "kind"),
    [
        ("filter", "(foo)", "LDAPInvalidFilterError"),
        ("filter", "(cn=a)(cn=b)", "LDAPInvalidFilterError"),
        ("base", "not a dn", "LDAPInvalidDnError"),
    ],
)
def test_a_filter_or_a_dn_ldap_refuses_before_sending_it_is_unreadable(tmp_path, field, value, kind):
    """The review's Minor 2: what ldap3 refuses before sending (a filter the settings take and LDAP does not, a base
    that is no DN) is the source's settings, not the network: `unreadable`, saying ldap3's type, never the value."""
    source = LdapSource(**{**SOURCE.model_dump(), field: value})  # the settings take it
    with pytest.raises(SourceError) as caught:
        read_ldap(source, lambda: PASSWORD, folder=tmp_path, connect=directory)
    assert caught.value.code == "unreadable"
    assert caught.value.message == f"the source's filter, base or bind is not one LDAP takes ({kind})"


def test_no_reason_or_log_holds_the_password(tmp_path, authority, monkeypatch, caplog):
    """The review's Minor 5: ldap3's Connection holds the password (its repr shows it). Latet never formats one, and no
    failure's reason, text, traceback or log line holds the password."""
    caplog.set_level(logging.DEBUG)
    failures = []
    refused = LdapSource(**{**SOURCE.model_dump(), "filter": "(foo)"})
    with pytest.raises(SourceError) as caught:
        read_ldap(refused, lambda: PASSWORD, folder=tmp_path, connect=directory)
    failures.append(caught.value)
    address = [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for certificate, bind, until in [
        (_named(authority, tmp_path / "named"), 49, None),  # the sign-in refused
        (_issued(authority, "127.0.0.1", address, tmp_path / "address"), 49, None),  # the certificate refused
        (_named(authority, tmp_path / "silent"), None, 100.5),  # cut at the deadline
    ]:
        found = _Directory(certificate, bind=bind)
        try:
            with contextlib.ExitStack() as stack:
                if until is not None:
                    stack.enter_context(bounded(until, lambda: 100.0))
                with pytest.raises(SourceError) as caught:
                    read_ldap(_at(found, monkeypatch), lambda: PASSWORD, folder=tmp_path)
        finally:
            found.close()
        failures.append(caught.value)
    assert [failure.code for failure in failures] == ["unreadable", "refused", "refused", "timeout"]
    for failure in failures:
        told = [failure.message, str(failure), repr(failure), "".join(traceback.format_exception(failure))]
        assert not any(PASSWORD in text for text in told), failure.code
    assert PASSWORD not in caplog.text


# --- A directory at a name with several addresses: the next one checked, when the connection to the one before failed --


def _opened(monkeypatch, refused=(addresses.PAST,)) -> list[str]:
    """Every connection ldap3 opens, by its address; one to an address of ``refused`` refused at once, as ldap3 says a
    connection refused, without the network."""
    from ldap3.core.exceptions import LDAPSocketOpenError
    from ldap3.strategy.base import BaseStrategy

    asked: list[str] = []
    refusing = frozenset(refused)
    opened = BaseStrategy._open_socket

    def recorded(self, address, use_ssl=False, unix_socket=False):
        asked.append(address[4][0])
        if address[4][0] in refusing:
            raise LDAPSocketOpenError("socket connection error while opening: the connection was refused")
        return opened(self, address, use_ssl, unix_socket)

    monkeypatch.setattr(BaseStrategy, "_open_socket", recorded)
    return asked


def _localhost(authority, folder: Path) -> tuple[Path, Path]:
    return _issued(authority, "localhost", [x509.DNSName("localhost")], folder / "server")


def _local(directory: _Directory, source: LdapSource = SOURCE) -> LdapSource:
    """``source`` at ``localhost``, at the test's directory's port, its authority's file `directory-ca.pem`; egress
    resolves the name as the test says."""
    scheme = "ldap" if directory.starttls else "ldaps"
    url = f"{scheme}://localhost:{directory.port}"
    return source.model_copy(update={"url": url, "start_tls": True, "certificate": "directory-ca.pem"})


@pytest.mark.parametrize("starttls", [False, True], ids=["ldaps", "starttls"])
def test_a_directory_on_this_computer_is_reached_at_the_next_address_its_lookup_checked(
    tmp_path, authority, monkeypatch, starttls
):
    """Windows resolves ``localhost`` to ``::1`` first, and a directory in Docker listens on 127.0.0.1 alone (the
    release checklist's item 4): it is reached at 127.0.0.1 once ``::1`` refused the connection, TLS's name and the
    name its certificate is checked against ``localhost``, and the sign-in crosses inside TLS; nothing is tried after
    it."""
    found = _Directory(_localhost(authority, tmp_path), starttls=starttls)
    asked = addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    connects = _opened(monkeypatch)
    try:
        with addresses.refused_on_v6(found.port), pytest.raises(SourceError) as caught:
            read_ldap(_local(found), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.message == "the directory refused the sign-in (invalidCredentials)"
    assert found.names == ["localhost"] and PASSWORD.encode() in found.inside[0]
    assert connects == ["::1", "127.0.0.1"] and asked == ["localhost"]


def test_a_directory_is_connected_at_no_address_its_lookup_did_not_check(tmp_path, authority, monkeypatch):
    """The addresses tried are those egress's lookup checked, in its order, and nothing else; a name with an address
    the server never reaches among its answers is not read, nothing connected."""
    found = _Directory(_localhost(authority, tmp_path))
    connects = _opened(monkeypatch, refused=("::1", addresses.PAST))
    try:
        addresses.resolving(monkeypatch, *addresses.LOCALHOST)
        with pytest.raises(SourceError) as caught:
            read_ldap(_local(found), lambda: PASSWORD, folder=tmp_path)
        assert caught.value.message == "the directory refused the sign-in (invalidCredentials)"
        assert connects == ["::1", "127.0.0.1"]
        connects.clear()
        addresses.resolving(monkeypatch, "::1", "127.0.0.1", addresses.METADATA)
        with pytest.raises(SourceError) as caught:
            read_ldap(_local(found), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == "unreachable" and connects == []
    assert caught.value.message == "the server does not connect to this source's directory"


@pytest.mark.parametrize(
    ("starttls", "refuse_tls", "code"),
    [(False, 0, "refused"), (True, 0, "refused"), (True, 2, "unreachable")],
    ids=["ldaps", "starttls", "starttls-refused"],
)
def test_a_directory_is_asked_at_no_other_address_once_tls_or_starttls_began(
    tmp_path, authority, monkeypatch, starttls, refuse_tls, code
):
    """Reached at 127.0.0.1, the directory's TLS fails (a certificate for another name, over LDAPS or after STARTTLS),
    or it refuses STARTTLS: no other address is tried, and no sign-in is sent anywhere."""
    certificate = _localhost(authority, tmp_path) if refuse_tls else _named(authority, tmp_path)
    found = _Directory(certificate, starttls=starttls, refuse_tls=refuse_tls)
    connects = _opened(monkeypatch, refused=("::1", addresses.PAST))
    addresses.resolving(monkeypatch, *addresses.LOCALHOST)
    try:
        with pytest.raises(SourceError) as caught:
            read_ldap(_local(found), lambda: PASSWORD, folder=tmp_path)
    finally:
        found.close()
    assert caught.value.code == code, caught.value.message  # its certificate not trusted, or STARTTLS refused
    assert connects == ["::1", "127.0.0.1"] and found.inside == []
    assert not any(PASSWORD.encode() in message for message in found.plain)
