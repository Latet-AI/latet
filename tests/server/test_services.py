"""PostgreSQL and Valkey answer on this computer, for the server's tests."""

import psycopg
import pytest

from .conftest import DATABASE, _valkey_client

pytestmark = pytest.mark.server


def test_postgresql_and_valkey_answer():
    with psycopg.connect(DATABASE) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    client = _valkey_client()
    try:
        assert client.ping()
    finally:
        client.close()
