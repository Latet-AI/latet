"""The engine's memory of restores (what each conversation's fakes restore to): one list kept for a conversation and a
mode, the newer one replacing the older, weighed in bytes; in an identity's space on a server bounded in bytes too, and no
list kept that is heavier than a limit (it is made again at each use, whole)."""

from latet.config import Config
from latet.engine import pipeline
from latet.engine.build import build
from latet.engine.pipeline import SPACE_BLOCK_BYTES, SPACE_CACHE_BYTES, Session
from latet.engine.types import EntityClass
from latet.engine.vault import Vault


def _conversation(values: int):
    vault = Vault(b"k" * 32, [])
    engine = build(Config(), vault).engine
    session = Session("cc:s1")
    for n in range(values):
        session.add({vault.intern("EMAIL", EntityClass.PII, f"person.number{n}@acme-corporation.example").id})
    return vault, engine, session


def test_a_conversations_older_restores_go_when_a_newer_one_is_kept():
    vault, engine, session = _conversation(50)
    for n in range(20):
        vault.intern("EMAIL", EntityClass.PII, f"newcomer{n}@acme-corporation.example")  # the vault's version moves
        engine.restores(session, json_mode=n % 2 == 0)
    assert len(engine._restores) == 2  # the latest of each mode, not one for each version
    assert engine.cached_bytes >= engine._restores.weight > 0
    again = engine.restores(session, json_mode=True)
    assert again is engine.restores(session, json_mode=True)  # kept, and found again while nothing moved


def test_a_spaces_engine_keeps_no_restores_heavier_than_its_limit_and_uses_them_whole(monkeypatch):
    _, engine, session = _conversation(200)
    monkeypatch.setattr(pipeline, "SPACE_RESTORE_LARGEST", 4096)
    engine.bound(SPACE_CACHE_BYTES, SPACE_BLOCK_BYTES)
    restores = engine.restores(session, json_mode=False)
    assert len(restores) >= 200  # whole
    assert len(engine._restores) == 0 and engine._restores.weight == 0  # heavier than the limit: not kept
