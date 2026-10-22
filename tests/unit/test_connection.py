"""A computer's side of Latet on a server: its connection and token, the address its agents get, which server a
command asks, and its calls over HTTPS with the certificate it pinned. None of it needs the `server` extra."""

import contextlib
import datetime
import errno
import ipaddress
import itertools
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import keyring.core
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from keyring.backends import null
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response, StreamingResponse

from latet.config import data_dir
from latet.engine.reviewer import certificate as certificates
from latet.server import client as client_module
from latet.server import connection as connection_module
from latet.server.certificate import is_authority, is_self_signed, make_certificate
from latet.server.client import ServerClient, ServerError, pin_certificate, printable, tls_context
from latet.server.connection import (
    KEYRING,
    Connection,
    NotConnected,
    agent_base,
    connection_path,
    fingerprint_form,
    forget_connection,
    keep_connection,
    kept_tokens,
    load_connection,
    load_server_token,
    read_key,
    remove_server_token,
    save_connection,
    save_server_token,
    server_certificate_file,
    server_target,
    server_url,
)
from latet.server.tokens import route_key
from latet.storage import folders
from latet.storage.folders import ADMINISTRATORS, CREATOR_OWNER, OWNER_RIGHTS, SYSTEM, Grant, private_folder_problem

from .conftest import MemoryKeyring
from .fake_latet_server import ADMIN_TOKEN, FakeLatetServer, free_port

URL = "https://latet.acme.lan"
PINNED = "sha256:" + "ab" * 32
TOKEN = "latet_pat_" + "x" * 43
EMILY = Connection(url=URL, user="u_emilycarte", user_name="Emily Carter")
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _certificate(folder, name, *, ca=None, issuer=None, host=None):
    """A certificate for ``host`` (``name`` by default) written as ``<name>.crt`` and ``<name>.key``: self-signed, or
    signed by ``issuer`` (its certificate and key); with BasicConstraints CA:``ca``, or none when ``ca`` is None."""
    host = host or name
    key = ec.generate_private_key(ec.SECP256R1())
    try:
        alt: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        alt = x509.DNSName(host)
    now = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} {host}")])
    signer, signer_name = (key, subject) if issuer is None else (issuer[1], issuer[0].subject)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    )
    if ca is not None:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    cert = builder.sign(signer, hashes.SHA256())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / f"{name}.key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert, key, folder / f"{name}.crt", folder / f"{name}.key"


def _handshake(client, crt, key, host):
    """A TLS handshake in memory between ``client`` and a server showing ``crt``: raises what the client refuses."""
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(crt, key)
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    sides = [
        (client.wrap_bio(c_in, c_out, server_hostname=host), c_out, s_in),
        (server.wrap_bio(s_in, s_out, server_side=True), s_out, c_in),
    ]
    done = [False, False]
    for _ in range(20):
        for index, (side, out, into) in enumerate(sides):
            if not done[index]:
                try:
                    side.do_handshake()
                    done[index] = True
                except ssl.SSLWantReadError:
                    pass
            into.write(out.read())
        if all(done):
            return
    raise AssertionError("the handshake did not end")


def _icacls(folder, *args):
    subprocess.run(["icacls", str(folder), *args], check=True, capture_output=True, timeout=30)


def _enrolled(server):
    """The fake server's certificate pinned, and a token it takes (its computer key in ``server.keys``)."""
    pinned = pin_certificate(server.url, server.fingerprint)
    with ServerClient(server.url, None, pinned) as client:
        return pinned, client.enrol(server.code, "laptop")["token"]


class _Trickle:
    """A server on 127.0.0.1 that answers ``start`` and then ``filler`` a byte at a time, a byte every 50 ms, so that no
    read of a client waits long: a TLS record's header that announces 16 KiB, or an HTTP answer's first line and a
    header that never ends. It sends for ``lasting`` seconds at most, then closes, so that a client with no deadline
    of its own ends too."""

    def __init__(self, start, filler=b"\x00", lasting=8.0):
        self.start, self.filler, self.end = start, filler, time.monotonic() + lasting
        self.stop = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set() and time.monotonic() < self.end:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                continue
            threading.Thread(target=self._trickle, args=(connection,), daemon=True).start()

    def _trickle(self, connection):
        with connection:
            connection.settimeout(1.0)
            with contextlib.suppress(OSError):
                connection.recv(65536)  # the client's hello, or its request
            for byte in itertools.chain(self.start, itertools.repeat(self.filler[0])):
                if self.stop.is_set() or time.monotonic() > self.end:
                    return
                try:
                    connection.sendall(bytes([byte]))
                except OSError:
                    return
                time.sleep(0.05)

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=5)


@pytest.fixture
def trickle(request):
    """A ``_Trickle`` server answering what the test's parameter says (``indirect``)."""
    server = _Trickle(*request.param)
    yield server
    server.close()


# --- an address, a fingerprint, a certificate ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://latet.acme.lan", URL),
        ("https://Latet.Acme.LAN/", URL),
        ("https://latet.acme.lan:443", URL),
        ("HTTPS://latet.acme.lan:0443", URL),
        ("https://latet.acme.lan:8443", "https://latet.acme.lan:8443"),
        ("https://10.0.0.5", "https://10.0.0.5"),
        ("http://127.0.0.1:6655", "http://127.0.0.1:6655"),  # on this computer: the tests
        ("http://[::1]:6655", "http://[::1]:6655"),
        ("https://[0:0:0:0:0:0:0:1]", "https://[::1]"),
        ("https://stra\N{LATIN SMALL LETTER SHARP S}e.acme.lan", "https://xn--strae-oqa.acme.lan"),  # as httpx sends it
        (
            "https://lat\N{CYRILLIC SMALL LETTER IE}t.acme.lan",
            "https://xn--latt-x4d.acme.lan",
        ),  # a look-alike, told apart
    ],
)
def test_an_address_is_kept_in_the_one_form_every_client_reads_alike(given, kept):
    assert server_url(given) == kept


@pytest.mark.parametrize(
    "given",
    [
        "http://latet.acme.lan",
        "https://latet.acme.lan/latet",
        "https://u:p@latet.acme.lan",
        "latet.acme.lan",
        "ftp://latet.acme.lan",
        "https://@latet.acme.lan",
        "https://latet.acme.lan?",
        "https://latet.acme.lan#",
        "https://latet.acme.lan//",
        "https://lat" + chr(0) + "et.acme.lan",
        "https://latet.acme.lan" + chr(0x85),
        "https://lat et.acme.lan",
        "https://latet" + chr(0x5C) + ".acme.lan",
        "https://latet.acme.lan%2f.evil.com",
        "https://*.acme.lan",
        "https://lat_et.acme.lan",
        "https://latet.acme.lan;x",
        "https://latet.acme.lan,evil.com",
        "https://a..b",
        "https://latet.acme.lan.",
        "https://2130706433",
        "https://0x7f.1",
        "https://127.1",
        "https://[fe80::1%25eth0]",
        "https://[::1",
        "https://\N{FULLWIDTH LATIN SMALL LETTER L}atet.acme.lan",
        "https://xn--zz.acme.lan",
        "https://latet.acme.lan:443:443",
        "https://latet.acme.lan:",
        "https://latet.acme.lan:0",
        "https://latet.acme.lan:65536",
        "https://latet.acme.lan:+443",
        "http://localhost:6655",
        "http://[::ffff:7f00:1]:6655",
        "https://",
    ],
)
def test_an_address_that_is_not_https_alone_is_refused(given):
    with pytest.raises(ValueError, match="https"):
        server_url(given)


def test_a_refused_address_is_not_repeated():
    with pytest.raises(ValueError, match="https") as refused:
        server_url("https://ci:hunter2@latet.acme.lan")
    assert "hunter2" not in str(refused.value)


def test_a_fingerprint_is_taken_as_latet_or_openssl_writes_it():
    assert fingerprint_form(PINNED) == fingerprint_form(":".join(["AB"] * 32)) == PINNED
    with pytest.raises(ValueError, match="64 hexadecimal"):
        fingerprint_form("sha256:1234")


@pytest.mark.parametrize(
    ("url", "name", "permitted"),
    [
        (
            URL,
            x509.DNSName("latet.acme.lan"),
            [
                x509.DNSName("latet.acme.lan"),
                x509.IPAddress(ipaddress.ip_network("0.0.0.0/32")),
                x509.IPAddress(ipaddress.ip_network("::/128")),
            ],
        ),
        (
            "https://127.0.0.1:6655",
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            [x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")), x509.DNSName("invalid")],
        ),
        (
            "https://[::1]:6655",
            x509.IPAddress(ipaddress.ip_address("::1")),
            [x509.IPAddress(ipaddress.ip_network("::1/128")), x509.DNSName("invalid")],
        ),
        (
            "https://stra\N{LATIN SMALL LETTER SHARP S}e.acme.lan",
            x509.DNSName("xn--strae-oqa.acme.lan"),
            [
                x509.DNSName("xn--strae-oqa.acme.lan"),
                x509.IPAddress(ipaddress.ip_network("0.0.0.0/32")),
                x509.IPAddress(ipaddress.ip_network("::/128")),
            ],
        ),
    ],
)
def test_a_self_signed_certificate_names_the_servers_host_alone_and_is_no_authority(tmp_path, url, name, permitted):
    crt, key, fingerprint = make_certificate(url, tmp_path, days=30)
    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value == x509.SubjectAlternativeName(
        [name]
    )
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)  # what holds for rustls (Codex)
    assert constraints.critical and constraints.value.excluded_subtrees is None
    assert list(constraints.value.permitted_subtrees or []) == permitted
    assert cert.not_valid_after_utc - cert.not_valid_before_utc <= datetime.timedelta(days=30, minutes=5)
    assert is_self_signed(crt.read_text()) and not is_authority(crt.read_text())
    assert fingerprint == certificates.fingerprint_of(crt.read_text())
    if os.name != "nt":
        assert key.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("ca", [True, None])
def test_a_certificate_that_is_or_may_be_an_authority_is_one(tmp_path, ca):
    _, _, crt, _ = _certificate(tmp_path, "openssl", ca=ca)  # CA:TRUE, as `openssl req -x509` makes it; or no CA:FALSE
    assert is_authority(crt.read_text())
    assert is_authority("not a certificate")


def test_a_text_of_more_than_one_certificate_is_an_authority(tmp_path):
    first = make_certificate(URL, tmp_path / "first")[0].read_text()
    second = make_certificate(URL, tmp_path / "second")[0].read_text()
    assert not is_authority(first) and not is_authority(second)
    assert is_authority(first + second)  # a pinned context would trust both, the second judged by nothing


@WINDOWS
def test_on_windows_the_servers_key_is_written_only_in_a_folder_that_is_the_persons_alone(tmp_path):
    folder = tmp_path / "tls"
    folder.mkdir()
    _icacls(folder, "/grant", "*S-1-5-32-545:(OI)(CI)R")  # Users may read, as a folder outside the profile lets them
    with pytest.raises(PermissionError, match="icacls"):
        make_certificate(URL, folder)
    assert list(folder.iterdir()) == []


# --- the token, kept with the connection it was issued for --------------------------------------------------------


class Failing(MemoryKeyring):
    """A keyring that fails while ``failing`` (a locked keychain, a session without D-Bus)."""

    def __init__(self):
        super().__init__()
        self.failing = True

    def get_password(self, service, username):
        if self.failing:
            raise RuntimeError("locked")
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        if self.failing:
            raise RuntimeError("locked")
        super().set_password(service, username, password)

    def delete_password(self, service, username):
        if self.failing:
            raise RuntimeError("locked")
        super().delete_password(service, username)


def test_the_token_is_kept_in_a_private_file_without_a_keyring(computer):
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file" and load_server_token(data_dir(), EMILY) == TOKEN
    path = data_dir() / "server.token"
    assert json.loads(path.read_text("utf-8")) == {
        "url": URL,
        "user": "u_emilycarte",
        "fingerprint": None,
        "token": TOKEN,
    }
    assert b"\r" not in path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    assert remove_server_token(data_dir()) == [] and load_server_token(data_dir(), EMILY) is None
    assert not path.exists()


def test_the_token_is_kept_in_the_keyring_when_it_gives_it_back(keyring_on):
    (data_dir() / "server.token").parent.mkdir(parents=True, exist_ok=True)
    (data_dir() / "server.token").write_text("an older copy", "utf-8")
    assert save_server_token(data_dir(), EMILY, TOKEN) == "keyring" and load_server_token(data_dir(), EMILY) == TOKEN
    assert json.loads(keyring_on.kept[("latet-server", "server-token")])["url"] == URL  # a service of its own
    assert not (data_dir() / "server.token").exists()  # it would be read if the keyring ever failed
    assert remove_server_token(data_dir()) == [] and keyring_on.kept == {}


def test_the_keyrings_copy_is_answered_without_reading_the_file(keyring_on, monkeypatch):
    save_server_token(data_dir(), EMILY, TOKEN)
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / "server.token").write_text("a copy left in a folder others may change", "utf-8")
    with monkeypatch.context() as matching:
        matching.setattr(
            connection_module, "_read_token_file", lambda data: pytest.fail("a matching keyring copy read the file")
        )
        assert load_server_token(data_dir(), EMILY) == TOKEN
    monkeypatch.setattr(connection_module, "private_folder_problem", lambda folder: f"{folder} is open to others")
    with pytest.raises(NotConnected, match="open to others"):  # every copy: the file is not read from such a folder
        kept_tokens(data_dir())


@pytest.mark.parametrize("backend", [Failing, null.Keyring], ids=["a keyring that fails", "one that keeps nothing"])
def test_a_keyring_that_does_not_give_the_token_back_leads_to_the_file(keyring_on, monkeypatch, backend):
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend())
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file" and load_server_token(data_dir(), EMILY) == TOKEN


def test_a_keyring_that_takes_no_new_token_keeps_no_older_one(keyring_on, monkeypatch):
    older = "latet_pat_" + "o" * 43
    save_server_token(data_dir(), EMILY, older)  # the same connection, an older token

    def refused(service, username, password):
        raise RuntimeError("the keychain takes nothing now")

    monkeypatch.setattr(keyring_on, "set_password", refused)
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file"
    assert keyring_on.kept == {} and load_server_token(data_dir(), EMILY) == TOKEN  # never the older one, first


def test_a_keyring_that_keeps_part_of_the_token_keeps_none_of_it(keyring_on, monkeypatch):
    def halved(service, username, password):
        keyring_on.kept[(service, username)] = password[: len(password) // 2]  # a size limit, say

    monkeypatch.setattr(keyring_on, "set_password", halved)
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file"  # what came back was not what was given
    assert keyring_on.kept == {} and load_server_token(data_dir(), EMILY) == TOKEN


def test_removing_the_token_clears_the_keyring_too_while_it_is_off(keyring_on, monkeypatch):
    save_server_token(data_dir(), EMILY, TOKEN)  # saved while it was on
    monkeypatch.setenv("LATET_KEYRING", "off")
    assert load_server_token(data_dir(), EMILY) is None  # not asked while off
    assert remove_server_token(data_dir()) == [] and keyring_on.kept == {}
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / f".server.token.{os.getpid()}.tmp").write_text("a write cut short", "utf-8")
    assert remove_server_token(data_dir()) == [] and not list(data_dir().glob(".server.token.*"))


@pytest.mark.parametrize(
    "other",
    [
        Connection(url="https://latet.other.lan", user="u_emilycarte", user_name="Emily Carter"),
        Connection(url=URL, user="u_danielokaf", user_name="Daniel Okafor"),
        Connection(url=URL, user="u_emilycarte", user_name="Emily Carter", fingerprint=PINNED),
    ],
    ids=["another server", "another identity", "another certificate"],
)
def test_a_token_is_answered_for_its_own_connection_alone(computer, other):
    save_server_token(data_dir(), EMILY, TOKEN)
    assert load_server_token(data_dir(), other) is None and load_server_token(data_dir(), EMILY) == TOKEN


def test_two_connection_files_never_get_each_others_token(computer, monkeypatch):
    first, second = EMILY, Connection(url="https://latet.other.lan", user="u_emilycarte", user_name="Emily Carter")
    monkeypatch.setenv("LATET_CONNECTION", str(computer / "first.yaml"))
    keep_connection(first, "latet_pat_" + "a" * 43)
    monkeypatch.setenv("LATET_CONNECTION", str(computer / "second.yaml"))
    keep_connection(second, "latet_pat_" + "b" * 43)  # its token takes the one place a token is kept
    assert agent_base() == f"{second.url}/u/{route_key('latet_pat_' + 'b' * 43)}"
    monkeypatch.setenv("LATET_CONNECTION", str(computer / "first.yaml"))
    assert load_server_token(data_dir(), first) is None
    with pytest.raises(NotConnected, match="token is missing"):  # never the other server's token, sent to this one
        agent_base()


def test_a_stale_keyring_copy_is_not_answered_for_a_newer_connection(keyring_on, monkeypatch):
    backend = Failing()
    backend.failing = False
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
    older = Connection(url="https://latet.old.lan", user="u_emilycarte", user_name="Emily Carter")
    assert save_server_token(data_dir(), older, "latet_pat_" + "o" * 43) == "keyring"
    backend.failing = True  # while the computer disconnects, then connects again
    assert remove_server_token(data_dir()) == [KEYRING]  # said: a copy may remain there
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file"
    backend.failing = False  # the keyring answers again, the older record still in it
    assert [kept.url for kept in kept_tokens(data_dir())] == ["https://latet.old.lan", URL]
    assert load_server_token(data_dir(), EMILY) == TOKEN


def test_nothing_is_kept_of_a_connection_whose_file_cannot_be_written(keyring_on, monkeypatch):
    write = connection_module.write_atomic

    def refused(path, *args, **kwargs):
        if path == connection_path():
            raise OSError(errno.EACCES, "Permission denied")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(connection_module, "write_atomic", refused)
    with pytest.raises(OSError, match="Permission denied"):
        keep_connection(EMILY, TOKEN)
    assert keyring_on.kept == {} and kept_tokens(data_dir()) == [] and load_connection() is None


def test_a_text_that_is_not_a_personal_token_is_not_kept(computer):
    for given in ("latet_key_" + "k" * 43, "latet_pat_short", TOKEN + "\n"):
        with pytest.raises(ValueError, match="personal token") as refused:
            save_server_token(data_dir(), EMILY, given)
        assert given not in str(refused.value)
    assert kept_tokens(data_dir()) == []


@pytest.mark.parametrize(
    "record",
    [
        {"url": URL, "user": "u_emilycarte", "fingerprint": None, "token": "latet_key_" + "k" * 43},
        {"url": "http://latet.acme.lan", "user": "u_emilycarte", "fingerprint": None, "token": TOKEN},
        {"url": URL, "user": "u_emilycarte", "fingerprint": "sha256:../x", "token": TOKEN},
        TOKEN,
    ],
    ids=["an API key", "an address in clear", "a fingerprint that is a path", "a bare token"],
)
def test_a_copy_that_is_not_a_record_of_a_personal_token_is_answered_for_no_connection(computer, record):
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / "server.token").write_text(json.dumps(record), "utf-8")
    (data_dir() / "server.token").chmod(0o600)  # a file of the person's alone: what it holds is what is read
    assert kept_tokens(data_dir()) == [] and load_server_token(data_dir(), EMILY) is None


def test_a_kept_copy_shows_no_token(computer):
    save_server_token(data_dir(), EMILY, TOKEN)
    [kept] = kept_tokens(data_dir())
    assert (kept.url, kept.place) == (URL, str(data_dir() / "server.token")) and TOKEN not in repr(kept)


@POSIX
def test_the_token_file_is_used_only_in_a_folder_and_as_a_file_that_are_the_persons_alone(computer):
    data = data_dir()
    data.mkdir(parents=True)
    data.chmod(0o777)
    with pytest.raises(NotConnected, match="chmod 700"):
        save_server_token(data, EMILY, TOKEN)
    assert not (data / "server.token").exists()
    data.chmod(0o700)
    save_server_token(data, EMILY, TOKEN)
    data.chmod(0o777)
    with pytest.raises(NotConnected, match="chmod 700"):
        load_server_token(data, EMILY)
    data.chmod(0o700)
    (data / "server.token").chmod(0o644)
    with pytest.raises(NotConnected, match="chmod 600"):
        load_server_token(data, EMILY)
    (data / "server.token").chmod(0o600)
    assert load_server_token(data, EMILY) == TOKEN
    elsewhere = computer / "elsewhere.token"
    os.replace(data / "server.token", elsewhere)
    (data / "server.token").symlink_to(elsewhere)  # a link is not followed
    with pytest.raises(NotConnected):
        load_server_token(data, EMILY)


@POSIX
def test_a_data_folder_made_for_the_token_is_the_persons_alone_whatever_the_umask(computer):
    before = os.umask(0o002)  # user private groups: what is made is group-writable
    try:
        assert save_server_token(data_dir(), EMILY, TOKEN) == "file"
    finally:
        os.umask(before)
    assert data_dir().stat().st_mode & 0o777 == 0o700 and load_server_token(data_dir(), EMILY) == TOKEN


def test_a_data_folder_made_for_the_token_passes_its_own_check(computer):
    assert not data_dir().exists()
    assert save_server_token(data_dir(), EMILY, TOKEN) == "file"
    assert private_folder_problem(data_dir()) is None


@WINDOWS
def test_on_windows_the_token_file_is_used_only_in_a_folder_open_to_nobody_else(computer):
    data = data_dir()
    data.mkdir(parents=True)
    assert private_folder_problem(data) is None  # the person's, as Python makes the tests' folders
    save_server_token(data, EMILY, TOKEN)
    _icacls(data, "/grant", "*S-1-5-32-545:(OI)(CI)R")  # Users may read, as a folder outside the profile lets them
    with pytest.raises(NotConnected, match="icacls"):
        load_server_token(data, EMILY)
    (data / "server.token").unlink()
    with pytest.raises(NotConnected, match="icacls"):
        save_server_token(data, EMILY, TOKEN)
    assert not (data / "server.token").exists()


@WINDOWS
def test_on_windows_a_folder_as_a_profile_holds_it_is_the_persons_alone(computer):
    """Latet's default data folder, in the person's profile: the person, SYSTEM and the Administrators."""
    profile = computer / "profile"
    profile.mkdir()
    me = folders.my_sid()
    _icacls(profile, "/inheritance:r", "/grant:r", f"*{me}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    assert private_folder_problem(profile) is None
    _icacls(profile, "/grant", "*S-1-5-11:(OI)(CI)M")  # and Authenticated Users, as a drive's root gives them
    assert "icacls" in (private_folder_problem(profile) or "")


ME, OTHER, ALL, READ = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", 0x1F01FF, 0x1200A9
PERSON_SYSTEM_ADMINISTRATORS = [Grant(ME, ALL, False), Grant(SYSTEM, ALL, False), Grant(ADMINISTRATORS, ALL, False)]


@pytest.mark.parametrize(
    ("owner", "grants", "says"),
    [
        (OTHER, PERSON_SYSTEM_ADMINISTRATORS, "/setowner"),  # its owner may rewrite the list at will
        (None, PERSON_SYSTEM_ADMINISTRATORS, "/setowner"),
        (
            OTHER,
            [Grant(SYSTEM, ALL, False), Grant(ADMINISTRATORS, ALL, False), Grant(OWNER_RIGHTS, ALL, False)],
            "/setowner",
        ),
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant("S-1-5-32-545", READ, False)], "/inheritance:r"),  # Users
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant("S-1-5-11", READ, True)], "/inheritance:r"),  # for what is made
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant(CREATOR_OWNER, ALL, False)], "/inheritance:r"),
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant("", ALL, False)], "/inheritance:r"),  # an entry of a kind not read
    ],
    ids=[
        "another owner",
        "no owner",
        "another owner and OWNER RIGHTS",
        "Users",
        "Authenticated Users on what is made inside",
        "CREATOR OWNER on the folder itself",
        "an entry not read",
    ],
)
def test_a_windows_folder_another_account_owns_or_may_open_is_refused(monkeypatch, owner, grants, says):
    monkeypatch.setattr(folders, "my_sid", lambda: ME)
    monkeypatch.setattr(folders, "folder_grants", lambda folder: (owner, grants))
    problem = folders._windows_folder_problem(Path("D:/latet"))
    assert problem is not None and says in problem and f"*{ME}" in problem
    if says == "/setowner":
        assert (owner or "no owner") in problem


@pytest.mark.parametrize(
    ("owner", "grants"),
    [
        (ME, PERSON_SYSTEM_ADMINISTRATORS),
        (ADMINISTRATORS, PERSON_SYSTEM_ADMINISTRATORS),  # what an elevated account makes
        (SYSTEM, PERSON_SYSTEM_ADMINISTRATORS),
        (
            ME,
            [Grant(SYSTEM, ALL, False), Grant(ADMINISTRATORS, ALL, False), Grant(OWNER_RIGHTS, ALL, False)],
        ),  # Python's
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant(CREATOR_OWNER, ALL, True)]),
        (ME, [*PERSON_SYSTEM_ADMINISTRATORS, Grant("S-1-15-2-1", ALL, False), Grant("S-1-15-3-1024-1", READ, True)]),
    ],
    ids=["the person's", "the Administrators'", "SYSTEM's", "as Python makes it private", "CREATOR OWNER", "app SIDs"],
)
def test_a_windows_folder_of_the_persons_alone_is_taken(monkeypatch, owner, grants):
    monkeypatch.setattr(folders, "my_sid", lambda: ME)
    monkeypatch.setattr(folders, "folder_grants", lambda folder: (owner, grants))
    assert folders._windows_folder_problem(Path("D:/latet")) is None


@WINDOWS
def test_on_windows_the_servers_key_is_not_written_in_a_folder_another_account_owns(tmp_path, monkeypatch):
    me = folders.my_sid()
    monkeypatch.setattr(folders, "folder_grants", lambda folder: (OTHER, [Grant(me, ALL, False)]))
    with pytest.raises(PermissionError, match="/setowner"):
        make_certificate(URL, tmp_path / "tls")
    assert list((tmp_path / "tls").iterdir()) == []


@WINDOWS
def test_on_windows_no_other_entry_of_latet_makes_the_token_roam(keyring_on, monkeypatch):
    """Windows' credential manager as keyring uses it, in a dict: one entry for each target, and an entry another one
    displaces saved again under ``<user>@<service>``, with the persistence of the instance that displaces it."""
    from keyring.backends import Windows

    class Vault(Windows.WinVaultKeyring):
        def __init__(self):
            super().__init__()
            self.kept = {}

        def _read_credential(self, target):
            found = self.kept.get(target)
            return Windows.DecodingCredential(found) if found else None

        def _set_password(self, target, username, password):
            blob = password.encode("utf-16")
            self.kept[target] = {"UserName": username, "CredentialBlob": blob, "Persist": self.persist}

        def _delete_password(self, target):
            self.kept.pop(target, None)

    vault = Vault()
    monkeypatch.setattr(keyring.core, "_keyring_backend", vault)

    def tokens():
        return [kept for kept in vault.kept.values() if kept["UserName"] == "server-token"]

    assert save_server_token(data_dir(), EMILY, TOKEN) == "keyring"
    vault.set_password("latet", "root-key", "a root key")  # as latet.storage.keys writes Latet's other entries
    vault.set_password("latet", "reviewer-api-key", "a reviewer's key")
    [kept] = tokens()
    assert kept["Persist"] == Windows.win32cred.CRED_PERSIST_LOCAL_MACHINE  # never saved again as one that roams
    newer = "latet_pat_" + "n" * 43
    assert save_server_token(data_dir(), EMILY, newer) == "keyring"
    [kept] = tokens()  # no older copy saved aside
    assert (
        load_server_token(data_dir(), EMILY) == newer
        and kept["Persist"] == Windows.win32cred.CRED_PERSIST_LOCAL_MACHINE
    )


@WINDOWS
def test_on_windows_the_keyring_keeps_the_token_for_this_computer_alone(monkeypatch):
    from keyring.backends import Windows

    monkeypatch.setattr(keyring.core, "_keyring_backend", Windows.WinVaultKeyring())  # nothing is asked of it here
    local = connection_module._keyring()
    assert local.persist == Windows.win32cred.CRED_PERSIST_LOCAL_MACHINE  # not CRED_PERSIST_ENTERPRISE, which roams


# --- the connection file --------------------------------------------------------------------------------------


def test_agents_go_to_this_computer_unless_it_is_connected(computer):
    assert agent_base() == "http://127.0.0.1:5283"
    save_connection(EMILY)
    with pytest.raises(NotConnected, match="token is missing"):
        agent_base()
    token = "latet_pat_" + "y" * 43
    save_server_token(data_dir(), EMILY, token)
    assert agent_base() == f"{URL}/u/{route_key(token)}"
    forget_connection()
    assert agent_base() == "http://127.0.0.1:5283"


def test_the_connection_file_is_this_computers_and_private(computer):
    assert connection_path() == data_dir() / "connection.yaml"  # not beside config.yaml, which roams with a profile
    path = save_connection(EMILY)
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0  # it holds the person's name
    assert load_connection() == EMILY


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "http://latet.acme.lan"),
        ("url", "https://Latet.acme.lan"),
        ("user", "emily"),
        ("user", "u_EMILYCARTE"),
        ("fingerprint", "sha256:../../outside"),
        ("fingerprint", PINNED.upper()),
        ("user_name", "E" * 1_001),
    ],
)
def test_a_connection_is_checked_field_by_field(field, value):
    with pytest.raises(ValidationError) as refused:
        Connection(**{"url": URL, "user": "u_emilycarte", "user_name": "Emily Carter", field: value})
    assert value not in str(refused.value)


def test_an_identity_id_is_read_as_e0b_makes_them():
    from latet.server import identities

    assert connection_module.USER_ID.pattern == identities.IDENTITY_ID.pattern


@pytest.mark.parametrize(
    ("text", "says"),
    [
        (f"url: http://latet.acme.lan\nuser: u_emilycarte\nuser_name: Emily Carter\n{TOKEN}: kept\n", "url: "),
        ("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: Emily Carter\nfingerprint: sha256:../x\n", "fin"),
        ("url: [https://latet.acme.lan\nuser_name: Emily Carter\n", "line 2"),
        (f"- {TOKEN}\n- Emily Carter\n", "the file"),
    ],
)
def test_a_connection_file_that_is_not_one_is_named_and_not_quoted(computer, text, says):
    path = connection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    with pytest.raises(NotConnected) as refused:
        load_connection()
    words = refused.value.message
    assert str(path) in words and says in words
    assert TOKEN not in words and "Emily" not in words and "../x" not in words


def test_the_pinned_certificate_is_given_to_agents_only_as_pinned_and_never_an_authority(computer, tmp_path):
    crt, _, fingerprint = make_certificate(URL, tmp_path / "tls")
    pinned = Connection(url=URL, user="u_emilycarte", user_name="Emily Carter", fingerprint=fingerprint)
    assert server_certificate_file(pinned) is None  # not pinned yet
    certificates.save(data_dir(), crt.read_text())
    path = server_certificate_file(pinned)
    assert path == data_dir() / "certificates" / f"{fingerprint.removeprefix('sha256:')}.pem"
    path.write_text(make_certificate(URL, tmp_path / "other")[0].read_text(), "utf-8")  # replaced by another
    assert server_certificate_file(pinned) is None
    _, _, authority, _ = _certificate(tmp_path, "openssl", ca=True)
    trusted = certificates.save(data_dir(), authority.read_text())  # kept by `latet llm trust`, say
    assert server_certificate_file(pinned.model_copy(update={"fingerprint": trusted})) is None


# --- which server a command asks --------------------------------------------------------------------------------


def test_a_command_finds_its_server_from_the_option_the_variable_or_the_connection(computer, monkeypatch):
    with pytest.raises(NotConnected, match="which server"):
        server_target()
    save_connection(Connection(url=URL, user="u_emilycarte", user_name="Emily Carter", fingerprint=PINNED))
    assert server_target() == (URL, PINNED)
    monkeypatch.setenv("LATET_SERVER_URL", "https://other.acme.lan")
    assert server_target() == ("https://other.acme.lan", None)
    assert server_target("https://third.acme.lan/", ":".join(["AB"] * 32)) == ("https://third.acme.lan", PINNED)


def test_a_wrong_server_is_named_by_where_it_came_from_and_never_repeated(computer, monkeypatch):
    monkeypatch.setenv("LATET_SERVER_URL", "https://ci:hunter2@latet.acme.lan")
    with pytest.raises(NotConnected) as refused:
        server_target()
    assert "LATET_SERVER_URL" in refused.value.message and "hunter2" not in refused.value.message
    with pytest.raises(NotConnected) as refused:
        server_target("https://ci:hunter2@latet.acme.lan")
    assert "--server" in refused.value.message and "hunter2" not in refused.value.message
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", "sha256:hunter2")
    with pytest.raises(NotConnected) as refused:
        server_target()
    assert "LATET_SERVER_FINGERPRINT" in refused.value.message and "hunter2" not in refused.value.message


def test_a_key_file_holds_an_api_key_alone(tmp_path):
    key = "latet_key_" + "k" * 43
    (tmp_path / "ci.key").write_text(key + "\n")
    assert read_key(tmp_path / "ci.key") == key
    (tmp_path / "bom.key").write_bytes(b"\xef\xbb\xbf" + key.encode() + b"\r\n")
    assert read_key(tmp_path / "bom.key") == key
    for name, text in (
        ("other.key", "sk-something-else"),
        ("two.key", f"{key}\nexport EVIL=1\n"),
        ("long.key", key + " " * 2_000),
        ("short.key", key[:-1]),
    ):
        (tmp_path / name).write_text(text)
        with pytest.raises(NotConnected, match="no API key") as refused:
            read_key(tmp_path / name)
        assert text.strip() not in refused.value.message
    with pytest.raises(NotConnected) as refused:
        read_key(tmp_path / "missing.key")
    assert str(tmp_path / "missing.key") in refused.value.message and "Errno" not in refused.value.message


# --- asking the server over HTTPS ---------------------------------------------------------------------------------


def test_a_computer_enrols_and_asks_with_the_certificate_it_pinned(latet_server):
    pinned = pin_certificate(latet_server.url, certificates.readable(latet_server.fingerprint))
    with ServerClient(latet_server.url, None, pinned) as client:
        enrolled = client.enrol(latet_server.code, "laptop")
    token, key = enrolled["token"], enrolled["computer_key"]
    assert enrolled == {  # no address
        "token": token,
        "computer_key": key,
        "identity": {"id": "u_emilycarte", "name": "Emily Carter"},
    }
    with ServerClient(latet_server.url, token, pinned, key=key) as client:
        who = client.whoami()
        assert who["name"] == "Emily Carter" and not {"url", "base", "anthropic", "openai"} & set(who)
        assert client.login_code().startswith(f"{latet_server.url}/_latet/ui/login?code=")
        assert client.request("GET", "/_latet/server/v1/whoami").status_code == 200  # the whole path too
        client.put_identity({"usernames": ["emily"]})
        client.revoke()
    assert latet_server.identities == [{"usernames": ["emily"]}] and token not in latet_server.tokens


def test_the_sign_in_link_is_made_on_the_computer_and_never_taken_from_the_server(latet_server):
    pinned, token = _enrolled(latet_server)
    code = "c" * 32
    latet_server.answers["login-code"] = JSONResponse({"code": code, "url": "file:///etc/passwd"})
    with ServerClient(latet_server.url, token, pinned, key=latet_server.keys[token]) as client:
        assert client.login_code() == f"{latet_server.url}/_latet/ui/login?code={code}"
        for other in ("c" * 15, "c" * 129, "c" * 31 + "/", "c" * 31 + "&", 42, None):
            latet_server.answers["login-code"] = JSONResponse({"code": other, "url": f"{latet_server.url}/x"})
            with pytest.raises(ServerError, match="sign-in code"):
                client.login_code()


def test_the_paths_a_computer_asks_are_e0bs():
    from latet.api.rest import UI
    from latet.server.settings import SERVER_API

    assert (client_module.UI, client_module.SERVER_API) == (UI, SERVER_API)


@pytest.mark.parametrize(
    "answer",
    [
        {"token": "latet_key_" + "k" * 43, "identity": {"id": "u_emilycarte", "name": "Emily Carter"}},
        {"token": TOKEN, "identity": {"id": "u_emily", "name": "Emily Carter"}},
        {"token": TOKEN, "identity": {"id": "u_emilycarte", "name": "E" * 1_001}},
        {"token": TOKEN, "identity": "u_emilycarte"},
        {"token": TOKEN},
    ],
)
def test_an_answer_that_is_not_an_enrolment_is_refused(latet_server, answer):
    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    latet_server.answers["enrol"] = JSONResponse(answer)
    with ServerClient(latet_server.url, None, pinned) as client, pytest.raises(ServerError) as refused:
        client.enrol(latet_server.code, "laptop")
    assert refused.value.status == 0 and "not an enrollment" in refused.value.message


def test_a_self_signed_server_without_its_fingerprint_says_how_to_trust_it(latet_server):
    with ServerClient(latet_server.url, None) as client, pytest.raises(ServerError) as refused:
        client.enrol(latet_server.code, "laptop")
    assert refused.value.status == 0 and "--fingerprint" in refused.value.message
    assert latet_server.code is not None  # nothing was sent


def test_another_certificate_than_the_fingerprint_given_is_refused_without_showing_it_and_nothing_kept(
    latet_server, tmp_path
):
    _, _, other = make_certificate(latet_server.url, tmp_path / "other")
    with pytest.raises(ServerError, match="another certificate") as refused:
        pin_certificate(latet_server.url, other)
    shown = latet_server.fingerprint
    for form in (shown, certificates.readable(shown), shown.removeprefix("sha256:")):
        assert form.lower() not in refused.value.message.lower()  # only the administrator's counts
    assert "administrator" in refused.value.message
    assert certificates.load(data_dir(), other) is None and certificates.load(data_dir(), shown) is None


def test_a_certificate_replaced_after_it_was_pinned_is_named_and_not_taken(latet_server, tmp_path):
    crt, _, other = make_certificate(latet_server.url, tmp_path / "other")
    certificates.save(data_dir(), crt.read_text())  # pinned once; the server now shows its own
    with ServerClient(latet_server.url, None, other) as client, pytest.raises(ServerError, match="other than the one"):
        client.whoami()
    with ServerClient(latet_server.url, None, PINNED) as client, pytest.raises(ServerError, match="missing"):
        client.whoami()  # pinned, then gone: nothing is accepted


@pytest.mark.parametrize("ca", [True, None], ids=["CA:TRUE", "no BasicConstraints"])
def test_a_certificate_that_is_an_authority_is_never_pinned_nor_trusted_for_a_server(computer, tmp_path, ca):
    _, _, crt, key = _certificate(tmp_path, "127.0.0.1", ca=ca)
    server = FakeLatetServer(tmp_path, certificate=(crt, key)).start()
    try:
        with pytest.raises(ServerError, match="is an authority") as refused:
            pin_certificate(server.url, server.fingerprint)
        assert refused.value.status == 0 and certificates.load(data_dir(), server.fingerprint) is None
        certificates.save(data_dir(), server.pem)  # kept before, by `latet llm trust` say
        with pytest.raises(ServerError, match="is an authority"):
            pin_certificate(server.url, server.fingerprint)
        with ServerClient(server.url, None, server.fingerprint) as client, pytest.raises(ServerError) as refused:
            client.enrol(server.code, "laptop")  # the very certificate the server shows: not trusted for it
        assert "is an authority" in refused.value.message and server.code is not None and server.asked == []
    finally:
        server.stop()


def test_latets_pinned_context_trusts_no_certificate_its_pins_key_signs_for_another_host(computer, tmp_path):
    # A server's own certificate, CA:FALSE (as `openssl req -x509 -addext basicConstraints=critical,CA:FALSE` makes it).
    anchor, anchor_key, anchor_crt, anchor_file = _certificate(tmp_path, "server", ca=False, host="127.0.0.1")
    _handshake(tls_context(certificates.save(data_dir(), anchor_crt.read_text())), anchor_crt, anchor_file, "127.0.0.1")
    *_, crt, key = _certificate(tmp_path, "leaf", ca=False, issuer=(anchor, anchor_key), host="api.github.com")
    with pytest.raises(ssl.SSLCertVerificationError, match="invalid CA"):
        _handshake(tls_context(certificates.fingerprint_of(anchor_crt.read_text())), crt, key, "api.github.com")
    # Why a pin is never an authority: a CA:TRUE one, in the same context, lets its key sign for any host.
    authority, authority_key, authority_crt, _ = _certificate(tmp_path, "authority", ca=True, host="127.0.0.1")
    *_, crt, key = _certificate(tmp_path, "signed", ca=False, issuer=(authority, authority_key), host="api.github.com")
    _handshake(certificates.context(authority_crt.read_text()), crt, key, "api.github.com")  # accepted
    trusted = certificates.save(data_dir(), authority_crt.read_text())
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls_context(trusted), crt, key, "api.github.com")  # Latet's own trusts no authority for a server


def test_a_client_takes_only_an_address_as_server_url_keeps_it(latet_server):
    for given in (latet_server.url + "/", latet_server.url.upper(), f"https://u:p@127.0.0.1:{latet_server.port}"):
        with pytest.raises(ValueError, match="server_url"):
            ServerClient(given, None)
        with pytest.raises(ValueError, match="server_url"):
            pin_certificate(given, latet_server.fingerprint)
    assert latet_server.asked == []


def test_a_client_shows_its_address_alone():
    client = ServerClient(URL, TOKEN)
    try:
        assert repr(client) == f"ServerClient({URL!r})" and TOKEN not in repr(client)
    finally:
        client.close()


def test_a_refusal_carries_the_servers_reason_and_silence_is_status_0(latet_server):
    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    with ServerClient(latet_server.url, None, pinned) as client, pytest.raises(ServerError) as refused:
        client.enrol("not-a-code", "laptop")
    assert refused.value.status == 401 and "used or late" in refused.value.message
    with ServerClient(f"https://127.0.0.1:{free_port()}", None) as client, pytest.raises(ServerError) as away:
        client.whoami()
    assert away.value.status == 0 and "does not answer" in away.value.message


def test_a_refusal_is_read_as_e0b_answers_it_and_made_printable(latet_server):
    pinned, token = _enrolled(latet_server)
    escape, override, slash = chr(0x1B), chr(0x202E), chr(0x5C)
    said = f"{escape}[31mred{override}" + "x" * 600
    details = [chr(7) + "bell", {"loc": ["terms", 0], "msg": "one\ntwo"}, 3, *(["more"] * 30)]
    latet_server.answers["whoami"] = JSONResponse({"error": said, "details": details}, status_code=400)
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.whoami()
    error = refused.value
    assert (
        error.status == 400 and error.message.startswith(f"{slash}x1b[31mred{slash}u202e") and len(error.message) == 500
    )
    assert error.details[:2] == (f"{slash}x07bell", f"one{slash}ntwo") and len(error.details) == 20
    assert not any(ord(char) < 0x20 or char == override for char in error.message + "".join(error.details))
    assert printable("Emily " + chr(0x2066) + "Carter" + chr(0x85)) == f"Emily {slash}u2066Carter{slash}x85"


@pytest.mark.parametrize(("given", "seconds"), [("120", 120), ("1.5", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)])
def test_retry_after_is_read_as_whole_seconds_alone(latet_server, given, seconds):
    pinned, token = _enrolled(latet_server)
    latet_server.answers["whoami"] = JSONResponse(
        {"error": "slow down"}, status_code=429, headers={"retry-after": given}
    )
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.whoami()
    assert (refused.value.status, refused.value.retry_after) == (429, seconds)
    latet_server.answers["whoami"] = JSONResponse({}, status_code=429, headers={"retry-after": chr(0xB2)})
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.whoami()
    assert refused.value.retry_after is None  # a superscript two is a digit to Python's isdigit, not to HTTP


def test_a_busy_server_is_asked_again_once_after_its_retry_after(latet_server):
    pinned, token = _enrolled(latet_server)
    slept = []
    with ServerClient(latet_server.url, token, pinned, sleep=slept.append) as client:
        latet_server.busy = 1
        assert client.whoami()["name"] == "Emily Carter" and slept == [1]
        latet_server.busy = 2
        with pytest.raises(ServerError) as refused:
            client.whoami()
        assert (refused.value.status, refused.value.retry_after, slept) == (503, 1, [1, 1])
        for headers in ({"retry-after": "6"}, {}):  # longer than a computer waits, or not said
            latet_server.answers["whoami"] = JSONResponse({"error": "busy"}, status_code=503, headers=headers)
            with pytest.raises(ServerError, match="busy"):
                client.whoami()
        assert slept == [1, 1]
        latet_server.answers.clear()
        latet_server.limited = True
        with pytest.raises(ServerError) as refused:
            client.whoami()
        assert (refused.value.status, refused.value.retry_after, slept) == (429, 60, [1, 1])  # said, never waited


@pytest.mark.parametrize("allowance", [119, 120, 121])
def test_opt_in_receive_budget_counts_the_busy_body_and_its_successful_retry(latet_server, allowance):
    from latet.server.client import ReceiveBudget

    pinned, token = _enrolled(latet_server)
    busy = b'{"error":"busy"}' + b" " * 44
    success = b'{"ok":true}' + b" " * 49
    assert len(busy) == len(success) == 60
    latet_server.answers["whoami"] = [
        Response(busy, status_code=503, headers={"retry-after": "1"}, media_type="application/json"),
        Response(success, media_type="application/json"),
    ]
    before = len(latet_server.asked)
    slept = []
    receive = ReceiveBudget(allowance)
    with ServerClient(latet_server.url, token, pinned, sleep=slept.append) as client:
        if allowance < 120:
            with pytest.raises(ServerError) as refused:
                client.request("GET", "whoami", limit=80, receive=receive)
            assert refused.value.status == 0
            assert receive.received >= 60
        else:
            assert client.request("GET", "whoami", limit=80, receive=receive).json() == {"ok": True}
            assert receive.received == 120
    assert slept == [1]
    assert latet_server.asked[before:] == [("GET", "whoami"), ("GET", "whoami")]


def test_an_answer_is_read_within_its_bound_and_never_further(latet_server):
    pinned, token = _enrolled(latet_server)
    big = b'{"name": "' + b"x" * (2 * 1024 * 1024) + b'"}'

    def chunks():
        for start in range(0, len(big), 64 * 1024):
            yield big[start : start + 64 * 1024]

    with ServerClient(latet_server.url, token, pinned) as client:
        for answer in (Response(big, media_type="application/json"), StreamingResponse(chunks())):
            latet_server.answers["whoami"] = answer
            with pytest.raises(ServerError, match="more than") as refused:
                client.whoami()
            assert refused.value.status == 0
        latet_server.answers["whoami"] = Response(big, media_type="application/json")
        allowed = client.request("GET", "whoami", limit=4 * 1024 * 1024)
        assert len(allowed.content) == len(big)  # a caller may allow more
        with pytest.raises(ValueError, match="bytes"):
            client.request("GET", "whoami", limit=64 * 1024 * 1024)  # up to 32 MiB


def test_an_answer_that_comes_too_late_is_cut_off(latet_server):
    pinned, token = _enrolled(latet_server)
    reached = len(latet_server.asked)

    def late():  # the call starts in time, and its deadline passes once it reached the server
        return 1_000.0 if len(latet_server.asked) > reached else 0.0

    with (
        ServerClient(latet_server.url, token, pinned, clock=late) as client,
        pytest.raises(ServerError, match="in time") as refused,
    ):
        client.whoami()
    assert refused.value.status == 0 and latet_server.asked[reached:] == [("GET", "whoami")]  # sent, its answer cut


def test_a_call_whose_connection_comes_after_its_deadline_sends_nothing(latet_server, monkeypatch):
    """A connection slow to come, made once its caller was told the server did not answer in time, carries nothing:
    for `latet connect` the one-time code would be spent, and the token issued for it lost."""
    from httpcore._backends.sync import SyncBackend

    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    given_up = threading.Event()
    connect_tcp = SyncBackend.connect_tcp

    def slow(self, *args, **kwargs):
        given_up.wait(10)  # the network brings the connection only once its caller gave up on it
        return connect_tcp(self, *args, **kwargs)

    monkeypatch.setattr(SyncBackend, "connect_tcp", slow)
    before = set(threading.enumerate())
    # Its clock stands still: what stops the call is its caller having left it, not the time it reads.
    with ServerClient(latet_server.url, None, pinned, clock=lambda: 0.0) as client:
        with pytest.raises(ServerError, match="in time"):
            client.request("POST", "enrol", body={"code": latet_server.code, "label": "laptop"}, deadline=0.5)
        given_up.set()
        for worker in set(threading.enumerate()) - before:
            if worker.name == "latet-server-call":
                worker.join(10)  # its connection made, its handshake done: and then?
                assert not worker.is_alive()
    assert latet_server.asked == [] and latet_server.code is not None  # nothing reached it: the code is unspent


def test_a_call_whose_wait_is_interrupted_sends_nothing_after(latet_server, monkeypatch):
    """A Ctrl+C while the caller waits for its call: the call is left as at its deadline, and a connection that comes
    after carries nothing."""
    from httpcore._backends.sync import SyncBackend

    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    interrupted = threading.Event()
    connect_tcp, join = SyncBackend.connect_tcp, threading.Thread.join

    def slow(self, *args, **kwargs):
        interrupted.wait(10)  # the connection comes once the caller was interrupted
        return connect_tcp(self, *args, **kwargs)

    def ctrl_c(self, timeout=None):
        if self.name == "latet-server-call" and not interrupted.is_set():
            raise KeyboardInterrupt
        return join(self, timeout)

    monkeypatch.setattr(SyncBackend, "connect_tcp", slow)
    monkeypatch.setattr(threading.Thread, "join", ctrl_c)
    before = set(threading.enumerate())
    with ServerClient(latet_server.url, None, pinned, clock=lambda: 0.0) as client, pytest.raises(KeyboardInterrupt):
        client.request("POST", "enrol", body={"code": latet_server.code, "label": "laptop"})
    interrupted.set()
    for worker in set(threading.enumerate()) - before:
        if worker.name == "latet-server-call":
            join(worker, 10)
            assert not worker.is_alive()
    assert latet_server.asked == [] and latet_server.code is not None  # nothing reached it: the code is unspent


def test_a_call_past_its_deadline_on_a_kept_connection_sends_nothing(latet_server):
    pinned, token = _enrolled(latet_server)
    late = threading.Event()

    def clock():  # past the deadline as the call's worker reads it, once `late` is set: before its request leaves
        return 1_000.0 if late.is_set() and threading.current_thread().name == "latet-server-call" else 0.0

    with ServerClient(latet_server.url, token, pinned, key=latet_server.keys[token], clock=clock) as client:
        client.whoami()  # its connection made, and kept for the next call
        late.set()
        with pytest.raises(ServerError, match="in time") as refused:
            client.put_identity({"usernames": ["emily"]})
    assert refused.value.status == 0 and latet_server.identities == []


def test_a_call_that_may_no_longer_send_stops_before_its_request_and_closes_a_connection_just_made():
    closed = []
    stream = SimpleNamespace(close=lambda: closed.append(True))
    gate = client_module._Gate(URL, 30.0, lambda: 0.0)
    for event in ("connection.start_tls.complete", "http11.send_request_headers.started"):
        gate.trace(event, {"return_value": stream})  # in time: it goes on
    assert closed == []
    gate.leave()  # its caller left it at its deadline
    for event in ("connection.start_tls.complete", "http11.send_request_headers.started"):
        with pytest.raises(ServerError, match="in time"):
            gate.trace(event, {"return_value": stream})
    assert closed == [True]  # the connection the handshake had just made: nothing goes on it
    gate.trace("http11.response_closed.started", {})  # what httpcore does after a refusal goes on
    late = client_module._Gate(URL, 30.0, lambda: 31.0)  # its deadline passed, its caller not told yet
    with pytest.raises(ServerError, match="in time"):
        late.trace("http11.send_request_headers.started", {})


TLS_RECORD = b"\x16\x03\x03\x40\x00"  # a handshake record of 16 KiB to come, as a server's first flight starts
SLOW_HEADERS = b"HTTP/1.1 200 OK\r\nx-slow: "


@pytest.mark.parametrize(
    ("trickle", "scheme", "said"),
    [((TLS_RECORD,), "https", "in time|does not answer"), ((SLOW_HEADERS, b"a"), "http", "in time")],
    ids=["a handshake a byte at a time", "headers a byte at a time"],
    indirect=["trickle"],
)
def test_an_exchange_a_server_sends_a_byte_at_a_time_ends_at_its_deadline(computer, trickle, scheme, said):
    # A handshake is bounded twice, by the call's deadline and by Python's ssl (the connection's time, the whole
    # handshake's): whichever ends it first says so, at the same time.
    with ServerClient(f"{scheme}://127.0.0.1:{trickle.port}", TOKEN) as client:
        started = time.monotonic()
        with pytest.raises(ServerError, match=said) as refused:
            client.request("GET", "whoami", deadline=1.0)
        took = time.monotonic() - started
        assert refused.value.status == 0 and took < 4.0, took  # each byte came well within httpx's time for a read
        trickle.close()  # and the client is still usable: a new one stands for the one left at the deadline
        with pytest.raises(ServerError, match="does not answer"):
            client.request("GET", "whoami", deadline=5.0)


@pytest.mark.parametrize("trickle", [(TLS_RECORD,)], indirect=True)
def test_a_certificate_shown_a_byte_at_a_time_is_not_waited_for_past_its_time(trickle):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        certificates.fetch(f"https://127.0.0.1:{trickle.port}", timeout=1.0)
    assert time.monotonic() - started < 4.0


@pytest.mark.parametrize("cut", [ssl.SSLWantReadError, ssl.SSLWantWriteError])
def test_a_handshake_python_ends_wanting_more_at_its_time_is_a_timeout(monkeypatch, cut):
    """On Windows, Python 3.12's ssl can end a handshake its socket's time cut short wanting to read or write, not with
    a timeout: it is one all the same, and said as one."""

    def cut_short(address, **kwargs):
        raise cut("The operation did not complete (read) (_ssl.c:1010)")

    monkeypatch.setattr(ssl, "get_server_certificate", cut_short)
    with pytest.raises(TimeoutError, match="1 seconds"):
        certificates.fetch("https://127.0.0.1:9", timeout=1.0)


@pytest.mark.parametrize(
    "answer",
    [
        Response(b"[" * 100_000 + b"]" * 100_000, media_type="application/json"),
        Response(b"[]", media_type="application/json"),
        Response(b"not json", media_type="text/plain"),
        Response(b"", status_code=200),
    ],
    ids=["nested deep", "a list", "not JSON", "nothing"],
)
def test_an_answer_that_is_not_a_json_object_is_refused(latet_server, answer):
    pinned, token = _enrolled(latet_server)
    latet_server.answers["whoami"] = answer
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.whoami()
    assert refused.value.status == 0 and "JSON object" in refused.value.message
    latet_server.answers["whoami"] = Response(b"[" * 100_000, status_code=400)
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.whoami()
    assert (refused.value.status, refused.value.message) == (400, "HTTP 400")


def test_a_compressed_answer_is_not_read(latet_server):
    pinned, token = _enrolled(latet_server)
    latet_server.answers["whoami"] = Response(b"\x1f\x8b", headers={"content-encoding": "gzip"})
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError, match="compressed"):
        client.whoami()


def test_a_call_the_client_cannot_make_is_status_0(latet_server):
    pinned, token = _enrolled(latet_server)
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.request("GET", "who" + chr(0) + "ami")
    assert refused.value.status == 0


def test_a_redirect_is_never_followed(latet_server):
    pinned, token = _enrolled(latet_server)
    with ServerClient(latet_server.url, token, pinned) as client, pytest.raises(ServerError) as refused:
        client.request("GET", "moved")
    assert refused.value.status == 307 and ("GET", "whoami") not in latet_server.asked


def test_no_proxy_of_the_environment_is_taken(latet_server, monkeypatch):
    pinned, token = _enrolled(latet_server)
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{free_port()}")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with ServerClient(latet_server.url, token, pinned) as client:
        assert client.whoami()["name"] == "Emily Carter"


def test_a_call_is_no_web_pages_and_the_server_refuses_one(latet_server):
    pinned, token = _enrolled(latet_server)
    with ServerClient(latet_server.url, token, pinned) as client:
        for header in ("Origin", "sec-fetch-site", "Sec-Fetch-Mode"):
            with pytest.raises(ValueError, match="web page"):
                client.request("GET", "whoami", headers={header: latet_server.url})
    assert ("GET", "whoami") not in latet_server.asked


def test_the_fake_server_answers_as_e0b_does(latet_server):
    import httpx

    pinned, token = _enrolled(latet_server)
    with httpx.Client(verify=tls_context(pinned), trust_env=False, base_url=latet_server.url) as plain:
        refused = plain.get("/_latet/server/v1/whoami", headers={"x-latet-token": "latet_pat_" + "0" * 43})
        assert (refused.status_code, refused.json()) == (401, {"error": "unauthorized"})
        assert refused.headers["cache-control"] == "no-store"
        page = plain.get("/_latet/server/v1/whoami", headers={"x-latet-token": token, "origin": latet_server.url})
        assert page.status_code == 403
        admin = plain.get("/_latet/server/v1/admin/status", headers={"x-latet-admin": ADMIN_TOKEN})
        assert admin.json()["edition"] == []
        latet_server.busy = 1
        busy = plain.get("/_latet/server/v1/whoami", headers={"x-latet-token": token})
        assert (busy.status_code, busy.headers["retry-after"]) == (503, "1")


# What a computer runs without the `server` extra (psycopg, psycopg_pool and redis made unimportable): Task 4's
# commands and Task 12's.
WITHOUT_EXTRA = [
    ["--help"], ["connect", "--help"], ["disconnect", "--help"], ["server", "--help"], ["server", "certificate", "--help"],
    ["server", "people", "--help"], ["server", "keys", "--help"], ["server", "admin-token", "--help"],
    ["server", "log", "--help"], ["server", "config", "--help"], ["server", "rules", "--help"],
]  # fmt: skip


def test_a_computer_needs_no_server_extra():
    code = (
        "import sys\n"
        "sys.modules['psycopg'] = None; sys.modules['psycopg_pool'] = None; sys.modules['redis'] = None\n"
        "import latet.server.connection, latet.server.client, latet.server.certificate\n"
        "import latet.cli.admin\n"
        "from typer.testing import CliRunner\n"
        "from latet.cli.main import app\n"
        f"for args in {WITHOUT_EXTRA!r}:\n"
        "    result = CliRunner().invoke(app, args)\n"
        "    assert result.exit_code == 0, (args, result.output)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr


def test_the_tests_carry_no_connection_and_no_server_of_yours():
    """tests/conftest.py takes your connection's and your server's variables out before any test runs, so that none
    reads, changes or removes your connection file, or asks your server with your administration token."""
    names = ("LATET_CONNECTION", "LATET_SERVER_URL", "LATET_SERVER_FINGERPRINT", "LATET_SERVER_ADMIN_TOKEN")
    code = f"import os\nimport tests.conftest\nprint(sorted(name for name in {names!r} if name in os.environ))\n"
    yours = {**os.environ, **dict.fromkeys(names, "yours")}
    root = Path(__file__).parents[2]
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False, cwd=root, env=yours
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_a_traceback_of_the_command_line_shows_no_locals():
    from latet.cli.main import app

    assert app.pretty_exceptions_show_locals is False


def test_a_command_that_lets_not_connected_out_says_its_words_with_exit_2():
    """Whichever command lets it out (Tasks 5, 7 and 13 add theirs): the refusal's words and exit code 2, as Typer
    shows an error, never a traceback."""
    import typer
    from typer.testing import CliRunner

    probe = typer.Typer()

    @probe.command()
    def needs_a_connection() -> None:
        raise NotConnected("this computer is connected to no Latet server: `latet connect` first")

    @probe.command()
    def other() -> None:
        """A second command, so that the first is named."""

    done = CliRunner().invoke(probe, ["needs-a-connection"])
    assert done.exit_code == 2 and "`latet connect` first" in done.output
    assert not isinstance(done.exception, NotConnected)
