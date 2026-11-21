"""scripts/test_owned_left.py: what still exists under the names a run's journals hold (``tests/owned.py``), read and
never removed. Only an exact name, or that name followed by ``_`` (a schema's decoy) or by a key under its prefix,
belongs to a record: never a name that LIKE or a glob would also take, nor another campaign's."""

import importlib.util
import json
from pathlib import Path

import pytest

SCHEMA = "latet_t_abcdef0123"
ROLE = "latet_r_0a1b2c3d"
DATABASE_NAME = "latet_t_0123456789"
PREFIX = "latet_t_9876543210:"
USER = "latet_t_0123456789abcdef0123456789abcdef"
PG = "127.0.0.1:55432/latet"
TLS = "127.0.0.1:55433/latet"
VALKEY = "127.0.0.1:56379/0"


@pytest.fixture
def lister():
    path = Path(__file__).resolve().parents[2] / "scripts" / "test_owned_left.py"
    assert path.is_file(), "the lister of what a run left is not written yet"
    spec = importlib.util.spec_from_file_location("test_owned_left_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(name, kind, at=PG):
    return {"name": name, "kind": kind, "at": at, "test": "tests/x.py::test_y (setup)", "pid": 1}


def test_an_object_is_under_a_record_by_its_exact_name_or_after_an_underscore(lister):
    assert (
        lister.under(SCHEMA, SCHEMA) and lister.under(SCHEMA + "_decoy", SCHEMA) and lister.under(SCHEMA + "_b", SCHEMA)
    )
    assert not lister.under(SCHEMA + "1", SCHEMA)  # another name that begins the same
    assert not lister.under("latetxt_abcdef0123", SCHEMA)  # what LIKE 'latet_t_abcdef0123%' would also take
    assert not lister.under("latet_t_keyfuzz_pi0102_99700a327719_0", SCHEMA)


def test_postgres_objects_left_are_those_under_a_record_and_no_others(lister):
    records = [_record(SCHEMA, "schema"), _record(ROLE, "role"), _record(DATABASE_NAME, "database")]
    schemas = [
        SCHEMA,
        SCHEMA + "_decoy",
        "latetxt_abcdef0123",
        SCHEMA + "1",
        "latet_t_keyfuzz_pi0102_99700a327719_0",  # another campaign's
        "t_0b176b5c0ada",  # of no journal
        "public",
    ]
    roles = [ROLE, SCHEMA, "latet", "latet_r_0a1b2c3e"]  # a role named like its schema is the schema's test's
    left = lister.postgres_left(records, schemas=schemas, roles=roles, databases=["latet", DATABASE_NAME, "postgres"])
    assert sorted((item["object"], item["name"], item["record"]) for item in left) == [
        ("database", DATABASE_NAME, DATABASE_NAME),
        ("role", ROLE, ROLE),
        ("role", SCHEMA, SCHEMA),
        ("schema", SCHEMA, SCHEMA),
        ("schema", SCHEMA + "_decoy", SCHEMA),
    ]


def test_valkey_keys_and_users_left_are_those_under_a_record_and_no_others(lister):
    records = [_record(PREFIX, "prefix", VALKEY), _record(USER, "valkey-user", VALKEY)]
    keys = [
        (PREFIX + "a").encode(),
        (PREFIX + "x*[a]?:b").encode(),  # a prefix's own prefix, as test_bus makes one
        b"latetxt_9876543210:k",  # what a glob with an unescaped name would also take
        b"latet_t_98765432101:k",
        (USER + ":k").encode(),
        b"ta61ec38f:k",  # of no journal
    ]
    left = lister.valkey_left(records, keys=keys, users=["default", USER, USER + "x"])
    assert sorted((item["object"], item["name"], item.get("count")) for item in left) == [
        ("keys", USER + ":", 1),
        ("keys", PREFIX, 2),
        ("valkey-user", USER, None),
    ]
    assert all("x*[a]" not in json.dumps(item) for item in left)  # counted, the keys themselves never written


def test_records_that_cannot_be_matched_are_set_apart_and_never_matched(lister):
    records = [
        _record(SCHEMA, "schema"),
        _record("latet_t_keyfuzz_x", "schema"),  # a shape no test makes: refused, never a prefix to match
        _record("", "schema"),
        _record(SCHEMA, "table"),
        _record(ROLE, "role", "unreadable"),
        _record(ROLE, "role", "127.0.0.1:1/latet"),  # a place the configuration does not name
        {"name": SCHEMA},  # a record cut short
    ]
    sorted_out = lister.sort_out(records, places={PG: "database", TLS: "database", VALKEY: "valkey"})
    assert [r["name"] for r in sorted_out["by_place"][PG]] == [SCHEMA]
    assert [r.get("name") for r in sorted_out["refused"]] == ["latet_t_keyfuzz_x", "", SCHEMA, SCHEMA]
    assert [r["name"] for r in sorted_out["unreadable"]] == [ROLE]
    assert [r["name"] for r in sorted_out["unmatched"]] == [ROLE]


def test_a_postgres_record_at_a_valkey_place_is_refused(lister):
    sorted_out = lister.sort_out([_record(SCHEMA, "schema", VALKEY)], places={PG: "database", VALKEY: "valkey"})
    assert sorted_out["by_place"] == {} and [r["name"] for r in sorted_out["refused"]] == [SCHEMA]


def test_every_journal_of_a_run_is_read_and_a_line_that_is_not_a_record_is_refused(lister, tmp_path):
    (tmp_path / "worker-1.owned.jsonl").write_text(json.dumps(_record(SCHEMA, "schema")) + "\n", "utf-8")
    (tmp_path / "quiet.owned.jsonl").write_text("not json\n" + json.dumps(_record(ROLE, "role")) + "\n", "utf-8")
    (tmp_path / "summary.json").write_text("{}", "utf-8")
    records = lister.journals(tmp_path)
    assert [(r.get("name"), r.get("line"), r["journal"]) for r in records] == [
        (None, 1, "quiet.owned.jsonl"),  # matched against nothing: sort_out refuses it
        (ROLE, None, "quiet.owned.jsonl"),
        (SCHEMA, None, "worker-1.owned.jsonl"),
    ]
    assert lister.sort_out(records[:1], places={PG: "database"})["refused"] == records[:1]


def test_a_directory_without_journals_is_not_said_to_be_clean(lister, tmp_path):
    """A wrong ``--output`` (or a run whose children never started) must not read as a run that left nothing."""
    (tmp_path / "summary.json").write_text("{}", "utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"environment": {}}), "utf-8")
    assert lister.main(["--output", str(tmp_path), "--config", str(config)]) == 1
    found = json.loads((tmp_path / "owned-left.json").read_text("utf-8"))
    assert found["journals"] == [] and found["left"] == [] and found["complete"] is False
