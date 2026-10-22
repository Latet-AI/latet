"""The reviewer key is read once at start and never stored with an identity."""

import pytest

from latet.config import Config, LlmReview
from latet.engine.reviewer.client import key_id
from latet.server.app import open_server
from latet.server.db import Database, migrate

from .conftest import dump, valkey_dump
from .support import DATABASE, listening

pytestmark = pytest.mark.server
KEY = "sk-server-" + "k" * 40


async def test_the_key_is_read_at_start_given_to_spaces_and_kept_out_of_stores(
    settings, schema, prefix, tmp_path, caplog, monkeypatch
):
    path = tmp_path / "reviewer.key"
    path.write_text(KEY + "\n", "utf-8")
    db = Database(DATABASE, schema=schema, max_size=1)
    try:
        migrate(db)
    finally:
        db.close()
    monkeypatch.setenv("LATET_LLM_API_KEY", "ignored-environment-secret")
    server = open_server(settings.model_copy(update={"reviewer_key_file": path}), schema=schema, prefix=prefix)
    try:
        assert server.reviewer_key == KEY and KEY not in repr(server)
        path.write_text("changed-after-start", "utf-8")
        server.shared.set(
            Config(llm_review=LlmReview(enabled=True, url="https://gpu.acme.lan/v1", remote=True, model="qwen")),
            actor="test",
        )
        identity, _ = server.identities.add_person("Emily Carter")
        async with listening(server):
            runtime = await server.spaces.runtime(identity)
            assert runtime.reviewer_key(runtime.config.llm_review) == KEY
            assert runtime.mediator.reviewer.client.key_id == key_id(KEY)
        assert KEY.encode() not in dump(server.db)
        assert KEY.encode() not in valkey_dump(server.bus, prefix)
        assert caplog.text.count("LATET_LLM_API_KEY is set:") == 1
        assert "ignored-environment-secret" not in caplog.text and KEY not in caplog.text
    finally:
        server.close()


def test_a_bad_key_file_stops_start_before_opening_stores(settings, schema, prefix, tmp_path):
    with pytest.raises(ValueError, match="reviewer_key_file"):
        open_server(
            settings.model_copy(update={"reviewer_key_file": tmp_path / "missing"}), schema=schema, prefix=prefix
        )
