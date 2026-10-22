"""PostgreSQL and Valkey on this computer, in Docker, for the server's tests (`uv run pytest -m server`), and a second
PostgreSQL that speaks TLS, which a server reaches with its certificate verified (``verify-full``).

    uv run python scripts/server-services.py up      # start them (Docker downloads the images the first time)
    uv run python scripts/server-services.py env     # the variables the tests read
    uv run python scripts/server-services.py down    # stop and remove them

Each command takes the names of the services it is for (``postgres``, ``valkey``, ``postgres-tls``), every one when it
is given none: CI's job starts the TLS PostgreSQL alone (``up postgres-tls``), the others being its service containers.

They listen on 127.0.0.1 only, on ports no usual service takes, with a password for tests only: never a server's.

The TLS PostgreSQL's certificate is made at each start, by an authority made for it alone, whose root certificate is
written where its address says (``LATET_TEST_TLS_DATABASE_URL``): in a folder of the user's own (``TLS_FOLDER``), which
nobody else can make first, never the shared temp folder, where another user could. The server's key is written on no
disk of this computer's. It goes, in base64, on docker's command line and into the container's environment, so that
whoever reads the computer's process list while `docker run` runs, or `docker inspect`, reads it: a throwaway key,
for a test database with a public password. The container's start unsets it before PostgreSQL runs.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import ipaddress
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote

PASSWORD = "latet-tests-only"  # what every service asks for, and what every address carries
IMAGES = {"postgres": "postgres:18-alpine", "valkey": "valkey/valkey:9.1-alpine", "postgres-tls": "postgres:18-alpine"}
NAMES = {"postgres": "latet-test-postgres", "valkey": "latet-test-valkey", "postgres-tls": "latet-test-postgres-tls"}
DATABASE_URL = f"postgresql://latet:{PASSWORD}@127.0.0.1:55432/latet"
VALKEY_URL = f"redis://:{PASSWORD}@127.0.0.1:56379/0"
TLS_PORT = 55433


def default_tls_folder() -> Path:
    """Where the root certificate the TLS PostgreSQL is verified with is kept: a folder of the user's own, which no
    other user can make first (``%LOCALAPPDATA%`` on Windows, ``~/.cache`` elsewhere)."""
    local = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    return (Path(local) if local else Path.home() / ".cache") / "latet-test-tls"


TLS_FOLDER = default_tls_folder()
VARIABLES = {
    "postgres": "LATET_TEST_DATABASE_URL",
    "valkey": "LATET_TEST_VALKEY_URL",
    "postgres-tls": "LATET_TEST_TLS_DATABASE_URL",
}
_READY = {
    # Over TCP, as the tests connect: while it sets itself up, PostgreSQL's image answers on its socket alone.
    "postgres": ["pg_isready", "-h", "127.0.0.1", "-U", "latet", "-d", "latet"],
    "valkey": ["valkey-cli", "-a", PASSWORD, "ping"],
    "postgres-tls": ["pg_isready", "-h", "127.0.0.1", "-U", "latet", "-d", "latet"],
}
# The TLS PostgreSQL's start: its certificate and key, handed over as base64 in its environment, written where the
# postgres user alone reads them and unset, then PostgreSQL's own entrypoint with TLS on.
_TLS_START = (
    "(umask 077 && printf '%s' \"$LATET_TLS_CERT\" | base64 -d > /tmp/server.crt"
    " && printf '%s' \"$LATET_TLS_KEY\" | base64 -d > /tmp/server.key)"
    " && chown postgres:postgres /tmp/server.crt /tmp/server.key"
    " && unset LATET_TLS_CERT LATET_TLS_KEY"
    " && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key"
)


def tls_database_url() -> str:
    """The TLS PostgreSQL's address: its certificate verified against the root certificate made for it, for its
    address's own name (``verify-full``)."""
    root = quote(str(TLS_FOLDER / "root.crt"), safe="/")
    return f"postgresql://latet:{PASSWORD}@127.0.0.1:{TLS_PORT}/latet?sslmode=verify-full&sslrootcert={root}"


def _private(folder: Path) -> Path:
    """``folder``, made for the user alone (0700) when it is not there. One that is there is taken only when it is a
    folder of the user's own that nobody else can write to: a link or a junction in its place, a file, another user's
    folder or one others can write to stops the script, saying so (whoever made it could swap the root in it)."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        folder.mkdir(mode=0o700)
    found = os.lstat(folder)
    if stat.S_ISLNK(found.st_mode) or os.path.isjunction(folder):
        sys.exit(f"{folder} is a link: the root certificate is kept in a folder of the user's own; remove it")
    if not stat.S_ISDIR(found.st_mode):
        sys.exit(f"{folder} is not a folder: the root certificate is kept in a folder of the user's own; remove it")
    if os.name != "nt":
        if found.st_uid != os.getuid():
            sys.exit(f"{folder} is another user's folder: the root certificate is kept in one of the user's own")
        if found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            sys.exit(f"{folder}: others can write to it, and could swap the root certificate in it; make it 0700")
    return folder


def _keep_root(pem: bytes) -> None:
    """The root certificate written to ``TLS_FOLDER`` (``_private``): to a new file of its own there, made with a name
    nobody can guess and that no file has, then moved into place, so that a link or another file planted where it goes
    is replaced, never followed or written through."""
    folder = _private(TLS_FOLDER)
    descriptor, written = tempfile.mkstemp(prefix=".root-", suffix=".crt", dir=folder)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(pem)
        os.replace(written, folder / "root.crt")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(written)
        raise


def certificates() -> tuple[str, str]:
    """A certificate for the TLS PostgreSQL, for 127.0.0.1 and localhost, and its key, as PEM, signed by an authority
    made for it alone, whose root certificate is written to ``TLS_FOLDER`` (``_keep_root``; the authority's own key is
    forgotten): made anew at each start, for tests only, and valid for a month."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)
    start, end = now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=30)
    authority = ec.generate_private_key(ec.SECP256R1())
    named = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Latet tests only")])
    unused = ("digital_signature", "content_commitment", "key_encipherment", "data_encipherment", "key_agreement")
    signs = x509.KeyUsage(
        key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False, **dict.fromkeys(unused, False)
    )
    root = (
        x509.CertificateBuilder()
        .subject_name(named)
        .issuer_name(named)
        .public_key(authority.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(signs, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(authority.public_key()), critical=False)
        .sign(authority, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(named)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()), critical=False)
        .sign(authority, hashes.SHA256())
    )
    _keep_root(root.public_bytes(serialization.Encoding.PEM))
    unencrypted = serialization.NoEncryption()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, unencrypted)
    return leaf.public_bytes(serialization.Encoding.PEM).decode(), pem.decode()


def docker_run(name: str, *, tls: tuple[str, str] | None = None) -> list[str]:
    """The `docker run` of one service, published on 127.0.0.1 only; the TLS PostgreSQL's with ``tls``, its
    certificate and key (``certificates``)."""
    if name == "postgres":
        return [
            "docker", "run", "-d", "--name", NAMES[name], "-p", "127.0.0.1:55432:5432",
            "-e", "POSTGRES_USER=latet", "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-e", "POSTGRES_DB=latet",
            IMAGES[name],
        ]  # fmt: skip
    if name == "postgres-tls":
        assert tls is not None, "the TLS PostgreSQL starts with its certificate and key"
        certificate, key = (base64.b64encode(pem.encode()).decode() for pem in tls)
        return [
            "docker", "run", "-d", "--name", NAMES[name], "-p", f"127.0.0.1:{TLS_PORT}:5432",
            "-e", "POSTGRES_USER=latet", "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-e", "POSTGRES_DB=latet",
            "-e", f"LATET_TLS_CERT={certificate}", "-e", f"LATET_TLS_KEY={key}",
            "--entrypoint", "sh", IMAGES[name], "-c", _TLS_START,
        ]  # fmt: skip
    return [
        "docker", "run", "-d", "--name", NAMES[name], "-p", "127.0.0.1:56379:6379", IMAGES[name],
        "valkey-server", "--requirepass", PASSWORD,
    ]  # fmt: skip


def _run(argv: list[str], *, show: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run docker. When it is not there, or ``check`` is set and it fails, say why and exit: with its own error, or with
    ``show``, which lets it print as it goes (a download takes a while), with its exit status."""
    try:
        done = subprocess.run(argv, capture_output=not show, text=True, errors="replace", check=False)
    except FileNotFoundError:
        sys.exit(f"{argv[0]} was not found: the server's tests need Docker, installed and running")
    if check and done.returncode != 0:
        said = "" if show else f": {(done.stderr or done.stdout).strip()}"
        sys.exit(f"{' '.join(argv[:2])} failed ({done.returncode}){said}")
    return done


def _running(name: str) -> bool:
    found = _run(["docker", "ps", "-q", "--filter", f"name=^{NAMES[name]}$"])
    return bool(found.stdout.strip())


def _tls_ready() -> bool:
    """Whether the TLS PostgreSQL runs with the root it is verified with in its place; False when Docker cannot be
    asked."""
    if not (TLS_FOLDER / "root.crt").is_file():
        return False
    try:
        found = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=^{NAMES['postgres-tls']}$"],
            capture_output=True, text=True, errors="replace", check=False,
        )  # fmt: skip
    except FileNotFoundError:
        return False
    return found.returncode == 0 and bool(found.stdout.strip())


def _chosen(names: Iterable[str] | None) -> list[str]:
    chosen = list(names or IMAGES)
    if unknown := [name for name in chosen if name not in IMAGES]:
        sys.exit(f"no such service: {', '.join(unknown)} (the services are {', '.join(IMAGES)})")
    return chosen


def up(names: Iterable[str] | None = None) -> None:
    chosen = _chosen(names)
    for name in chosen:
        if _running(name):
            if name != "postgres-tls" or (TLS_FOLDER / "root.crt").is_file():
                print(f"{NAMES[name]} runs already")
                continue
            # The root it is verified with is gone (its authority's key with it): a new certificate, a new root.
            print(f"{NAMES[name]} runs, but the root certificate it is verified with is gone: started again")
        _run(["docker", "rm", "-f", "-v", NAMES[name]])  # one that stopped still holds the name
        _run(docker_run(name, tls=certificates() if name == "postgres-tls" else None), show=True)
    for name in chosen:
        deadline = time.monotonic() + 60
        while _run(["docker", "exec", NAMES[name], *_READY[name]], check=False).returncode != 0:
            if time.monotonic() > deadline:
                sys.exit(f"{NAMES[name]} did not answer within a minute: `docker logs {NAMES[name]}` says why")
            time.sleep(0.5)
    print("They answer. The tests read:")
    env(chosen)


def env(names: Iterable[str] | None = None) -> None:
    """The variables of the services named; with no name, of every service, the TLS PostgreSQL's only when it runs
    with its root in place (``up postgres valkey`` alone starts none: its tests then skip, saying why, rather than
    fail on an address nothing answers at)."""
    addresses = {"postgres": DATABASE_URL, "valkey": VALKEY_URL, "postgres-tls": tls_database_url()}
    chosen = _chosen(names)
    if not names and not _tls_ready():
        chosen.remove("postgres-tls")
    for name in chosen:
        print(f"{VARIABLES[name]}={addresses[name]}")


def down(names: Iterable[str] | None = None) -> None:
    chosen = _chosen(names)
    for name in chosen:
        _run(["docker", "rm", "-f", "-v", NAMES[name]])  # -v: the volume docker made for the container goes with it
    if "postgres-tls" in chosen:
        shutil.rmtree(TLS_FOLDER, ignore_errors=True)
    print(f"{', '.join(NAMES[name] for name in chosen)}: removed.")


if __name__ == "__main__":
    commands = {"up": up, "down": down, "env": env}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(f"usage: server-services.py up|down|env [{'|'.join(IMAGES)} ...]")
    commands[sys.argv[1]](sys.argv[2:])
