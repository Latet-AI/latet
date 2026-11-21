"""The names the server's fixtures make for PostgreSQL and Valkey are written in the run's journal (``tests/owned.py``)
before anything is made under them: a run stopped by force leaves nothing of theirs that its journal does not name."""

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from ..owned import OWNED, place
from .support import DATABASE, VALKEY

pytestmark = pytest.mark.server


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """The run's journal, or one of the test's own when the run names none: named before the fixtures that follow it
    among the test's arguments. A run's journal is kept, so that what these tests make is named where the run looks."""
    if named := os.environ.get(OWNED):
        return Path(named)
    path = tmp_path / "owned.jsonl"
    monkeypatch.setenv(OWNED, str(path))
    return path


def _records(path) -> list[tuple[str, str, str]]:
    if not path.exists():
        return []
    return [(r["kind"], r["name"], r["at"]) for r in map(json.loads, path.read_text("utf-8").splitlines())]


def _schema_exists(name: str) -> bool:
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        return conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,)).fetchone() is not None


def _role_exists(name: str) -> bool:
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


def test_the_schema_and_the_prefix_are_named_before_the_test_makes_anything(journal, schema, prefix):
    assert _records(journal)[-2:] == [("schema", schema, place(DATABASE)), ("prefix", prefix, place(VALKEY))]
    assert place(DATABASE) != "unreadable" and place(VALKEY) != "unreadable"
    assert not _schema_exists(schema)  # named, and nothing made under the name yet


def test_a_role_is_named_in_the_journal(journal, role_url):
    role = urlsplit(role_url()).username
    assert ("role", role, place(DATABASE)) in _records(journal)
    assert _role_exists(role)


def test_a_role_whose_name_cannot_be_written_down_is_not_made(tmp_path, monkeypatch, role_url):
    """The journal is written before ``CREATE ROLE``: when it cannot be, the role is not made, and the test stops."""
    chosen = secrets.token_hex(16)
    monkeypatch.setenv(OWNED, str(tmp_path / "missing" / "owned.jsonl"))
    monkeypatch.setattr(secrets, "token_hex", lambda size: chosen[: 2 * size])  # the role's name, known beforehand
    with pytest.raises(FileNotFoundError):
        role_url()
    monkeypatch.undo()
    with psycopg.connect(DATABASE, autocommit=True) as conn:  # the role the fixture would have made: not there
        found = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'latet_r_' || %s", (chosen[:8],))
        assert found.fetchone() == (0,)


def test_a_role_whose_setting_is_refused_after_it_is_made_is_not_left_behind(monkeypatch, role_url):
    """``role_url`` makes, grants and sets a role in one transaction (``role_catalog``): a setting PostgreSQL refuses
    after CREATE ROLE takes the role back with it, so no role is left that the fixture would not drop."""
    chosen = secrets.token_hex(16)
    monkeypatch.setattr(secrets, "token_hex", lambda size: chosen[: 2 * size])
    with pytest.raises(psycopg.errors.UndefinedObject):
        role_url(not_a_setting_of_postgresql="1")
    monkeypatch.undo()
    with psycopg.connect(DATABASE, autocommit=True) as conn:
        found = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'latet_r_' || %s", (chosen[:8],))
        assert found.fetchone() == (0,)
