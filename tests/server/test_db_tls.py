"""A server's connection to a PostgreSQL that speaks TLS, its certificate verified as the address says (``verify-full``:
signed by the authority the address names, for the address's own host), whatever the computer's libpq environment
holds: `scripts/server-services.py up` starts one, and CI's job (LATET_TEST_TLS_DATABASE_URL)."""

import datetime
import os
import secrets
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from latet.server.db import Database, DatabaseError, migrate

from .conftest import _HERE, unmet
from .libpq_environment import hostile

pytestmark = pytest.mark.server

TLS_DATABASE = os.environ.get("LATET_TEST_TLS_DATABASE_URL", "")


@pytest.fixture
def tls_database():
    """The TLS PostgreSQL's address, on this computer alone (as the guard has it for the others), and a schema of the
    test's own there, dropped after it."""
    if not TLS_DATABASE:
        unmet("LATET_TEST_TLS_DATABASE_URL not set: `uv run python scripts/server-services.py up` starts a PostgreSQL "
              "that speaks TLS, and `env` prints the variable")  # fmt: skip
    found = conninfo_to_dict(TLS_DATABASE)
    if found.get("host") not in _HERE or found.get("hostaddr") or found.get("sslmode") != "verify-full":
        unmet("LATET_TEST_TLS_DATABASE_URL names no PostgreSQL on 127.0.0.1 or ::1 to verify in full")
    schema = "latet_t_" + secrets.token_hex(5)
    yield TLS_DATABASE, schema
    with psycopg.connect(TLS_DATABASE, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _another_authority(folder) -> str:
    """The root certificate of an authority that signed nothing of the server's, written in ``folder``: its path."""
    key = ec.generate_private_key(ec.SECP256R1())
    named = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Not the tests' authority")])
    now = datetime.datetime.now(datetime.UTC)
    root = (
        x509.CertificateBuilder().subject_name(named).issuer_name(named).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256())
    )  # fmt: skip
    path = folder / "another-root.crt"
    path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_a_verified_tls_connection_is_made_as_the_address_says_whatever_the_environment_holds(
    tls_database, tmp_path, monkeypatch
):
    """Every variable libpq reads for an option is set wrong (PGSSLMODE=disable, another PGSSLROOTCERT, PGSSLCERT and
    PGSSLKEY...; the two that are refused apart), and the server's connection verifies the certificate in full."""
    address, schema = tls_database
    for variable, value in hostile(tmp_path).items():
        if variable not in ("PGSERVICE", "PGREQUIREAUTH"):
            monkeypatch.setenv(variable, value)
    db = Database(address, schema=schema)
    try:
        assert migrate(db)[0] == "latet.server.migrations:001_core"
        with db.connection() as conn:
            assert conn.pgconn.ssl_in_use
            assert conn.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()").fetchone() == (True,)
            assert conn.execute("SHOW search_path").fetchone() == (schema,)
    finally:
        db.close()


def _where(address: str, **query: str) -> str:
    """``address`` with ``query`` laid over its own query."""
    parts = urlsplit(address)
    given = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    return parts._replace(query=urlencode({**given, **query}, quote_via=quote)).geturl()


def test_a_certificate_for_another_name_is_refused_as_verify_full_checks_the_name(tls_database):
    """verify-full checks the certificate's name, which stops whoever holds a certificate from the same authority for
    another name: the same PostgreSQL, reached at its address under a name its certificate does not carry, is refused,
    said by its type; asked with verify-ca, which checks the authority alone, it answers, so that the refusal was the
    name's."""
    address, schema = tls_database
    password = urlsplit(address).password
    elsewhere = Database(_where(address, host="db.invalid", hostaddr="127.0.0.1"), schema=schema, connect_timeout=5)
    try:
        with pytest.raises(DatabaseError) as refused:
            migrate(elsewhere)
        assert password and password not in str(refused.value)
    finally:
        elsewhere.close()
    authority_alone = Database(
        _where(address, host="db.invalid", hostaddr="127.0.0.1", sslmode="verify-ca"), schema=schema, connect_timeout=5
    )
    try:
        assert migrate(authority_alone)[0] == "latet.server.migrations:001_core"
    finally:
        authority_alone.close()


def test_a_certificate_signed_by_another_authority_is_refused_and_said_by_its_type(tls_database, tmp_path):
    address, schema = tls_database
    parts = urlsplit(address)
    another = parts._replace(query=f"sslmode=verify-full&sslrootcert={quote(_another_authority(tmp_path), safe='/')}")
    db = Database(another.geturl(), schema=schema, connect_timeout=5)
    try:
        with pytest.raises(DatabaseError) as refused:
            migrate(db)
        assert parts.password and parts.password not in str(refused.value)
    finally:
        db.close()
