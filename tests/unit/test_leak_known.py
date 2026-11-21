"""Whether Latet's vault knows a secret (``latet.leak.known``): this computer's vault and history, as Latet's own store
and daemon wrote them in the test's data folder, opened read-only, with the root key found where Latet keeps it and
never one made."""

from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import keyring.core
import pytest

from latet import wrappers
from latet.config import Config, IdentityConfig, data_dir
from latet.engine.types import EntityClass, Policy
from latet.engine.vault import Entity, Vault
from latet.gateway.app import create_app
from latet.leak import known as known_module
from latet.leak.known import (
    CONNECTED,
    NO_KEY,
    NO_VAULT,
    UNREADABLE_KEY,
    UNREADABLE_VAULT,
    NoAnswer,
    answer,
    local_known,
)
from latet.leak.scan import scan
from latet.leak.sources import Text
from latet.runtime import make_runtime
from latet.server.connection import NotConnected
from latet.storage.db import Store
from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore
from latet.storage.keys import find_root_key, load_root_key

from ..conftest import ROOT_KEY
from .conftest import MemoryKeyring
from .test_request_leaks import _Answer
from .test_request_outcomes import OPENAI

SECRET = "ghp_" + "Z8mK2pQ7rT4vW1xY6bN3cD9fG5hJ0kL2sA7e"  # a GitHub token's shape: masked by every profile
OTHER = "ghp_" + "Q4nB7vC1xZ9mL3kJ6hG2fD8sA5pO0iU7yT4r"
CLAUDE_SESSION = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"  # x-claude-code-session-id: a UUID, as Claude Code's
CODEX_SESSION = "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a41"  # Codex's session-id header: its thread's UUID
SECOND_SESSION = "7e8f9a0b-1c2d-4e3f-8a5b-6c7d8e9f0a1b"
CHAT = "/openai/v1/chat/completions"
REPLY = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'
CONFIG = Config(ner="off", identity=IdentityConfig(enabled=False))


def _kept(*values: tuple[str, str], key: bytes = ROOT_KEY) -> dict[str, Entity]:
    """``values`` (type, value as written) kept in this computer's vault by Latet's own store, sealed with ``key``."""
    store = Store(data_dir() / "latet.db", key)
    try:
        vault = Vault(key, store.load_entities(), on_insert=store.save_entity)
        return {value: vault.intern(etype, EntityClass.SECRET, value, rule="test") for etype, value in values}
    finally:
        store.close()


def _daemon(requests: list[tuple[dict[str, str], str]]) -> dict[str, str]:
    """Each request (its headers, its user's text) through this computer's daemon as `latet serve` builds it, to a
    provider that answers it: masked, sent, and recorded in the history of the data folder. The types its vault keeps
    the values under, by value."""
    rt = make_runtime(CONFIG, data=data_dir(), config_file=data_dir().parent / "config.yaml")
    try:

        def provider(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(REPLY))

        rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
        rt.gateway.fixed_routes = [OPENAI]
        app = create_app(rt.gateway)

        async def send() -> None:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://127.0.0.1:5283", trust_env=False
                ) as client:
                    for headers, text in requests:
                        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": text}]})
                        answered = await client.post(
                            CHAT, headers={"content-type": "application/json", **headers}, content=body.encode()
                        )
                        assert answered.status_code == 200, answered.text

        asyncio.run(send())
        assert rt.recorder is not None and rt.recorder.drain(10)
        return {e.original: e.etype for e in rt.built.vault.entities()}
    finally:
        rt.close()


def test_the_vault_answers_when_it_first_saw_a_value(computer):
    kept = _kept(("GITHUB_TOKEN", SECRET))
    known = local_known()
    assert known is not None
    assert known.since("GITHUB_TOKEN", SECRET) == datetime.fromtimestamp(kept[SECRET].created, UTC)
    assert known.since("GITHUB_TOKEN", OTHER) is None
    assert known.masked_in("GITHUB_TOKEN", SECRET) == {}  # no history here


def test_a_value_kept_under_another_type_is_known(computer):
    """The type of a value's first sighting is its type for good (``Vault.intern``): a value the vault keeps as a
    password is known when the scan finds it as a token."""
    kept = _kept(("PASSWORD", SECRET))
    known = local_known()
    assert known is not None
    assert known.since("GITHUB_TOKEN", SECRET) == datetime.fromtimestamp(kept[SECRET].created, UTC)


@pytest.mark.parametrize(
    ("kept", "written"),
    [
        ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYzQ8", "wJalrXUtnFEMI\\/K7MDENG\\/bPxRfiCYzQ8"),  # JSON's escaped slash
        ("Sup3r@Secret!2026x", "Sup3r%40Secret%212026x"),  # a URL's escapes
        ("p\N{LATIN SMALL LETTER A WITH DIAERESIS}ssw0rd-Zq3vR8", "p\\u00e4ssw0rd-Zq3vR8"),  # JSON's \u escape
        ("Sup3r%40Secret%212026x", "Sup3r%40Secret%212026x"),  # kept as it is written
    ],
)
def test_a_value_written_in_escapes_is_found_as_the_vault_reads_it(computer, kept, written):
    """The scan names a value as the session writes it, escapes and all; the vault finds a value it knows where a
    text's escapes read as what they stand for too (``escapes.read_escaped``): so it is known there."""
    entity = _kept(("PASSWORD", kept))[kept]
    known = local_known()
    assert known is not None
    assert known.since("PASSWORD", written) == datetime.fromtimestamp(entity.created, UTC)


@pytest.mark.parametrize("keyring_mode", ["off", "on"])
def test_no_root_key_is_created(computer, monkeypatch, keyring_mode):
    """No key anywhere: no answer, and no key made, in a file or in the keyring."""
    monkeypatch.setenv("LATET_KEYRING", keyring_mode)
    monkeypatch.delenv("LATET_ROOT_KEY", raising=False)
    monkeypatch.delenv("LATET_ROOT_KEY_FILE", raising=False)
    assert local_known() is None  # no vault either
    _kept(("GITHUB_TOKEN", SECRET), key=os.urandom(32))  # a vault whose key is nowhere now
    assert find_root_key(data_dir()) is None
    assert local_known() is None
    assert not (data_dir() / "root.key").exists()
    memory = keyring.core.get_keyring()  # the computer fixture's keyring, in memory
    assert isinstance(memory, MemoryKeyring) and memory.kept == {}  # nothing written there


@pytest.mark.parametrize("where", ["variable", "file named by a variable", "keyring", "root.key"])
def test_the_key_is_found_where_latet_keeps_it(computer, monkeypatch, tmp_path, where):
    key = os.urandom(32)
    encoded = base64.b64encode(key).decode()
    monkeypatch.delenv("LATET_ROOT_KEY", raising=False)
    monkeypatch.delenv("LATET_ROOT_KEY_FILE", raising=False)
    _kept(("GITHUB_TOKEN", SECRET), key=key)
    if where == "variable":
        monkeypatch.setenv("LATET_ROOT_KEY", encoded)
    elif where == "file named by a variable":
        (tmp_path / "secret").write_text(encoded + "\n")
        monkeypatch.setenv("LATET_ROOT_KEY_FILE", str(tmp_path / "secret"))
    elif where == "keyring":
        monkeypatch.setenv("LATET_KEYRING", "on")
        keyring.core.get_keyring().set_password("latet", "root-key", encoded)
    else:
        (data_dir() / "root.key").write_text(encoded)
    assert find_root_key(data_dir()) == key == load_root_key(data_dir())
    known = local_known()
    assert known is not None and known.since("GITHUB_TOKEN", SECRET) is not None


def test_a_vault_sealed_with_another_key_gives_no_answer(computer):
    """A key that does not open the vault (a variable of another install's, say) names none of its values: Latet cannot
    tell from here, rather than saying every secret went in clear."""
    _kept(("GITHUB_TOKEN", SECRET), key=os.urandom(32))
    assert local_known() is None


def test_a_connected_computer_has_no_local_answer(computer, monkeypatch):
    _kept(("GITHUB_TOKEN", SECRET))
    assert local_known() is not None
    monkeypatch.setattr(wrappers, "connected", lambda: object())
    assert local_known() is None  # its vault and history are the server's

    def unusable():
        raise NotConnected("this computer's connection cannot be used")

    monkeypatch.setattr(wrappers, "connected", unusable)
    assert local_known() is None


def test_the_history_names_the_sessions_a_value_was_masked_in(computer):
    """Each session the history shows a value masked in, with the first time it does: its request's."""
    before = datetime.now(UTC)
    types = _daemon(
        [
            ({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}"),
            ({"session-id": CODEX_SESSION, "user-agent": "codex_cli_rs/0.162.0"}, f"the token again: {SECRET}"),
            ({}, f"no session id here, another token {OTHER}"),  # a key of the conversation's opening alone
        ]
    )
    after = datetime.now(UTC)
    known = local_known()
    assert known is not None
    masked = known.masked_in(types[SECRET], SECRET)
    assert set(masked) == {CLAUDE_SESSION, CODEX_SESSION}
    assert before <= masked[CLAUDE_SESSION] <= masked[CODEX_SESSION] <= after  # sent in this order
    assert known.since(types[OTHER], OTHER) is not None
    assert known.masked_in(types[OTHER], OTHER) == {}  # masked, in a request no agent's session id names
    assert known.masked_in(types[SECRET], "ghp_" + "N0tK3ptAnyWh3r3" * 2 + "abcdef") == {}


def test_an_older_latets_history_names_its_sessions_too(computer):
    """A row an older Latet wrote keeps its conversation's key in clear in its own column, with no sealed copy of it (as
    tests/unit/test_request_audit.py makes one: a row recorded, its summary then written as an older Latet wrote it)."""
    entity = _kept(("GITHUB_TOKEN", SECRET))[SECRET]
    store = ExchangeStore(data_dir() / "exchanges.db", ROOT_KEY)
    try:
        before = "deploy with the token "
        masked = before + entity.surrogate
        found = {"masked_start": len(before), "masked_end": len(masked), "type": "GITHUB_TOKEN", "class": "SECRET"}
        block = BlockRecord(
            store.block_id(masked),
            "$.messages[0].content",
            "user_text",
            False,
            True,
            masked,
            [found | {"entity": entity.id}],
        )
        at = time.time() - 86400
        exchange_id = store.record(ExchangeRecord({"ts": at, "session": "s", "flags": []}, [block]))
        key = f"cc:{CLAUDE_SESSION}"
        older = json.dumps({"id": exchange_id, "ts": at, "session": key, "agent": "claude-code", "flags": []})
        store._db.execute("UPDATE exchanges SET summary = ?, session = ? WHERE id = ?", (older, key, exchange_id))
    finally:
        store.close()
    known = local_known()
    assert known is not None
    assert known.masked_in("GITHUB_TOKEN", SECRET) == {CLAUDE_SESSION: datetime.fromtimestamp(at, UTC)}


def test_a_secret_masked_in_a_session_is_replaced_and_confirmed(computer):
    """The scan with this computer's answers, on a transcript's own timeline: the agent writes the prompt down, then
    sends its request, which Latet masks and keeps the value as it does. The session Latet first saw the value in is one
    it replaced it in, confirmed by the history; a session days before, never through Latet, is one it went in clear."""
    typed = datetime.now(UTC)
    _daemon([({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}")])
    known = local_known()
    assert known is not None
    since = known.since("GITHUB_PAT", SECRET)
    assert since is not None and typed < since
    report = scan(
        [
            Text("claude", CLAUDE_SESSION, None, typed, f"deploy with the token {SECRET}"),
            Text("claude", "an-older-session", None, typed - timedelta(days=3), f"export GITHUB_TOKEN={SECRET}"),
        ],
        CONFIG,
        known=known,
    )
    [finding] = report.findings
    assert (finding.replaced.sessions, finding.replaced.confirmed, finding.in_clear.sessions) == (1, 1, 1)


def test_a_value_forgotten_and_met_again_keeps_the_sessions_the_history_names(computer):
    """Masked in a session days ago, forgotten in the vault since (by Latet's own store), then met again in another
    session: its entry is new, later than the first session, which the history still names with the time it masked the
    value there. That session is replaced and confirmed by the history, the new one by both; one never through Latet is
    in clear. (Days ago: the first request's time in the history moved back three days, its session's text with it.)"""
    typed = datetime.now(UTC)
    _daemon([({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}")])
    days = timedelta(days=3)
    with closing(sqlite3.connect(data_dir() / "exchanges.db")) as history, history:
        history.execute("UPDATE exchanges SET ts = ts - ?", (days.total_seconds(),))
    store = Store(data_dir() / "latet.db", ROOT_KEY)
    try:
        [entity] = [e for e in store.load_entities() if e.original == SECRET]
        store.forget_entity(entity.id)
    finally:
        store.close()
    again = datetime.now(UTC)
    _daemon([({"x-claude-code-session-id": SECOND_SESSION}, f"the token once more {SECRET}")])
    known = local_known()
    assert known is not None
    since = known.since("GITHUB_PAT", SECRET)
    assert since is not None and since > datetime.fromtimestamp(entity.created, UTC)
    report = scan(
        [
            Text("claude", CLAUDE_SESSION, None, typed - days, f"deploy with the token {SECRET}"),
            Text("claude", SECOND_SESSION, None, again, f"the token once more {SECRET}"),
            Text("claude", "never-through-latet", None, again - timedelta(days=5), f"token {SECRET}"),
        ],
        CONFIG,
        known=known,
    )
    [finding] = report.findings
    assert (finding.replaced.sessions, finding.replaced.confirmed, finding.in_clear.sessions) == (2, 2, 1)


def test_no_private_copy_is_left_behind(computer, monkeypatch, tmp_path):
    """The private copies of the vault and of the history are deleted once read (``tempfile.tempdir`` pointed at a
    folder of the test's own)."""
    types = _daemon([({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}")])
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    known = local_known()
    assert known is not None and list(temp.iterdir()) == []
    assert set(known.masked_in(types[SECRET], SECRET)) == {CLAUDE_SESSION}
    assert known.latet_since() is not None
    assert list(temp.iterdir()) == []


def test_a_history_in_a_later_format_never_stops_the_scan(computer):
    """References in a form this Latet does not know (a later Latet's) name nothing: they never raise."""
    entity = _kept(("GITHUB_TOKEN", SECRET))[SECRET]
    store = ExchangeStore(data_dir() / "exchanges.db", ROOT_KEY)
    try:
        masked = f"token {entity.surrogate}"
        found = {"masked_start": 6, "masked_end": len(masked), "type": "GITHUB_TOKEN", "class": "SECRET"}
        block = BlockRecord(
            store.block_id(masked), "$", "user_text", False, True, masked, [found | {"entity": entity.id}]
        )
        exchange_id = store.record(ExchangeRecord({"ts": time.time(), "session": "s", "flags": []}, [block]))
        later = store._seal([{"block": block.id, "path": "$"}], f"{exchange_id}:refs")
        store._db.execute("UPDATE exchanges SET refs = ? WHERE id = ?", (later, exchange_id))
    finally:
        store.close()
    known = local_known()
    assert known is not None
    assert known.masked_in("GITHUB_TOKEN", SECRET) == {}


def test_the_store_is_opened_read_only(computer):
    """Nothing is written in the data folder: no file changed, none made beside the databases (their -wal, -shm)."""
    types = _daemon([({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}")])

    def folder() -> dict[str, tuple[int, int]]:
        return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in Path(data_dir()).iterdir() if p.is_file()}

    before = folder()
    assert "latet.db" in before and "exchanges.db" in before
    known = local_known()
    assert known is not None
    assert known.since(types[SECRET], SECRET) is not None
    assert set(known.masked_in(types[SECRET], SECRET)) == {CLAUDE_SESSION}
    assert known.latet_since() is not None
    assert folder() == before


def test_a_time_no_date_can_be_made_of_is_left_out(computer):
    """A damaged row's time (one past any date) names nothing and never stops the scan."""
    types = _daemon([({"x-claude-code-session-id": CLAUDE_SESSION}, f"deploy with the token {SECRET}")])
    with closing(sqlite3.connect(data_dir() / "exchanges.db")) as history, history:
        history.execute("UPDATE exchanges SET ts = 1e300")
    with closing(sqlite3.connect(data_dir() / "latet.db")) as vault, vault:
        vault.execute("UPDATE entities SET created = 1e300")
    known = local_known()
    assert known is not None
    assert known.since(types[SECRET], SECRET) is None and known.masked_in(types[SECRET], SECRET) == {}
    assert known.latet_since() is None


def test_latet_since_is_its_oldest_vault_entry_or_history_request(computer):
    """The earliest date Latet has on this computer: its vault's oldest entry or its history's oldest request, whichever
    is older; None where it has neither."""
    Store(data_dir() / "latet.db", ROOT_KEY).close()  # a vault with nothing in it
    known = local_known()
    assert known is not None and known.latet_since() is None and known.history_horizon() is None
    kept = _kept(("GITHUB_TOKEN", SECRET))[SECRET]
    known = local_known()
    assert known is not None and known.latet_since() == datetime.fromtimestamp(kept.created, UTC)
    assert known.history_horizon() is None  # no history yet
    history = ExchangeStore(data_dir() / "exchanges.db", ROOT_KEY)
    try:
        old = time.time() - 3 * 86400
        history.record(ExchangeRecord({"ts": old, "session": "s", "flags": []}))
    finally:
        history.close()
    known = local_known()
    assert known is not None and known.latet_since() == datetime.fromtimestamp(old, UTC)
    assert known.history_horizon() == datetime.fromtimestamp(old, UTC)  # the history's oldest request still kept
    history = ExchangeStore(data_dir() / "exchanges.db", ROOT_KEY)
    try:
        history.record(ExchangeRecord({"ts": time.time(), "session": "s", "flags": []}))
    finally:
        history.close()
    known = local_known()
    assert known is not None and known.history_horizon() == datetime.fromtimestamp(old, UTC)


def test_why_latet_gives_no_answer_here_is_said(computer, monkeypatch):
    """Each reason the vault and the history give no answer, for the command to say: no vault, a connection to a Latet
    server, no key found that opens the vault, a key or a vault that could not be read (with what stopped the read)."""
    assert answer()[1] == NoAnswer(NO_VAULT)
    _kept(("GITHUB_TOKEN", SECRET))
    found, unknown = answer()
    assert found is not None and unknown is None
    monkeypatch.setattr(wrappers, "connected", lambda: object())
    assert answer() == (None, NoAnswer(CONNECTED))
    monkeypatch.setattr(wrappers, "connected", lambda: None)
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(os.urandom(32)).decode())
    assert answer() == (None, NoAnswer(NO_KEY))  # a key of another install's
    monkeypatch.setenv("LATET_ROOT_KEY", "abc")
    assert answer() == (None, NoAnswer(UNREADABLE_KEY, "it is not in base64"))
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    assert answer()[1] is None

    def no_room(path):
        raise OSError(errno.ENOSPC, "not enough free space for a private copy")

    monkeypatch.setattr(known_module, "connect_read_only", no_room)
    assert answer() == (None, NoAnswer(UNREADABLE_VAULT, "not enough free space for a private copy"))


def test_a_vault_that_is_no_database_is_said(computer):
    (data_dir() / "latet.db").parent.mkdir(parents=True, exist_ok=True)
    (data_dir() / "latet.db").write_bytes(b"not a database, " * 64)
    assert answer() == (None, NoAnswer(UNREADABLE_VAULT, "file is not a database"))


REDACTING = Config(ner="off", identity=IdentityConfig(enabled=False), policies={"GITHUB_PAT": Policy.REDACT})


def test_with_no_vault_and_no_server_latet_masked_nothing_here(computer, monkeypatch):
    """No vault on this computer, and no Latet server: Latet masked nothing here, so a secret of a type the settings
    redact is in clear in every session, as every other is. Connected to a server, whose vault and history are not
    here, Latet may have redacted it: those sessions stay redacted."""
    texts = [
        Text("claude", "dated", None, datetime.now(UTC) - timedelta(days=2), f"export GITHUB_TOKEN={SECRET}"),
        Text("claude", "now", None, datetime.now(UTC), f"token {SECRET}"),
        Text("claude", "timeless", None, None, f"again {SECRET}"),
    ]
    found, unknown = answer()
    assert unknown == NoAnswer(NO_VAULT) and found is not None
    [finding] = scan(texts, REDACTING, known=found).findings
    assert (finding.in_clear.sessions, finding.replaced.sessions, finding.redacted.sessions) == (3, 0, 0)
    monkeypatch.setattr(wrappers, "connected", lambda: object())
    found, unknown = answer()
    assert (found, unknown) == (None, NoAnswer(CONNECTED))
    [finding] = scan(texts, REDACTING, known=found).findings
    assert (finding.in_clear.sessions, finding.redacted.sessions) == (0, 3)
