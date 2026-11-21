"""The Enterprise tests' instances name their schema and their key prefix in the run's journal (``tests/owned.py``)
before the schema is made: a run stopped by force leaves nothing of theirs that its journal does not name."""

import json

import pytest

from ..owned import OWNED, place
from .servers import DATABASE, MASTER_KEY, VALKEY, Instances, services

pytestmark = [pytest.mark.server, services]


def test_the_instances_name_their_schema_and_prefix_before_the_schema_is_made(tmp_path, monkeypatch, clock):
    from latet.server import db as db_module

    journal = tmp_path / "owned.jsonl"
    monkeypatch.setenv(OWNED, str(journal))
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    seen: list[list[tuple[str, str, str]]] = []

    def migrate(db, *args, **kwargs):  # the migration that would make the schema: what the journal holds then
        lines = journal.read_text("utf-8").splitlines() if journal.exists() else []
        seen.append([(r["kind"], r["name"], r["at"]) for r in map(json.loads, lines)])

    monkeypatch.setattr(db_module, "migrate", migrate)
    opened = Instances(clock)
    try:
        assert seen == [[("schema", opened.schema, place(DATABASE)), ("prefix", opened.prefix, place(VALKEY))]]
    finally:
        opened.close()  # nothing was made: its DROP SCHEMA IF EXISTS finds none
