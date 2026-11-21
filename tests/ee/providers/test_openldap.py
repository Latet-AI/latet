"""Term sync against a real directory (the release checklist's item 8, "A real directory tried"): OpenLDAP from Debian's
packages (openldap/Dockerfile, its configuration and entries in openldap/*.ldif), with TLS from a certificate authority
made for the run, set up as docs/src/content/docs/enterprise/term-sync.mdx says: a source over LDAPS and one over
STARTTLS, each signed in as a read-only service account whose password is the source's secret, with a `base`, a
`filter` and a mapping, the authority's PEM file in the term-sync folder. Everything on Latet's side goes through
`latet server terms`, as an administrator runs it. Opt-in: LATET_PROVIDER_TESTS=1, with debian:trixie-slim pulled by
hand (README.md); the image is built from it here, with Debian's own packages."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from latet.ee.settings import TERM_FILES
from latet.engine.types import SlotKind

from ..people.server_support import add_person, settle
from . import docker
from .conftest import HOST, PROVIDER_TESTS

pytestmark = PROVIDER_TESTS

BASE_IMAGE = "debian:trixie-slim"
IMAGE = "latet-prov-openldap:test"
HERE = Path(__file__).parent / "openldap"
SUFFIX = "dc=acme,dc=lan"
SYNC = f"cn=latet-sync,ou=services,{SUFFIX}"
KEYCLOAK = f"cn=keycloak,ou=services,{SUFFIX}"
AUTHORITY = "acme-ca.pem"  # the file's name in the term-sync folder
UNTRUSTED = (
    "the directory's certificate was not trusted: check the certificate authority's file (`certificate`), and that "
    "the certificate names the host in `url`"
)
NETWORK_NAME = "ldap.acme.lan"  # the directory's name on a network of the containers' own (test_keycloak_ldap.py)


def _name(text: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])


@dataclass
class Authority:
    """A certificate authority made for the run, which signs the directory's certificate."""

    key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    @classmethod
    def make(cls, name: str) -> Authority:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(_name(name))
            .issuer_name(_name(name))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
            )  # certificate and CRL signing
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        return cls(key, certificate)

    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    def server(self) -> tuple[bytes, bytes]:
        """A certificate for localhost and 127.0.0.1, and for the directory's name on the containers' own network
        (NETWORK_NAME, where Keycloak reaches it), signed by this authority, and its key, in PEM."""
        import ipaddress

        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                 x509.DNSName(NETWORK_NAME)]  # fmt: skip
        certificate = (
            x509.CertificateBuilder()
            .subject_name(_name("localhost"))
            .issuer_name(self.certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        return certificate.public_bytes(serialization.Encoding.PEM), key_pem


def ssha(password: str) -> str:
    """A password as slapd keeps one: {SSHA}, salted SHA-1."""
    salt = secrets.token_bytes(8)
    return "{SSHA}" + base64.b64encode(hashlib.sha1(password.encode() + salt).digest() + salt).decode()


@dataclass
class Directory:
    container: docker.Container
    authority: Authority
    passwords: dict[str, str]

    @property
    def ldaps(self) -> str:
        return f"ldaps://{HOST}:{self.container.ports[636]}"

    @property
    def ldap(self) -> str:
        return f"ldap://{HOST}:{self.container.ports[389]}"

    def delete(self, dn: str) -> None:
        """An entry taken out of the directory, by root on slapd's own socket."""
        done = self.container.run("ldapdelete", "-Q", "-Y", "EXTERNAL", "-H", "ldapi:///", dn)
        assert done.returncode == 0, done.stderr

    def version(self) -> str:
        done = self.container.run("dpkg-query", "--show", "--showformat", "${Version}", "slapd")
        return done.stdout.strip()


@pytest.fixture(scope="module")
def directory(tmp_path_factory):
    with running_directory(tmp_path_factory) as found:
        yield found


@contextmanager
def running_directory(tmp_path_factory):
    """OpenLDAP built from Debian's packages and started with the run's own TLS files and password hashes."""
    docker.require_image(BASE_IMAGE)
    docker.build(IMAGE, HERE)
    authority = Authority.make("Acme test authority")
    certificate, key = authority.server()
    passwords = {"sync": secrets.token_urlsafe(18), "keycloak": secrets.token_urlsafe(18),
                 "person": secrets.token_urlsafe(12)}  # fmt: skip
    setup = tmp_path_factory.mktemp("openldap-setup")
    (setup / "tls").mkdir()
    (setup / "tls" / "ca.pem").write_bytes(authority.pem())
    (setup / "tls" / "server.pem").write_bytes(certificate)
    (setup / "tls" / "server-key.pem").write_bytes(key)
    (setup / "config.ldif").write_bytes((HERE / "config.ldif").read_bytes())
    entries = (HERE / "directory.ldif").read_text("utf-8")
    for name, placeholder in (("sync", "@SYNC_HASH@"), ("keycloak", "@KEYCLOAK_HASH@"), ("person", "@PERSON_HASH@")):
        while placeholder in entries:  # a salt of its own for each
            entries = entries.replace(placeholder, ssha(passwords[name]), 1)
    (setup / "directory.ldif").write_text(entries, "utf-8", newline="\n")
    container = docker.start("openldap", IMAGE, ports=[389, 636], files={"/setup": setup})
    try:
        search = ("ldapsearch", "-Q", "-Y", "EXTERNAL", "-H", "ldapi:///", "-b", SUFFIX, "-s", "base", "dn")
        docker.wait(lambda: container.run(*search).returncode == 0, "slapd's start", container, seconds=60)
        yield Directory(container, authority, passwords)
    finally:
        container.remove()


@pytest.fixture
def folder(cluster, directory):
    """The server's term-sync folder, with the directory's certificate authority in it (and another, unrelated)."""
    found = cluster.server.data / TERM_FILES
    found.mkdir(parents=True, exist_ok=True)
    (found / AUTHORITY).write_bytes(directory.authority.pem())
    (found / "other-ca.pem").write_bytes(Authority.make("Another authority").pem())
    yield found
    for name in (AUTHORITY, "other-ca.pem"):
        (found / name).unlink(missing_ok=True)


def sources(directory, *, people_url=None, people_certificate=AUTHORITY, teams_url=None, teams_certificate=AUTHORITY):
    """The two sources, as `latet server terms sources set` takes them: the people over LDAPS, the teams over
    STARTTLS, each signed in as the read-only service account."""
    return (
        "sources:\n"
        "  - name: people\n"
        "    kind: ldap\n"
        f"    url: {people_url or directory.ldaps}\n"
        f"    bind: {SYNC}\n"
        f"    base: ou=people,{SUFFIX}\n"
        '    filter: "(objectClass=inetOrgPerson)"\n'
        f"    certificate: {people_certificate}\n"
        '    mapping: {"{givenName} {sn}": PERSON, mail: EMAIL}\n'
        "  - name: teams\n"
        "    kind: ldap\n"
        f"    url: {teams_url or directory.ldap}\n"
        "    start_tls: true\n"
        f"    bind: {SYNC}\n"
        f"    base: ou=groups,{SUFFIX}\n"
        '    filter: "(objectClass=groupOfNames)"\n'
        f"    certificate: {teams_certificate}\n"
        "    exclude: [latet-users]\n"
        "    mapping: {cn: TEAM}\n"
    )


def set_sources(latet_server, tmp_path, text, *, ok=True):
    path = tmp_path / "term-sync.yaml"
    path.write_text(text, "utf-8")
    return latet_server("terms", "sources", "set", str(path), "--yes", ok=ok)


def keep_secrets(latet_server, directory):
    for name in ("people", "teams"):
        kept = latet_server("terms", "secret", name, input=directory.passwords["sync"] + "\n")
        assert f"signing in as {SYNC}" in kept.output and f"Kept on the server for the source {name}." in kept.output


def masked(cluster, index, identity, text):
    runtime = cluster.clients[index].portal.call(cluster.servers[index].spaces.runtime, identity)
    engine = runtime.built.engine
    return engine.mask_text(text, SlotKind.USER_TEXT, engine.session("providers")).text


def status(cluster):
    return {s["name"]: s for s in cluster.server.edition.terms.status()["sources"]}


def settled(cluster, done, seconds=60.0):
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError(f"term sync did not settle: {status(cluster)}")
        time.sleep(0.5)


TEXT = (
    "Emily Carter and Priya Raman of Orion Payments, Daniel Okafor of Halcyon Research "
    "(daniel.okafor@acme.com) and latet-users"
)


def test_people_over_ldaps_and_teams_over_starttls_are_synced_masked_and_a_term_taken_out_is_no_longer_in_its_source(
    cluster, directory, folder, latet_server, tmp_path
):
    print(f"slapd {directory.version()}")
    set_sources(latet_server, tmp_path, sources(directory))
    keep_secrets(latet_server, directory)
    ran = latet_server("terms", "run")
    print(ran.output)
    shown = latet_server("terms", "status").output
    print(shown)
    found = status(cluster)
    assert found["people"]["terms"] == 6 and found["people"]["by_type"] == {"PERSON": 3, "EMAIL": 3}, found
    assert found["teams"]["terms"] == 2 and found["teams"]["by_type"] == {"TEAM": 2}, found  # latet-users excluded
    assert all(s["last"]["outcome"] == "ok" for s in found.values())

    person = add_person(cluster, "Mia Lorenz", "mia.lorenz@acme.com")
    synced = ("Emily Carter", "Priya Raman", "Orion Payments", "Daniel Okafor", "Halcyon Research",
              "daniel.okafor@acme.com")  # fmt: skip
    for index in (0, 1):  # every instance masks them, the other within 30 seconds
        settle(lambda index=index: not any(t in masked(cluster, index, person, TEXT) for t in synced), seconds=40)
        assert "latet-users" in masked(cluster, index, person, TEXT)  # in `exclude`
    print(masked(cluster, 1, person, TEXT))

    directory.delete(f"uid=priya.raman,ou=people,{SUFFIX}")
    again = latet_server("terms", "run", "people")
    print(again.output)
    found = status(cluster)
    assert found["people"]["terms"] == 4 and found["people"]["gone"] == 2, found
    gone = latet_server("terms", "gone").output
    print(gone)
    assert "Priya Raman" in gone and "priya.raman@acme.com" in gone and "2 terms no longer in their source" in gone
    said = masked(cluster, 1, person, TEXT)
    assert "Priya Raman" not in said  # still masked until someone removes it


def test_the_service_account_reads_and_changes_nothing(directory):
    """The directory's ACL: Latet's service account reads people and teams, and is refused any change."""
    import ssl

    from ldap3 import MODIFY_REPLACE, Connection, Server, Tls

    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=directory.authority.pem().decode(), valid_names=["localhost"])
    server = Server("127.0.0.1", port=directory.container.ports[636], use_ssl=True, tls=tls)
    with Connection(server, user=SYNC, password=directory.passwords["sync"], auto_bind=True) as conn:
        assert conn.search(f"ou=people,{SUFFIX}", "(objectClass=inetOrgPerson)", attributes=["cn"])
        assert len(conn.entries) >= 2
        assert not conn.modify(f"uid=emily.carter,ou=people,{SUFFIX}", {"sn": [(MODIFY_REPLACE, ["Changed"])]})
        assert conn.result["description"] == "insufficientAccessRights", conn.result
        conn.search(f"ou=services,{SUFFIX}", "(cn=latet-sync)", attributes=["userPassword"])
        assert all(not entry.entry_attributes_as_dict.get("userPassword") for entry in conn.entries)
    with Connection(server, auto_bind=True) as anonymous:
        anonymous.search(SUFFIX, "(objectClass=*)")
        assert anonymous.entries == []


def test_plain_ldap_and_a_wrong_certificate_authority_are_refused(cluster, directory, folder, latet_server, tmp_path):
    plain = sources(directory, teams_url="ldap://dc1.acme.lan").replace("start_tls: true", "start_tls: false")
    refused = set_sources(latet_server, tmp_path, plain, ok=False)
    print(refused.output)
    assert refused.exit_code != 0
    assert "ldaps://, or ldap:// with start_tls: names and the password cross the network (TLS)" in refused.output

    wrong = sources(directory, people_certificate="other-ca.pem", teams_certificate="other-ca.pem")
    set_sources(latet_server, tmp_path, wrong)
    keep_secrets(latet_server, directory)
    ran = latet_server("terms", "run", ok=False)
    print(ran.output)
    print(latet_server("terms", "status").output)
    found = status(cluster)
    for name in ("people", "teams"):  # LDAPS and STARTTLS alike: refused, the certificate not trusted
        last = found[name]["last"]
        print(f"{name}: {last['code']}: {last['reason']}")
        assert last["outcome"] != "ok" and found[name]["terms"] == 0, found
        assert last["code"] == "refused" and last["reason"] == UNTRUSTED, last
