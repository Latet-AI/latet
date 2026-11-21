"""scripts/test_owned_left.py against the shared PostgreSQL and Valkey: what a run's journals name and still exists is
listed beside its reports, read and never removed; what other campaigns keep there is not listed."""

import importlib.util
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ..owned import owned, place
from .conftest import _valkey_client
from .support import DATABASE, VALKEY

pytestmark = pytest.mark.server


@pytest.fixture
def lister():
    path = Path(__file__).resolve().parents[2] / "scripts" / "test_owned_left.py"
    assert path.is_file(), "the lister of what a run left is not written yet"
    spec = importlib.util.spec_from_file_location("test_owned_left_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _journal(folder: Path, *records: dict) -> None:
    folder.mkdir(exist_ok=True)
    lines = [json.dumps({"test": "tests/x.py::test_y (call)", "pid": 1, **record}) for record in records]
    (folder / "worker-1.owned.jsonl").write_text("\n".join(lines) + "\n", "utf-8")


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"environment": {"LATET_TEST_DATABASE_URL": DATABASE, "LATET_TEST_VALKEY_URL": VALKEY}}), "utf-8"
    )
    return config


def test_what_a_journal_names_and_still_exists_is_listed_and_nothing_else(lister, tmp_path, db, role_url, prefix):
    role = urlsplit(role_url()).username
    client = _valkey_client()
    try:
        client.set(prefix + "k", b"1")
    finally:
        client.close()
    gone = owned("latet_t_" + secrets.token_hex(5), "schema", DATABASE)  # named, never made
    reports = tmp_path / "reports"
    _journal(
        reports,
        {"name": db.schema, "kind": "schema", "at": place(DATABASE)},
        {"name": role, "kind": "role", "at": place(DATABASE)},
        {"name": prefix, "kind": "prefix", "at": place(VALKEY)},
        {"name": gone, "kind": "schema", "at": place(DATABASE)},
        {"name": gone, "kind": "schema", "at": "unreadable"},
        {"name": gone, "kind": "schema", "at": "127.0.0.1:1/latet"},
    )
    assert lister.main(["--output", str(reports), "--config", str(_config(tmp_path))]) == 1  # something is left
    found = json.loads((reports / "owned-left.json").read_text("utf-8"))
    assert sorted((item["object"], item["name"]) for item in found["left"]) == [
        ("keys", prefix),
        ("role", role),
        ("schema", db.schema),
    ]
    assert [item["count"] for item in found["left"] if item["object"] == "keys"] == [1]
    assert [r["name"] for r in found["unreadable"]] == [gone] and [r["name"] for r in found["unmatched"]] == [gone]
    assert found["refused"] == [] and found["unreachable"] == [] and found["complete"] is True
    assert "latet-tests-only" not in (reports / "owned-left.json").read_text("utf-8")  # the services' password
    with db.connection() as conn:  # listed, not removed: the fixtures remove them after the test
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (db.schema,)).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,)).fetchone() == (1,)
    client = _valkey_client()
    try:
        assert client.get(prefix + "k") == b"1"
    finally:
        client.close()


def test_a_journal_whose_names_are_all_gone_leaves_nothing_to_list(lister, tmp_path):
    gone = owned("latet_t_" + secrets.token_hex(5), "schema", DATABASE)
    reports = tmp_path / "reports"
    _journal(reports, {"name": gone, "kind": "schema", "at": place(DATABASE)})
    assert lister.main(["--output", str(reports), "--config", str(_config(tmp_path))]) == 0
    found = json.loads((reports / "owned-left.json").read_text("utf-8"))
    assert found["left"] == [] and found["records"] == 1 and found["complete"] is True
