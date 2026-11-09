"""P2-5 source-bound privacy witnesses and sensitive-retention controls.

New consumer witnesses stay separate from established sink checks. Results bind
immutable source/harness manifests and are never inferred from setup alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from latet.engine.masker import Masker
from latet.engine.pipeline import Engine
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, Vault
from latet.engine.verifier import Verifier
from latet.gateway.cloud import _AccessRedactor
from latet.gateway.mediate import VERIFY, Mediator, Prepared
from latet.gateway.outbound import server_calls, stand_ins
from latet.server.logs import RequestLog

from .. import thread_pools

ROOT_KEY = b"\x07" * 32
TOOL = "P25_PRIVATE_REMOTE_TOOL_61937"
FAKE = "Niccol\N{LATIN SMALL LETTER O WITH GRAVE} Ferri"
ORIGINAL = "Mario Rossi"
CASES = ("anthropic-server", "anthropic-mcp", "responses-mcp", "gemini-name", "gemini-type")


@pytest.fixture
def outbound_owner():
    """Real vault/masker/session and mediator; no detectors, providers or model I/O."""
    entity = Entity("e1", "PERSON", EntityClass.NER, ORIGINAL, FAKE, 0, 0.0, "ner")
    vault = Vault(ROOT_KEY, [entity])
    masker = Masker(vault, [])
    mediator = Mediator(Engine(vault, masker), Verifier(masker))
    try:
        yield mediator
    finally:
        mediator.close()


def _document(case, value=FAKE, name=TOOL):
    named = {} if name is None else {"name": name}
    if case.startswith("anthropic"):
        kind = "server_tool_use" if case == "anthropic-server" else "mcp_tool_use"
        call = {"type": kind, **named, "input": {"who": value}}
        return "anthropic", {"messages": [{"role": "assistant", "content": [call]}]}
    if case == "responses-websearch":
        call = {"type": "web_search_call", "action": {"type": "search", "query": value}}
        return "responses", {"input": [call]}
    if case == "responses-mcp":
        call = {"type": "mcp_call", **named, "arguments": json.dumps({"who": value})}
        return "responses", {"input": [call]}
    key = "toolName" if case == "gemini-name" else "toolType"
    call = {"args": {"who": value}}
    if name is not None:
        call[key] = name
    return "gemini", {"contents": [{"role": "model", "parts": [{"toolCall": call}]}]}


def _prepared(mediator, case, *, value=FAKE, name=TOOL, present=True, off=False):
    protocol, document = _document(case, value, name)
    session = mediator.engine.session("p25-owned-session") if present else None
    return Prepared(
        VERIFY,
        protocol,
        json.dumps(document).encode(),
        session=session,
        document=document,
        off=off,
    )


@pytest.mark.parametrize("case", CASES)
def test_provider_tool_warning_contains_only_safe_categories(outbound_owner, caplog, case):
    """Catches raw Anthropic/Responses/Gemini names interpolated into ordinary warnings."""
    mediator = outbound_owner
    prepared = _prepared(mediator, case)
    [(name, refs)] = list(server_calls(prepared.protocol, prepared.document))
    assert name == TOOL
    assert stand_ins(mediator._masker(prepared.session), [ref.get() for ref in refs]) == ["PERSON"]
    with caplog.at_level(logging.WARNING, logger="latet.gateway.mediate"):
        assert mediator._mark_outbound(prepared) is prepared
        repeated = _prepared(mediator, case)
        mediator._mark_outbound(repeated)
    assert prepared.outbound_slots, "the actual stand-in producer must be reached"
    assert prepared.outbound == [TOOL], "sensitive discovery retains its tool identity"
    assert repeated.outbound == [] and repeated.outbound_slots == [], "repeat must remain deduplicated"
    warnings = [record.getMessage() for record in caplog.records if record.name == "latet.gateway.mediate"]
    assert len(warnings) == 1, "one actual provider warning, not an early coverage refusal"
    assert TOOL not in warnings[0], "raw provider tool name escaped into an ordinary warning"
    assert FAKE not in warnings[0] and ORIGINAL not in warnings[0]


@pytest.mark.parametrize("guard", ["absent-session", "off", "original", "clean"])
def test_provider_tool_warning_guards_remain_effective(outbound_owner, caplog, guard):
    """Catches warning/audit work taking place when the original producer should do nothing."""
    mediator = outbound_owner
    value = ORIGINAL if guard == "original" else ("ordinary input" if guard == "clean" else FAKE)
    prepared = _prepared(
        mediator,
        "anthropic-server",
        value=value,
        present=guard != "absent-session",
        off=guard == "off",
    )
    assert len(list(server_calls(prepared.protocol, prepared.document))) == 1
    with caplog.at_level(logging.WARNING, logger="latet.gateway.mediate"):
        mediator._mark_outbound(prepared)
    assert prepared.outbound == [] and prepared.outbound_slots == []
    assert not [record for record in caplog.records if record.name == "latet.gateway.mediate"]


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("anthropic-server", "server_tool_use"),
        ("anthropic-mcp", "mcp_tool_use"),
        ("responses-mcp", "mcp"),
        ("gemini-name", "tool"),
        ("responses-websearch", "web_search"),
    ],
)
def test_provider_tool_fixed_fallbacks_remain_reachable(outbound_owner, caplog, case, expected):
    """Catches categorization accidentally dropping calls with the existing fixed fallback."""
    mediator = outbound_owner
    prepared = _prepared(mediator, case, name=None)
    [(name, refs)] = list(server_calls(prepared.protocol, prepared.document))
    assert name == expected
    assert stand_ins(mediator._masker(prepared.session), [ref.get() for ref in refs]) == ["PERSON"]
    with caplog.at_level(logging.WARNING, logger="latet.gateway.mediate"):
        mediator._mark_outbound(prepared)
    assert prepared.outbound_slots and prepared.outbound == [expected]


@pytest.mark.parametrize("escaped", [False, True], ids=("plain", "escaped"))
def test_preapp_request_log_never_copies_a_raw_target(caplog, escaped):
    """Catches target disclosure before any gateway routing/authentication occurs."""
    marker = "P25_PRIVATE_TARGET_28361"
    raw = ("/u/x/openai/v1/" + marker).encode()
    if escaped:
        raw = raw.replace(b"P25", b"%50%32%35")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/u/x/openai/v1/" + marker,
        "raw_path": raw,
        "query_string": b"note=P25_PRIVATE_QUERY_78314",
        "headers": [(b"x-private", b"P25_PRIVATE_HEADER_72315")],
    }
    messages = []

    async def app(given, receive, send):
        assert given is scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"P25_PRIVATE_BODY_28731", "more_body": False}

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.INFO, logger="latet.server.requests"):
        asyncio.run(RequestLog(app)(scope, receive, send))
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    records = [record for record in caplog.records if record.name == "latet.server.requests"]
    assert len(records) == 1 and records[0].status == 200
    line = records[0].getMessage() + json.dumps({"path": records[0].path, "method": records[0].method})
    for omitted in ("P25_PRIVATE_QUERY_78314", "P25_PRIVATE_HEADER_72315", "P25_PRIVATE_BODY_28731"):
        assert omitted not in line
    assert marker not in line, "outer RequestLog retained a client-controlled path segment"


def test_local_access_log_never_copies_a_raw_target():
    """Catches noncredential query/path text surviving the pre-app uvicorn filter."""
    marker = "P25_PRIVATE_ACCESS_78361"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", "/openai/" + marker + "?note=" + marker, "1.1", 200),
        None,
    )
    assert _AccessRedactor().filter(record)
    rendered = record.getMessage()
    assert "200" in rendered, "filter must leave the actual access line usable"
    assert marker not in rendered, "local access filtering retained arbitrary target text"


# These present-consumer witnesses are added before changing Recorder/legacy readers.
def _recorded_metadata(tmp_path, mediator, history):
    from latet.gateway.recorder import Outcome, Recorder
    from latet.storage.exchanges import ExchangeStore

    store = ExchangeStore(tmp_path / (history + ".db"), ROOT_KEY)
    recorder = Recorder(store, engine=mediator.engine, history=history)
    try:
        prepared = _prepared(mediator, "anthropic-server", value=FAKE + " P25_PRIVATE_BODY_61932")
        mediator._mark_outbound(prepared)
        assert prepared.outbound_slots and prepared.outbound == [TOOL]
        prepared.model = "P25_PRIVATE_MODEL_29163"
        prepared.block_reason = "P25_PRIVATE_REASON_61379"
        recorder.finish(
            Outcome(
                {},
                "P25_PRIVATE_UPSTREAM_73619",
                "/P25_PRIVATE_PATH_59136",
                "http",
                1_800_000_000.0,
                prepared,
                400,
            )
        )
        assert recorder.drain(timeout=5), "actual recorder worker must end"
        return store
    except BaseException:
        recorder.close()
        store.close()
        raise
    finally:
        recorder.close()


def test_metadata_history_contains_no_sensitive_source_fields(tmp_path, outbound_owner):
    """Catches raw model/reason/tool/path/upstream in an actually persisted metadata row."""
    store = _recorded_metadata(tmp_path, outbound_owner, "metadata")
    try:
        rows, cursor = store.list()
        assert len(rows) == 1 and cursor is None, "privacy cannot pass by dropping the record"
        detail = store.detail(rows[0]["id"])
        assert detail is not None and detail["request"]["slots"] == []
        assert detail["sensitive"] is None
        assert store._db.execute("SELECT refs FROM exchanges").fetchone()[0] is None
        [(clear_flags,)] = store._db.execute("SELECT flags FROM exchanges").fetchall()
        visible = json.dumps(rows) + json.dumps(detail["leaks"]) + clear_flags
        assert "P25_PRIVATE_" not in visible, "safe metadata copied sensitive source fields"
        assert FAKE not in visible and ORIGINAL not in visible
    finally:
        store.close()


def test_full_history_retains_its_authorized_sensitive_detail(tmp_path, outbound_owner):
    """Controls against fixing safe metadata by deleting separately authorized full history."""
    store = _recorded_metadata(tmp_path, outbound_owner, "full")
    try:
        [summary] = store.list()[0]
        detail = store.detail(summary["id"])
        assert detail is not None
        assert "P25_PRIVATE_BODY_61932" in json.dumps(detail["request"]["slots"])
        assert "P25_PRIVATE_" not in json.dumps(summary), "full history still has a safe list view"
        assert "P25_PRIVATE_" not in json.dumps(detail["summary"]), "sensitive detail is not a safe summary"
        private = detail.get("sensitive")
        assert private is not None, "full history preserves explicit separately sealed private detail"
        assert private.get("model") == "P25_PRIVATE_MODEL_29163"
        assert private.get("block_reason") == "P25_PRIVATE_REASON_61379"
        assert TOOL in private.get("outbound", [])
    finally:
        store.close()


def test_history_off_does_not_persist_a_separate_record(tmp_path, outbound_owner):
    """Preserves off-history storage while new process status is implemented later."""
    store = _recorded_metadata(tmp_path, outbound_owner, "off")
    try:
        assert store.list() == ([], None)
        assert store._db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("reader", ["list", "detail", "since", "parts"])
def test_legacy_history_is_readable_with_coverage_unknown(tmp_path, reader):
    """Catches a historical verified flag being presented as new complete coverage."""
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "legacy.db", ROOT_KEY)
    try:
        now = 1_800_000_000.0
        exchange_id = store.record(ExchangeRecord({"ts": now, "session": "legacy-session", "flags": []}))
        # A genuine historical row, independent of any future writer's projection.
        historical = json.dumps(
            {
                "id": exchange_id,
                "ts": now,
                "session": "legacy-session",
                "agent": "other",
                "flags": [],
                "mode": "verify",
                "masked": 0,
                "new_masked": 0,
                "verified": True,
                "model": "legacy-sensitive-model",
            }
        )
        store._db.execute(
            "UPDATE exchanges SET summary = ?, session = ?, agent = ?, flags = ? WHERE id = ?",
            (historical, "legacy-session", "other", "  ", exchange_id),
        )
        before = store._db.execute("SELECT summary, session, agent, flags FROM exchanges").fetchone()
        assert before == (historical, "legacy-session", "other", "  ")
        assert json.loads(before[0])["verified"] is True
        assert json.loads(before[0])["mode"] == "verify" and "legacy-sensitive-model" in before[0]
        if reader == "list":
            [summary] = store.list()[0]
        elif reader == "detail":
            detail = store.detail(exchange_id)
            assert detail is not None
            summary = detail["summary"]
        elif reader == "since":
            [summary] = store.summaries_since(now - 1)
        else:
            [[summary]] = list(store.summaries_parts(now - 1, 10))
        assert summary["id"] == exchange_id
        assert summary.get("coverage") == "unknown", "old mode/masked count is not coverage evidence"
        assert summary.get("verified") is False
        assert "legacy-sensitive-model" not in json.dumps(summary)
        after = store._db.execute("SELECT summary, session, agent, flags FROM exchanges").fetchone()
        assert after == before, "reading old history must not rewrite/backfill its stored bytes"
    finally:
        store.close()


def test_opt_in_capture_preserves_sensitive_content_and_stays_off_by_default(tmp_path):
    """Controls against classifying intentional sensitive capture as a safe audit sink."""
    from latet.gateway.capture import Capture

    assert Capture(None).exchange("http", "openai", "POST", "http://127.0.0.1/x") is None
    folder = tmp_path / "capture"
    exchange = Capture(folder).exchange(
        "http",
        "openai",
        "POST",
        "http://127.0.0.1/P25_PRIVATE_CAPTURE_PATH_27219?key=credential-canary",
    )
    assert exchange is not None
    exchange.request([("authorization", "Bearer credential-canary")], b"P25_PRIVATE_CAPTURE_BODY_71623", None)
    exchange.response_end()
    [path] = folder.glob("*.jsonl")
    raw = path.read_text("utf-8")
    assert "P25_PRIVATE_CAPTURE_BODY_71623" in raw and "P25_PRIVATE_CAPTURE_PATH_27219" in raw
    assert "credential-canary" not in raw, "sensitive capture still preserves credential filtering"


@pytest.mark.parametrize("width", [4, 5, 6])
def test_historical_list_refs_keep_blocks_sources_and_sealing(tmp_path, width):
    """Old four/five/six-part refs still open without changing their sealed row."""
    from cryptography.exceptions import InvalidTag

    from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore, block_ref

    store = ExchangeStore(tmp_path / "old-refs.db", ROOT_KEY)
    try:
        block = BlockRecord(
            store.block_id("authorized masked block"),
            "$.messages[0]",
            "user_text",
            False,
            True,
            "authorized masked block",
            [],
            tool="private-tool",
            source="private-source",
        )
        exchange_id = store.record(
            ExchangeRecord(
                {"ts": 1_800_000_000.0, "session": "owned", "flags": []},
                [block],
            )
        )
        old_refs = [block_ref(block)[:width]]
        sealed = store._seal(old_refs, f"{exchange_id}:refs")
        private_session = "P25_HISTORICAL_PRIVATE_SESSION_731"
        private_model = "P25_HISTORICAL_PRIVATE_MODEL_732"
        private_path = "/P25_HISTORICAL_PRIVATE_PATH_733"
        historical = {
            "id": exchange_id,
            "ts": 1_800_000_000.0,
            "session": private_session,
            "model": private_model,
            "path": private_path,
            "mode": "verify",
            "verified": True,
            "flags": [],
        }
        raw_summary = json.dumps(historical)
        store._db.execute(
            "UPDATE exchanges SET summary = ?, session = ?, refs = ? WHERE id = ?",
            (raw_summary, private_session, sealed, exchange_id),
        )
        before = store._db.execute(
            "SELECT summary, session, refs FROM exchanges WHERE id = ?", (exchange_id,)
        ).fetchone()
        assert before == (raw_summary, private_session, sealed)
        assert all(canary in before[0] for canary in (private_session, private_model, private_path))
        detail = store.detail(exchange_id)
        [slot] = detail["request"]["slots"]
        assert slot["masked"] == block.masked and slot["path"] == block.path
        assert slot["tool"] == (block.tool if width >= 5 else None)
        assert slot["source"] == (block.source if width >= 6 else None)
        assert detail["sensitive"]["session"] == private_session
        assert detail["sensitive"]["model"] == private_model and detail["sensitive"]["path"] == private_path
        assert detail["summary"]["coverage"] == "unknown" and detail["summary"]["verified"] is False
        assert detail["summary"]["session"] == store.history_group(private_session)
        assert all(
            canary not in json.dumps(detail["summary"]) for canary in (private_session, private_model, private_path)
        )
        assert (
            store._db.execute("SELECT summary, session, refs FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()
            == before
        )
        with pytest.raises(InvalidTag):
            store._open(sealed, "different-exchange:refs")
        other = ExchangeStore(tmp_path / "wrong-key.db", b"\x08" * 32)
        try:
            with pytest.raises(InvalidTag):
                other._open(sealed, f"{exchange_id}:refs")
        finally:
            other.close()
    finally:
        store.close()


def test_full_private_refs_use_existing_retention_and_do_not_enter_safe_reads(tmp_path):
    """The full detail shares the exchange's sealed field, row AAD, pruning and clear."""
    from cryptography.exceptions import InvalidTag

    from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "private-refs.db", ROOT_KEY)
    marker = "P25_PRIVATE_SEALED_DETAIL_19273"
    try:

        def record(at):
            block = BlockRecord(store.block_id("masked body"), "$", "user_text", False, True, "masked body")
            return store.record(
                ExchangeRecord(
                    {"ts": at, "session": "owned", "flags": []},
                    [block],
                    sensitive={"model": marker, "outbound": ["private-tool"]},
                )
            )

        exchange_id = record(1_800_000_000.0)
        summary, sealed = store._db.execute("SELECT summary, refs FROM exchanges").fetchone()
        assert marker not in summary and marker.encode() not in sealed
        assert marker in json.dumps(store.detail(exchange_id)["sensitive"])
        for rows in (store.list()[0], store.summaries_since(0), *store.summaries_parts(0, 10)):
            assert marker not in json.dumps(rows)
        with pytest.raises(InvalidTag):
            store._open(sealed, f"{exchange_id}:response")
        assert store.prune(now=1_800_000_000.0 + 15 * 86400) == 1
        assert store.detail(exchange_id) is None
        assert store._db.execute("SELECT COUNT(refs) FROM exchanges").fetchone()[0] == 0
        record(1_800_000_001.0)
        store.clear()
        assert store.list() == ([], None)
        assert store._db.execute("SELECT COUNT(refs) FROM exchanges").fetchone()[0] == 0
    finally:
        store.close()


def test_legacy_full_private_fields_remain_readable_without_backfill(tmp_path):
    """Historical content stays available in authorized detail, while all safe readers normalize it."""
    from latet.storage.exchanges import BlockRecord, ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "old-private.db", ROOT_KEY)
    try:
        block = BlockRecord(store.block_id("old masked body"), "$", "user_text", False, True, "old masked body")
        exchange_id = store.record(
            ExchangeRecord(
                {"ts": 1_800_000_000.0, "session": "owned", "flags": []},
                [block],
            )
        )
        historical = json.dumps(
            {
                "id": exchange_id,
                "ts": 1_800_000_000.0,
                "session": "owned",
                "flags": ["outbound:P25_PRIVATE_OLD_TOOL_73129"],
                "model": "P25_PRIVATE_OLD_MODEL_91273",
                "block_reason": "P25_PRIVATE_OLD_REASON_17392",
                "verified": True,
                "mode": "verify",
            }
        )
        store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (historical, exchange_id))
        before = store._db.execute("SELECT summary, refs FROM exchanges").fetchone()
        detail = store.detail(exchange_id)
        assert detail["request"]["slots"][0]["masked"] == "old masked body"
        assert detail["sensitive"]["model"] == "P25_PRIVATE_OLD_MODEL_91273"
        assert detail["sensitive"]["block_reason"] == "P25_PRIVATE_OLD_REASON_17392"
        assert detail["sensitive"]["outbound"] == ["P25_PRIVATE_OLD_TOOL_73129"]
        assert detail["summary"]["coverage"] == "unknown" and detail["summary"]["verified"] is False
        assert "P25_PRIVATE_" not in json.dumps(detail["summary"])
        assert "P25_PRIVATE_" not in json.dumps(store.list()[0])
        assert store._db.execute("SELECT summary, refs FROM exchanges").fetchone() == before
    finally:
        store.close()


@pytest.mark.parametrize(
    "value",
    [
        {"version": 2, "blocks": [], "sensitive": {}},
        {"version": True, "blocks": [], "sensitive": {}},
        {"version": 1, "blocks": [], "sensitive": {}, "coverage": "complete"},
    ],
)
def test_unknown_private_envelopes_never_become_coverage_or_authority(tmp_path, value):
    """No unsupported wrapper silently returns an apparently complete inspected exchange."""
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "unknown-refs.db", ROOT_KEY)
    try:
        exchange_id = store.record(ExchangeRecord({"ts": 1_800_000_000.0, "session": "owned", "flags": []}))
        sealed = store._seal(value, f"{exchange_id}:refs")
        store._db.execute("UPDATE exchanges SET refs = ? WHERE id = ?", (sealed, exchange_id))
        with pytest.raises(ValueError, match="unsupported history references"):
            store.detail(exchange_id)
        [summary] = store.list()[0]
        assert summary["coverage"] == "unknown" and summary["verified"] is False
        assert store._db.execute("SELECT refs FROM exchanges").fetchone()[0] == sealed
    finally:
        store.close()


def test_safe_history_categories_do_not_copy_unrecognized_labels(tmp_path):
    """Unknown detector/module/tool labels collapse to fixed categories while numeric totals survive."""
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "categories.db", ROOT_KEY)
    marker = "P25_PRIVATE_CATEGORY_39127"
    try:
        exchange_id = store.record(
            ExchangeRecord(
                {
                    "ts": 1_800_000_000.0,
                    "session": "owned",
                    "agent": marker,
                    "agent_version": marker,
                    "upstream": marker,
                    "host": marker,
                    "path": "/" + marker,
                    "model": marker,
                    "block_reason": marker,
                    "mode": "verify",
                    "flags": ["outbound:" + marker, "degraded:" + marker, "uninspected:" + marker],
                    "masked": 3,
                    "new_masked": 2,
                    "by_type": {"PERSON": 1, marker: 2},
                    "by_class": {"IDENTITY": 1, marker: 2},
                    "by_module": {"identity": 1, marker: 2},
                    "tokens": {"input": 4, marker: 10},
                    "verified": True,
                },
                leaks=[
                    {"type": marker, "kind": marker, "preview": marker, "detail": marker, "rule": marker, "new": True}
                ],
            )
        )
        [summary] = store.list()[0]
        detail = store.detail(exchange_id)
        stored = store._db.execute("SELECT summary, flags, leaks, refs FROM exchanges").fetchone()
        assert marker not in json.dumps([summary, detail["summary"], detail["leaks"], list(stored[:3])])
        assert stored[3] is None and detail["sensitive"] is None
        assert summary["by_type"] == {"PERSON": 1, "OTHER": 2}
        assert summary["by_class"] == {"IDENTITY": 1, "OTHER": 2}
        assert summary["by_module"] == {"identity": 1, "other": 2}
        assert summary["masked"] == 3 and summary["new_masked"] == 2
        assert summary["tokens"]["input"] == 4 and marker not in summary["tokens"]
        assert summary["coverage"] == "unknown" and summary["verified"] is True  # the recorded fact, as written now
        assert set(summary["flags"]) == {"outbound:remote-tool", "degraded:other", "uninspected:other"}
    finally:
        store.close()


def test_configured_destinations_keep_distinct_last_used_and_legacy_maxima(tmp_path):
    """Only explicit configured names resolve private equality indexes, including untouched old raw rows."""
    from latet.config import Config, Destination
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    config = Config(
        upstreams=[
            Destination(name="acme-east", url="https://east.example"),
            Destination(name="acme-west", url="https://west.example"),
        ]
    )
    names = tuple(d.name for d in config.upstreams)
    path = tmp_path / "destinations.db"
    store = ExchangeStore(path, ROOT_KEY)
    try:
        for name, at in ((names[0], -2.0), (names[1], 0.0), (names[0], -1.0)):
            store.record(
                ExchangeRecord(
                    {"ts": at, "session": "owned", "flags": [], "upstream": name},
                    destination_name=name,
                )
            )
        legacy_ids = []
        for name, at in ((names[0], -3.0), (names[1], 1.0)):
            exchange_id = store.record(ExchangeRecord({"ts": at, "session": "owned", "flags": []}))
            old = json.dumps({"id": exchange_id, "ts": at, "session": "owned", "flags": [], "upstream": name})
            store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (old, exchange_id))
            assert store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()[0] == old
            legacy_ids.append(exchange_id)
        before = store._db.execute("SELECT id, summary, refs FROM exchanges ORDER BY id").fetchall()
        assert store.last_used(names) == {names[0]: -1.0, names[1]: 1.0}
        assert store.last_used((names[1], names[1], "not-configured")) == {names[1]: 1.0}
        assert store.last_used([]) == {} and store.last_used() == {}
        assert store._db.execute("SELECT id, summary, refs FROM exchanges ORDER BY id").fetchall() == before
        new_physical = [row[1] for row in before if row[0] not in legacy_ids]
        assert all("dest:v1:" in row and all(name not in row for name in names) for row in new_physical)
        assert {s["upstream"] for s in store.list()[0]} <= {"custom", "unknown"}
    finally:
        store.close()
    reopened = ExchangeStore(path, ROOT_KEY)
    try:
        assert reopened.last_used(names) == {names[0]: -1.0, names[1]: 1.0}
    finally:
        reopened.close()


def test_destination_last_used_batches_real_sql_results_before_transfer(tmp_path):
    """65 configured routes + both key formats require two queries, each returning at most64 canonical slots."""
    import sqlite3

    from latet.config import Destination
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    class Rows(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.wanted = sql.startswith("WITH wanted")
            return super().execute(sql, parameters)

        def fetchall(self):
            rows = super().fetchall()
            if getattr(self, "wanted", False):
                batches.append(len(rows))
            return rows

    class Database(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=Rows).execute(sql, parameters)

    names = tuple(Destination(name=f"dest-{n:03}", url="https://gateway.example").name for n in range(65))
    path = tmp_path / "batches.db"
    store = ExchangeStore(path, ROOT_KEY)
    batches = []
    try:
        for n, name in enumerate(names):
            store.record(
                ExchangeRecord(
                    {"ts": float(n), "session": "owned", "flags": []},
                    destination_name=name,
                )
            )
            legacy_id = store.record(ExchangeRecord({"ts": float(n + 100), "session": "owned", "flags": []}))
            old = json.dumps({"id": legacy_id, "ts": float(n + 100), "session": "owned", "flags": [], "upstream": name})
            store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (old, legacy_id))
        store._db.close()
        store._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, factory=Database)
        assert store.last_used((*names, names[0])) == {name: float(n + 100) for n, name in enumerate(names)}
        assert batches == [64, 1], "must group new+legacy inside SQL before transferring result rows"
        batches.clear()
        assert store.last_used([]) == {} and batches == []
    finally:
        store.close()


@pytest.mark.parametrize("name", [None, "UpperCase", "bad_name", "x" * 64, "backend-api"])
def test_raw_summary_or_invalid_destination_never_becomes_a_custom_index(tmp_path, name):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    class ClientString(str):
        pass

    store = ExchangeStore(tmp_path / "untrusted-destination.db", ROOT_KEY)
    try:
        for value in (name, ClientString("acme-east")):
            store.record(
                ExchangeRecord(
                    {"ts": 1.0, "session": "owned", "flags": [], "upstream": "acme-east"},
                    destination_name=value,
                )
            )
        raw = store._db.execute("SELECT summary FROM exchanges").fetchall()
        assert all("acme-east" not in row[0] and "dest:v1:" not in row[0] for row in raw)
        assert store.last_used(["acme-east"]) == {}
    finally:
        store.close()


@pytest.mark.parametrize("history", ["metadata", "full", "off"])
def test_matched_destination_recorder_reaches_authenticated_integrations(
    tmp_path, outbound_owner, monkeypatch, history
):
    """Real matched configured routes feed Recorder; the existing signed-in API returns distinct last-used facts."""
    import time
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from latet.api.rest import Dashboard
    from latet.config import Config, Destination
    from latet.doctor import checks
    from latet.gateway.recorder import Outcome, Recorder
    from latet.gateway.routes import RouteTable, destination_upstream
    from latet.service import autostart
    from latet.storage.exchanges import ExchangeStore

    configured = Config(
        upstreams=[
            Destination(name="acme-east", url="https://east.example"),
            Destination(name="acme-west", url="https://west.example"),
        ]
    )
    table = RouteTable(destination_upstream(d) for d in configured.upstreams)
    store = ExchangeStore(tmp_path / "matched-destinations.db", ROOT_KEY)
    recorder = Recorder(store, engine=outbound_owner.engine, history=history)
    try:
        for name, at in (
            ("acme-east", 1_800_000_001.0),
            ("acme-west", 1_800_000_002.0),
            ("acme-east", 1_800_000_003.0),
        ):
            path = "/" + name + "/v1/messages"
            matched = table.match(path)
            assert matched is not None and matched.name == name
            prepared = _prepared(outbound_owner, "anthropic-server", value=FAKE + " P25_PRIVATE_DESTINATION_BODY")
            prepared.model = "P25_PRIVATE_DESTINATION_MODEL"
            recorder.finish(Outcome({}, matched.name, path, "http", at, prepared, 400))
        assert recorder.drain(timeout=5)
        rows, _ = store.list()
        assert len(rows) == (0 if history == "off" else 3)
        if history != "off":
            assert {row["upstream"] for row in rows} == {"custom"}
            assert "acme-east" not in json.dumps(rows) and "P25_PRIVATE_" not in json.dumps(rows)
            physical = store._db.execute("SELECT summary, refs FROM exchanges").fetchall()
            assert all("acme-" not in row[0] and "dest:v1:" in row[0] for row in physical)
            assert all((row[1] is None) == (history == "metadata") for row in physical)
            if history == "full":
                assert store.detail(rows[0]["id"])["sensitive"]["model"] == "P25_PRIVATE_DESTINATION_MODEL"
        rt = SimpleNamespace(
            config=configured,
            exchanges=store,
            identity=None,
            backends=None,
            gateway=SimpleNamespace(port=5283, on_server=None),
        )
        dashboard = Dashboard(rt)
        dashboard._agents = (time.time(), [])
        monkeypatch.setattr(checks, "check_engine", lambda: [])
        monkeypatch.setattr(checks, "check_port", lambda: [])
        monkeypatch.setattr(autostart, "installed", lambda: False)
        monkeypatch.setattr(dashboard, "_in_use", lambda: set())
        app = Starlette(routes=dashboard.routes())
        with TestClient(app, base_url="http://127.0.0.1:5283") as client:
            assert client.get("/_latet/api/v1/integrations").status_code == 401
            client.cookies.set(dashboard.cookie, dashboard.sign_ins.open(""))
            response = client.get("/_latet/api/v1/integrations")
            assert response.status_code == 200
            actual = {d["name"]: d["last_used"] for d in response.json()["destinations"]}
        expected = (
            {"acme-east": None, "acme-west": None}
            if history == "off"
            else {
                "acme-east": 1_800_000_003.0,
                "acme-west": 1_800_000_002.0,
            }
        )
        assert actual == expected
    finally:
        recorder.close()
        store.close()


def test_history_groups_merge_real_utf8_legacy_rows_before_limit_without_backfill(tmp_path, monkeypatch):
    """Qualifies readonly reentrant SQLite TEXT blob reads and canonical grouping before LIMIT."""
    import sqlite3

    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    reads = []

    class Blob:
        def __init__(self, actual):
            self.actual = actual

        def __len__(self):
            return len(self.actual)

        def read(self, count):
            data = self.actual.read(count)
            reads.append((count, len(data)))
            return data

        def __enter__(self):
            self.actual.__enter__()
            return self

        def __exit__(self, *args):
            return self.actual.__exit__(*args)

    class Database(sqlite3.Connection):
        def blobopen(self, table, column, row, *, readonly=False, name="main"):
            assert table in ("exchanges", "reviews") and column == "session" and readonly is True
            return Blob(super().blobopen(table, column, row, readonly=readonly, name=name))

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Database))
    path = tmp_path / "mixed-groups.db"
    source = "x-session-id:P25_PRIVATE_SOURCE_" + "\u00e9\u6f22\U0001f642" * 1500
    store = ExchangeStore(path, ROOT_KEY)
    try:
        group = store.history_group(source)
        old_id = store.record(ExchangeRecord({"ts": 10.0, "session": source, "flags": []}))
        historical = json.dumps(
            {
                "id": old_id,
                "ts": 10.0,
                "session": source,
                "agent": "other",
                "flags": ["blocked"],
                "new_masked": 1,
                "verified": True,
                "mode": "verify",
            }
        )
        store._db.execute(
            "UPDATE exchanges SET session = ?, summary = ?, flags = ? WHERE id = ?",
            (source, historical, " blocked ", old_id),
        )
        before = store._db.execute("SELECT session, summary, refs FROM exchanges WHERE id = ?", (old_id,)).fetchone()
        assert before[0] == source and json.loads(before[1])["verified"] is True
        current = store.record(
            ExchangeRecord({"ts": 20.0, "session": group, "flags": [], "new_masked": 2}, session_source=source)
        )
        store.record(ExchangeRecord({"ts": 15.0, "session": "other source", "flags": []}))
        backwards = store.record(ExchangeRecord({"ts": 5.0, "session": source, "flags": [], "new_masked": 4}))
        review_id = store.record_review(
            {
                "ts": 21.0,
                "session": group,
                "project": None,
                "block": "b" * 32,
                "kind": "user_text",
                "inline": True,
                "findings": [],
            },
            session_source=source,
        )
        review_old = json.dumps(
            {
                "ts": 21.0,
                "session": source,
                "project": None,
                "block": "b" * 32,
                "kind": "user_text",
                "inline": True,
                "findings": [],
            }
        )
        store._db.execute("UPDATE reviews SET session = ?, summary = ? WHERE id = ?", (source, review_old, review_id))
        review_before = store._db.execute("SELECT session, summary FROM reviews WHERE id = ?", (review_id,)).fetchone()
        [session] = store.sessions(limit=1)
        assert (
            session["key"],
            session["first_ts"],
            session["last_ts"],
            session["exchanges"],
            session["masked"],
            session["blocked"],
        ) == (
            group,
            5.0,
            20.0,
            3,
            7,
            1,
        )
        assert store.in_order(source) == store.in_order(group) == [old_id, current, backwards]
        assert {s["id"] for s in store.list(session=source)[0]} == {old_id, current, backwards}
        assert {s["id"] for s in store.list(session=group)[0]} == {old_id, current, backwards}
        for rows in (store.list(session=group)[0], store.summaries_since(0), *store.summaries_parts(0, 10)):
            assert all(s["session"] == group for s in rows if s["id"] in (old_id, current, backwards))
            assert source not in json.dumps(rows)
        assert store.detail(old_id)["summary"]["session"] == group
        assert store.reviews(session=source) == store.reviews(session=group)
        assert store.reviews(session=group)[0]["session"] == group
        assert all(requested <= 4096 and actual <= requested for requested, actual in reads)
        assert any(actual == 4096 for _requested, actual in reads), "actual multi-chunk TEXT read must occur"
        assert (
            store._db.execute("SELECT session, summary, refs FROM exchanges WHERE id = ?", (old_id,)).fetchone()
            == before
        )
        assert (
            store._db.execute("SELECT session, summary FROM reviews WHERE id = ?", (review_id,)).fetchone()
            == review_before
        )
    finally:
        store.close()
    reopened = ExchangeStore(path, ROOT_KEY)
    try:
        assert reopened.history_group(source) == group and reopened.sessions(limit=1)[0]["key"] == group
    finally:
        reopened.close()


def test_handle_shaped_raw_legacy_session_is_never_writer_provenance(tmp_path):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "prefix-groups.db", ROOT_KEY)
    try:
        raw = store.history_group("a different original source")
        actual = store.history_group(raw)
        assert actual != raw
        old_id = store.record(ExchangeRecord({"ts": 1.0, "session": "temporary", "flags": []}))
        old = json.dumps({"id": old_id, "ts": 1.0, "session": raw, "flags": [], "new_masked": 0})
        store._db.execute("UPDATE exchanges SET session = ?, summary = ? WHERE id = ?", (raw, old, old_id))
        new_id = store.record(ExchangeRecord({"ts": 2.0, "session": raw, "flags": [], "history_group_version": 1}))
        [session] = store.sessions()
        assert session["key"] == actual and session["exchanges"] == 2
        assert store.in_order(actual) == [old_id, new_id]
        assert store.detail(old_id)["summary"]["session"] == actual
    finally:
        store.close()


@pytest.mark.parametrize("corruption", ["bool-marker", "wrong-version", "wrong-mac", "invalid-json"])
def test_invalid_new_history_group_is_explicitly_unavailable_not_merged(tmp_path, corruption):
    import sqlite3

    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "invalid-group.db", ROOT_KEY)
    try:
        exchange_id = store.record(ExchangeRecord({"ts": 1.0, "session": "private source", "flags": []}))
        if corruption == "bool-marker":
            store._db.execute(
                "UPDATE exchanges SET summary = json_set(summary, '$.history_group_version', json('true'))"
            )
        elif corruption == "wrong-version":
            store._db.execute("UPDATE exchanges SET summary = json_set(summary, '$.history_group_version', 2)")
        elif corruption == "wrong-mac":
            store._db.execute("UPDATE exchanges SET session = ?", ("hg:l1:" + "0" * 64 + ":" + "0" * 64,))
        else:
            store._db.execute("UPDATE exchanges SET summary = ?", ("{not-json",))
        with pytest.raises(sqlite3.OperationalError):
            store.sessions()
        if corruption != "invalid-json":
            detail = store.detail(exchange_id)
            assert detail["summary"]["session"] is None and detail["summary"]["coverage"] == "unknown"
    finally:
        store.close()


@pytest.mark.parametrize("history", ["metadata", "full", "off"])
def test_registered_projects_and_session_groups_reach_recorder_feed_and_authenticated_filters(
    tmp_path, outbound_owner, history
):
    from types import SimpleNamespace

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from latet.api.rest import Dashboard
    from latet.config import Config, IdentityConfig
    from latet.gateway.recorder import Outcome, Recorder
    from latet.projects import Projects
    from latet.storage.exchanges import ExchangeStore

    config = Config(ner="off", identity=IdentityConfig(enabled=False))
    projects = Projects(config, outbound_owner.engine.vault, files=False)
    outbound_owner.engine.scopes = projects
    store = ExchangeStore(tmp_path / "registered-history.db", ROOT_KEY)
    feed = []

    class Feed:
        def publish(self, summary, kind="exchange"):
            feed.append((summary, kind))

    recorder = Recorder(store, engine=outbound_owner.engine, history=history, broadcaster=Feed())
    try:
        known = []
        for n in (0, 1, 0):
            prepared = _prepared(outbound_owner, "anthropic-server")
            source = prepared.session.key
            project = projects.attach(prepared.session, str(tmp_path / f"P25_PRIVATE_FOLDER_{n}"))
            assert project is not None and projects.peek(project.id) is project
            known.append(project.id)
            recorder.finish(
                Outcome({}, "anthropic", "/v1/messages", "http", 1_800_000_000.0 + len(known), prepared, 400)
            )
        assert recorder.drain(timeout=5)
        rows = store.list()[0]
        assert len(rows) == (0 if history == "off" else 3)
        assert len(feed) == len(rows)
        if history == "off":
            assert store._db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0
            return
        assert known[0] == known[2] and known[0] != known[1]
        group = store.history_group(source)
        assert all(row["session"] == group and row["project"]["name"] == row["project"]["id"] for row in rows)
        assert all(summary["session"] == group for summary, _kind in feed)
        assert "P25_PRIVATE_FOLDER_" not in json.dumps(rows + [summary for summary, _kind in feed])
        columns = store._db.execute("SELECT session, project, summary, refs FROM exchanges").fetchall()
        assert all(
            row[0] == group and row[1] in known and source not in row[2] and "P25_PRIVATE_FOLDER_" not in row[2]
            for row in columns
        )
        assert all((row[3] is None) == (history == "metadata") for row in columns)
        if history == "full":
            assert "P25_PRIVATE_FOLDER_" in store.detail(rows[0]["id"])["sensitive"]["project"]["name"]
        rt = SimpleNamespace(
            config=config,
            exchanges=store,
            identity=None,
            backends=None,
            projects=projects,
            gateway=SimpleNamespace(port=5283, on_server=None, work=None),
        )
        dashboard = Dashboard(rt)
        with TestClient(Starlette(routes=dashboard.routes()), base_url="http://127.0.0.1:5283") as client:
            assert client.get("/_latet/api/v1/exchanges").status_code == 401
            client.cookies.set(dashboard.cookie, dashboard.sign_ins.open(""))
            first = client.get(
                "/_latet/api/v1/exchanges", params={"project": known[0], "session": group, "limit": 1}
            ).json()
            second = client.get(
                "/_latet/api/v1/exchanges",
                params={"project": known[0], "session": group, "limit": 1, "before": first["next"]},
            ).json()
            assert [row["project"]["id"] for row in first["items"] + second["items"]] == [known[0], known[0]]
            assert first["items"][0]["id"] != second["items"][0]["id"] and second["next"] is None
            assert client.get(f"/_latet/api/v1/projects/{known[0]}").status_code == 200
            assert client.get("/_latet/api/v1/projects/ffffffffffff").status_code == 404
    finally:
        recorder.close()
        store.close()


def test_claimed_legacy_projects_normalize_without_backfill_and_new_claims_are_dropped(tmp_path):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "legacy-project.db", ROOT_KEY)
    try:
        pid = "012345abcdef"
        claimed = store.record(
            ExchangeRecord(
                {"ts": 2.0, "session": "owned", "flags": [], "project": {"id": pid, "name": "P25_PRIVATE_NEW_CLAIM"}}
            )
        )
        assert store.detail(claimed)["summary"]["project"] is None
        assert store.list(project=pid)[0] == []
        old_id = store.record(ExchangeRecord({"ts": 1.0, "session": "owned", "flags": []}))
        historical = json.dumps(
            {
                "id": old_id,
                "ts": 1.0,
                "session": "owned",
                "flags": [],
                "mode": "verify",
                "verified": True,
                "project": {"id": pid, "name": "P25_PRIVATE_OLD_FOLDER", "cwd": "/P25_PRIVATE_CWD"},
            }
        )
        store._db.execute(
            "UPDATE exchanges SET session = ?, summary = ?, project = ? WHERE id = ?",
            ("owned", historical, pid, old_id),
        )
        before = store._db.execute(
            "SELECT session, project, summary, refs FROM exchanges WHERE id = ?", (old_id,)
        ).fetchone()
        [summary] = store.list(project=pid)[0]
        assert summary["project"] == {"id": pid, "name": pid}
        assert (
            "P25_PRIVATE_" not in json.dumps(summary)
            and summary["coverage"] == "unknown"
            and summary["verified"] is False
        )
        assert (
            store._db.execute(
                "SELECT session, project, summary, refs FROM exchanges WHERE id = ?", (old_id,)
            ).fetchone()
            == before
        )
        malformed = json.dumps(
            json.loads(historical) | {"project": {"id": "/P25_PRIVATE_BAD_PATH", "name": "P25_PRIVATE_BAD_NAME"}}
        )
        store._db.execute(
            "UPDATE exchanges SET summary = ?, project = ? WHERE id = ?", (malformed, "/P25_PRIVATE_BAD_PATH", old_id)
        )
        bad_before = store._db.execute(
            "SELECT session, project, summary, refs FROM exchanges WHERE id = ?", (old_id,)
        ).fetchone()
        assert store.detail(old_id)["summary"]["project"] is None
        assert (
            store._db.execute(
                "SELECT session, project, summary, refs FROM exchanges WHERE id = ?", (old_id,)
            ).fetchone()
            == bad_before
        )
    finally:
        store.close()


@pytest.mark.parametrize("registered", [True, False])
def test_runtime_review_uses_raw_engine_key_before_private_history_copy(
    tmp_path, outbound_owner, monkeypatch, registered
):
    from types import SimpleNamespace

    from latet.config import Config, IdentityConfig
    from latet.engine.reviewer.reviewer import Answer
    from latet.gateway.mediate import ReviewRecord
    from latet.projects import Projects
    from latet.runtime import Runtime
    from latet.storage.exchanges import ExchangeStore

    config = Config(ner="off", identity=IdentityConfig(enabled=False))
    engine = outbound_owner.engine
    projects = Projects(config, engine.vault, files=False)
    engine.scopes = projects
    source = "session-id:P25_PRIVATE_REVIEW_SOURCE"
    session = engine.session(source)
    project = projects.attach(session, str(tmp_path / "P25_PRIVATE_REVIEW_FOLDER")) if registered else None
    pid = project.id if project is not None else "012345abcdef"
    store = ExchangeStore(tmp_path / "review-group.db", ROOT_KEY)
    calls, feed = [], []
    original_session = engine.session
    original_group = store.history_group

    def lookup(key):
        calls.append(key)
        return original_session(key)

    def history_group(raw):
        assert calls and calls[0] == source, "normal raw engine allow lookup must precede history projection"
        return original_group(raw)

    class Feed:
        def publish(self, summary, kind="exchange"):
            feed.append((summary, kind))

    monkeypatch.setattr(engine, "session", lookup)
    monkeypatch.setattr(store, "history_group", history_group)
    rt = Runtime(
        config,
        SimpleNamespace(engine=engine, vault=engine.vault),
        None,
        SimpleNamespace(mediator=outbound_owner),
        tmp_path,
        exchanges=store,
        recorder=SimpleNamespace(broadcaster=Feed()),
        projects=projects,
    )
    record = ReviewRecord(source, pid, "user_text", True, "masked body", Answer(), [])
    try:
        rt._record_review(record)
        assert calls and all(key == source for key in calls)
        assert record.session == source and record.project == pid
        [(summary, kind)] = feed
        assert kind == "review" and summary["session"] == original_group(source)
        assert summary["project"] == (pid if registered else None)
        assert source not in json.dumps(summary) and "P25_PRIVATE_REVIEW_FOLDER" not in json.dumps(summary)
        [persisted] = store.reviews(session=summary["session"])
        assert persisted["session"] == summary["session"] and persisted["project"] == summary["project"]
        assert source not in json.dumps(store._db.execute("SELECT session, summary FROM reviews").fetchall())
    finally:
        store.close()


def test_full_leak_locations_stay_sealed_and_readable_while_metadata_has_no_preview(tmp_path):
    import time

    from latet.engine.types import SlotKind
    from latet.engine.verifier import Leak
    from latet.gateway.recorder import Outcome, Recorder
    from latet.protocols.base import SlotRef
    from latet.storage.exchanges import ExchangeStore

    preview = "P25_PRIVATE_LEAK_PREVIEW_19372"
    text = "masked body"
    leaks = [
        Leak(
            "PASSWORD",
            EntityClass.SECRET,
            "field-secret:password",
            SlotKind.TOOL_RESULT,
            preview,
            True,
            slot=0,
            start=0,
            end=6,
        )
    ]
    for history in ("full", "metadata"):
        store = ExchangeStore(tmp_path / (history + "-leaks.db"), ROOT_KEY)
        recorder = Recorder(store, history=history)
        try:
            document = [text]
            prepared = Prepared(
                VERIFY,
                "anthropic",
                json.dumps(document).encode(),
                None,
                leaks,
                document=document,
                pending=[(SlotRef(document, 0, SlotKind.TOOL_RESULT), [], False)],
                block_reason="stopped",
            )
            recorder.finish(Outcome({}, "anthropic", "/v1/messages", "http", time.time(), prepared, 400))
            assert recorder.drain(timeout=5)
            [summary] = store.list()[0]
            detail = store.detail(summary["id"])
            raw_summary, raw_leaks, sealed = store._db.execute("SELECT summary, leaks, refs FROM exchanges").fetchone()
            before = (raw_summary, raw_leaks, sealed)
            assert preview not in raw_summary and preview not in raw_leaks
            assert preview not in json.dumps(summary)
            [returned] = detail["leaks"]
            if history == "full":
                [slot] = detail["request"]["slots"]
                assert (
                    returned["block"] == slot["block"] and returned["masked_start"] == 0 and returned["masked_end"] == 6
                )
                assert returned["rule"] == "field-secret:password" and returned["preview"] == preview
                assert preview.encode() not in sealed
                assert detail["sensitive"]["leaks"][0] == returned
            else:
                assert returned == {"type": "PASSWORD", "kind": "tool_result", "new": True, "allowed": False}
                assert detail["sensitive"] is None and sealed is None
            assert store._db.execute("SELECT summary, leaks, refs FROM exchanges").fetchone() == before
        finally:
            recorder.close()
            store.close()


@pytest.mark.parametrize("transition", ["replace", "close"])
def test_selected_history_pages_keep_original_group_after_other_store_reuses_rowids(tmp_path, transition):
    """Actual page-size+1 snapshot survives a second Store clear/reinsert, or closing the selecting Store."""
    from latet.storage import exchanges as history
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    path = tmp_path / "page-snapshot.db"
    selecting = ExchangeStore(path, ROOT_KEY)
    replacing = ExchangeStore(path, ROOT_KEY)
    original = selecting.history_group("P25_PRIVATE_ORIGINAL_SESSION")
    replacement = selecting.history_group("P25_PRIVATE_REPLACEMENT_SESSION")
    try:
        count = history._SUMMARIES_AT_ONCE + 1  # actual production page boundary; no cap/time override
        ids = [
            selecting.record(ExchangeRecord({"ts": float(n), "session": "P25_PRIVATE_ORIGINAL_SESSION", "flags": []}))
            for n in range(count)
        ]
        old_rowid, old_summary = selecting._db.execute(
            "SELECT rowid, summary FROM exchanges WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert old_rowid == 1 and json.loads(old_summary)["session"] == original
        pages = selecting.summaries_parts(0, count)
        first = next(pages)
        assert len(first) == history._SUMMARIES_AT_ONCE
        assert [row["id"] for row in first] == list(reversed(ids[1:]))
        assert all(row["session"] == original for row in first)
        if transition == "replace":
            replacing.clear()
            new_id = replacing.record(
                ExchangeRecord(
                    {
                        "ts": 10_000.0,
                        "session": "P25_PRIVATE_REPLACEMENT_SESSION",
                        "flags": [],
                    }
                )
            )
            new_rowid, new_summary = replacing._db.execute(
                "SELECT rowid, summary FROM exchanges WHERE id = ?",
                (new_id,),
            ).fetchone()
            assert new_rowid == old_rowid and new_id != ids[0], "actual rowid reuse must be reached"
            assert json.loads(new_summary)["session"] == replacement and original != replacement
            assert replacing._db.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 1
        else:
            selecting.close()
        [remaining] = list(pages)
        assert [row["id"] for row in remaining] == [ids[0]], "must still be the old selected summary"
        assert remaining[0]["session"] == original, "selected old summary was rebound after its page yielded"
    finally:
        selecting.close()
        replacing.close()


def test_selected_history_summary_keeps_group_when_other_store_replaces_after_real_fetchall(tmp_path, monkeypatch):
    """Real native fetchall completes before a second connection clears/reinserts; no fake SQL rows."""
    import sqlite3

    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    reached = []

    class Rows(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.selected = (
                sql.startswith("SELECT ") and " FROM exchanges" in sql and " ORDER BY ts DESC, id DESC LIMIT" in sql
            )
            return super().execute(sql, parameters)

        def fetchall(self):
            rows = super().fetchall()
            after = getattr(self.connection, "after_selection", None)
            if self.selected and after is not None:
                self.connection.after_selection = None
                after(rows)  # the rows returned below are the actual native result, unchanged
            return rows

    class Database(sqlite3.Connection):
        after_selection = None

        def execute(self, sql, parameters=()):
            return self.cursor(factory=Rows).execute(sql, parameters)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Database))
    path = tmp_path / "fetchall-snapshot.db"
    selecting = ExchangeStore(path, ROOT_KEY)
    replacing = ExchangeStore(path, ROOT_KEY)
    try:
        original = selecting.history_group("P25_PRIVATE_FETCH_ORIGINAL")
        replacement = selecting.history_group("P25_PRIVATE_FETCH_REPLACEMENT")
        old_id = selecting.record(
            ExchangeRecord(
                {
                    "ts": 1.0,
                    "session": "P25_PRIVATE_FETCH_ORIGINAL",
                    "flags": [],
                }
            )
        )
        old_rowid, old_summary = selecting._db.execute(
            "SELECT rowid, summary FROM exchanges WHERE id = ?",
            (old_id,),
        ).fetchone()
        assert old_rowid == 1 and json.loads(old_summary)["session"] == original

        def replace_after_real_fetchall(rows):
            assert len(rows) == 1 and old_summary in rows[0], "actual old native SELECT result must be reached"
            replacing.clear()
            new_id = replacing.record(
                ExchangeRecord(
                    {
                        "ts": 2.0,
                        "session": "P25_PRIVATE_FETCH_REPLACEMENT",
                        "flags": [],
                    }
                )
            )
            new_rowid, new_summary = replacing._db.execute(
                "SELECT rowid, summary FROM exchanges WHERE id = ?",
                (new_id,),
            ).fetchone()
            assert new_rowid == old_rowid and new_id != old_id
            assert json.loads(new_summary)["session"] == replacement and replacement != original
            reached.append((old_id, new_id, old_rowid))

        selecting._db.after_selection = replace_after_real_fetchall
        [summary], cursor = selecting.list()
        assert reached and reached[0][0] == old_id and cursor is None
        assert summary["id"] == old_id, "must still be the original selected row"
        assert summary["session"] == original, "post-fetch normalization read another conversation's reused rowid"
    finally:
        selecting.close()
        replacing.close()


# New infrastructure contracts below are scaffolding until executed against the
# real owner; missing new symbols are never an existing-behavior causal RED.
@pytest.fixture
def audit_space(tmp_path):
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    from latet.config import Config, IdentityConfig
    from latet.engine.build import build
    from latet.gateway.app import GatewayConfig
    from latet.projects import Projects
    from latet.runtime import NewValues, Runtime
    from latet.server.spaces import Space, Spaces

    config = Config(ner="off", identity=IdentityConfig(enabled=False))
    vault = Vault(ROOT_KEY)
    built = build(config, vault)
    mediator = Mediator(built.engine, built.verifier)
    storage = SimpleNamespace(index_bytes=0, save_entity=lambda entity: entity, close=lambda: None)
    runtime = Runtime(config, built, storage, GatewayConfig(port=5283, mediator=mediator), tmp_path)
    runtime.new_values = NewValues(storage)
    vault._on_insert = runtime.new_values.keep
    runtime.projects = Projects(config, vault, files=False)
    spaces = Spaces(SimpleNamespace())
    space = Space(
        SimpleNamespace(id="u_p25core"),
        runtime,
        None,
        SimpleNamespace(approvals=SimpleNamespace(holding=False)),
        AsyncExitStack(),
        0,
    )
    spaces._spaces[space.identity.id] = space
    spaces._bind_request_budget(space)
    spaces._weighed(space)
    try:
        yield spaces, space, runtime
    finally:
        runtime.close()
        spaces._executor.shutdown(wait=True)
        spaces._index_executor.shutdown(wait=True)


def _audit_record(**changes):
    from latet.gateway.request_audit import SafeDecisionRecord

    return SafeDecisionRecord(**({"source": 1, "outcome": 1, "coverage": 1, "attempts": 1, "answered": 1} | changes))


def _owned_audit_shape(owner, *queued_inputs):
    """Actual owned graph; server clients/mediator/context referents are borrowed."""
    import contextvars
    import sys
    import threading
    import traceback
    import types
    from collections import OrderedDict, deque
    from concurrent.futures import Future

    from starlette.datastructures import MutableHeaders

    from latet.api.rest import (
        _PublicationInput,
        _RequestStatusResponse,
        _StatusInput,
        _StatusSubmission,
        _StatusTerminal,
    )
    from latet.gateway.request_audit import (
        ExceptionReference,
        RequestAudit,
        Reservation,
        SafeDecisionRecord,
        SnapshotWork,
        _D2Counts,
        _Entry,
        _ProjectionScope,
        _Status,
        _SweepLease,
    )

    seen = set()
    from latet.server.live import ValkeyRequestStatus
    from latet.server.spaces import _AuditFence, _StatusSweep

    owned_types = (
        ExceptionReference,  # the references an audit keeps (minors after 0.11, item 10)
        RequestAudit,
        Reservation,
        SafeDecisionRecord,
        SnapshotWork,
        _D2Counts,
        _Entry,
        _ProjectionScope,
        _Status,
        _SweepLease,
        _StatusSubmission,
        _StatusTerminal,
    )

    def size(value):
        if id(value) in seen or isinstance(value, (types.FunctionType, types.MethodType, type)):
            return 0
        seen.add(id(value))
        total = sys.getsizeof(value)
        if isinstance(value, (dict, OrderedDict)):
            total += sum(size(k) + size(v) for k, v in value.items())
        elif isinstance(value, (list, tuple, deque, set, frozenset)):
            total += sum(size(v) for v in value)
        elif type(value) in (Future, threading.Condition, _RequestStatusResponse, MutableHeaders):
            total += size(vars(value))
        elif type(value) in (_StatusInput, _PublicationInput):
            total += sum(size(getattr(value, slot)) for slot in type(value).__slots__ if slot != "_mediator")
        elif type(value) is _StatusSweep:
            total += sum(
                size(getattr(value, slot)) for slot in type(value).__slots__ if slot not in ("_loop", "__weakref__")
            )
        elif type(value) is asyncio.Handle:
            total += size(value._callback) + size(value._args) + size(value._context) + size(value._source_traceback)
        elif type(value) is contextvars.Context:
            total += sum(size(key) + size(item) for key, item in value.items())
        elif type(value) is traceback.FrameSummary:
            total += sum(size(getattr(value, slot)) for slot in traceback.FrameSummary.__slots__)
        elif type(value) is ValkeyRequestStatus:
            total += sum(
                size(getattr(value, slot)) for slot in type(value).__slots__ if slot not in ("_bus", "_script")
            )
            if value._script is not None:
                # Existing shared client and immutable module script text are
                # borrowed. Count the actual per-transport Script shell/dict/SHA.
                total += sys.getsizeof(value._script) + sys.getsizeof(vars(value._script))
                total += size(value._script.sha)
        elif type(value) is _AuditFence:
            total += sys.getsizeof(value.locks) + sys.getsizeof(value.check)
        elif type(value) in owned_types:
            total += sum(size(getattr(value, slot)) for slot in type(value).__slots__ if slot != "_ceiling")
            if type(value) is RequestAudit and type(value._ceiling) is _AuditFence:
                total += size(value._ceiling)
        return total

    return size(owner) + sum(size(value) for value in queued_inputs if value is not None)


def test_real_empty_audit_base_is_in_runtime_and_first_space_weigh(audit_space):
    from latet.gateway.request_audit import BASE_BYTES, RequestAudit
    from latet.server.spaces import space_size

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    assert type(audit) is RequestAudit and audit.retained_bytes == BASE_BYTES == 4096
    assert runtime.request_retained_bytes == 4096 and space.retained == 4096
    assert space.size == space_size(runtime)
    old = space.size
    assert audit.record(_audit_record())
    delta = audit.retained_bytes - 4096
    assert delta > 0 and space_size(runtime) == old + delta
    spaces._weighed(space)
    assert space.size == old + delta and space.retained == audit.retained_bytes


@pytest.mark.parametrize(
    "changes",
    [
        {"source": TOOL},
        {"source": True},
        {"attempts": 3},
        {"answered": 2},
        {"status": 99},
        {"revision": -1},
        {"rules": ((9, 1),)},  # 8 is the exception category now
        {"rules": ((7, 1),)},  # 7 named legacy-0.10: retired, never written again
        {"rules": ((1, 1), (1, 1))},
    ],
)
def test_audit_schema_rejects_nonfinite_or_inconsistent_facts_without_copy(changes):
    from latet.gateway.request_audit import RequestAudit

    audit = RequestAudit()
    before = audit.retained_bytes
    assert not audit.record(_audit_record(**changes))
    assert audit.retained_bytes == before and not audit._events and not audit._statuses
    assert audit.overflow == 1 and audit.partial
    audit.close()
    assert audit.retained_bytes == 0


def test_audit_event_and_status_caps_keep_cumulative_counts_and_native_full_shape():
    import json

    from latet.gateway.request_audit import MAX_BYTES, MAX_EVENTS, RequestAudit, new_exception_reference

    audit = RequestAudit()
    assert _owned_audit_shape(audit) <= audit.retained_bytes
    for _ in range(128):
        reservation = audit.reserve_exception(new_exception_reference(1, 1))
        assert reservation is not None
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        assert audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for _ in range(MAX_EVENTS + 100):
        assert audit.record(_audit_record(), bucket=0)
    assert len(audit._events) == MAX_EVENTS and len(audit._statuses) == 136
    assert audit._statuses[0].uses == MAX_EVENTS + 101
    assert audit.retained_bytes <= MAX_BYTES == 1024 * 1024
    assert _owned_audit_shape(audit) <= audit.retained_bytes
    assert all(len(entry.payload) <= 4096 and json.loads(entry.payload)[0] == 1 for entry in audit._events.values())
    replacement = audit.reserve_exception(new_exception_reference(2, 1))
    assert replacement is not None and audit.partial
    assert len(audit._statuses) == 136
    assert _owned_audit_shape(audit) <= audit.retained_bytes
    audit.abandon(replacement)
    audit.close()
    audit.close()
    assert audit.retained_bytes == 0 and not audit._events and not audit._statuses


def test_active_reservations_survive_capacity_close_and_cancel_until_actual_finalizer():
    from latet.gateway.request_audit import MAX_BYTES, RequestAudit, new_exception_reference

    audit = RequestAudit()
    reservations = []
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for _ in range(128):
        reference = new_exception_reference(1, 1)
        reservation = audit.reserve_exception(reference)
        assert reservation is not None
        reservations.append(reservation)
    while (reservation := audit.reserve_exception(reference)) is not None:
        reservations.append(reservation)
    assert len(audit._statuses) == 136 and audit.active == len(reservations)
    assert audit.retained_bytes <= MAX_BYTES and _owned_audit_shape(audit) <= audit.retained_bytes
    before = audit.retained_bytes
    assert not audit.record(_audit_record()) and audit.retained_bytes == before
    audit.close()
    assert audit.retained_bytes > 0 and audit.active == len(reservations)
    assert audit.reserve_exception(reference) is None
    for reservation in reservations[:-1]:
        assert audit.abandon(reservation)
        assert not audit.abandon(reservation)
    assert audit.active == 1 and audit.retained_bytes > 0
    assert audit.finalize(reservations[-1], _audit_record(answered=None))
    assert audit.active == 0 and audit.retained_bytes == 0
    assert all(reservation._owner is None for reservation in reservations)
    assert not audit.finalize(reservations[-1], _audit_record())


# --- an exception's status reference is its identity's own (minors after 0.11, item 10; G2 M-4) -------------------------


def _exception_row(row_id, reason="custom-header"):
    """What ``forward._visible`` reads of a request exception a request is sent under: its id and its reason."""
    from types import SimpleNamespace

    return SimpleNamespace(id=row_id, reason=reason)


def _used_under(audit, row):
    """One request sent under ``row``'s exception, as the forwarder makes it visible (``_visible``) and lets its
    reservation go as it ends (``_settle``, not sent): the status key its use was reserved under, which carries the
    exception's reference."""
    from latet.gateway import forward

    turn = forward._Turn()
    assert forward._visible(audit, turn, (row,))
    [reservation] = turn.reservations
    key = audit._events[reservation._sequence].status
    forward._settle(audit, turn, None, None)
    assert turn.reservations == [] and audit.active == 0
    return key


def test_an_exceptions_status_reference_is_its_identitys_own_and_never_evicted_while_its_audit_lives():
    """An exception's uses are counted under a reference its identity's audit keeps, never under one of a list the
    whole process shares, keyed by the exception's id alone and holding 4,096. Two identities whose exceptions have the
    same id (40 random bits) never share a reference, and other identities' exceptions, however many, never take one
    away: an exception's status never splits into a new row while its audit lives."""
    from latet.gateway.request_audit import EXCEPTION_SLOTS, RequestAudit

    first, second, others = RequestAudit(), RequestAudit(), RequestAudit()
    same = "x0123456789"  # the same exception id in both identities
    try:
        before = (_used_under(first, _exception_row(same)), _used_under(second, _exception_row(same)))
        for n in range(4097):  # 4,097 other exceptions, a third identity's, used in between
            _used_under(others, _exception_row(f"x{n + 1:010x}"))
        after = (_used_under(first, _exception_row(same)), _used_under(second, _exception_row(same)))
        facts = {
            "the first identity's reference is the same at each use": after[0] == before[0],
            "the second identity's reference is the same at each use": after[1] == before[1],
            "the two identities' references differ": before[0][0] != before[1][0],
        }
        assert all(facts.values()), facts
        assert len(others._references) == len(others._statuses) == EXCEPTION_SLOTS  # each went with its status row
    finally:
        for audit in (first, second, others):
            audit.close()
    assert first.retained_bytes == second.retained_bytes == others.retained_bytes == 0


def test_an_exceptions_status_reference_is_charged_to_its_audit_and_let_go_with_its_status(tmp_path, monkeypatch):
    """The reference is charged to its audit's retained bytes when it is made, inside the audit's own budget (no other
    memory manager, RC-R1), and freed when its status row goes. With the audit's budget full no reference is made and
    the request is not sent: 503, as when its status cannot be reserved."""
    import base64

    import httpx
    from starlette.testclient import TestClient

    from latet.config import Config, IdentityConfig
    from latet.gateway import forward
    from latet.gateway.app import create_app
    from latet.gateway.request_audit import (
        EXCEPTION_SLOTS,
        MAX_BYTES,
        REFERENCE_BYTES,
        STATUS_BYTES,
        RequestAudit,
        new_exception_reference,
    )
    from latet.gateway.routes import Upstream
    from latet.runtime import make_runtime

    from .test_request_project_binding import _acme, _acme_chat, _Provider, _team

    audit = RequestAudit()
    base = audit.retained_bytes
    same = _exception_row("x0123456789")
    key = _used_under(audit, same)
    charged = audit.retained_bytes - base - STATUS_BYTES  # what its audit keeps beyond the status row: its reference
    assert charged > 0, "the exception's reference is not charged to its audit"
    assert _owned_audit_shape(audit) <= audit.retained_bytes
    assert _used_under(audit, same) == key and audit.retained_bytes == base + STATUS_BYTES + charged  # charged once
    for n in range(EXCEPTION_SLOTS):  # as many other exceptions as the audit has status rows: its row goes first
        _used_under(audit, _exception_row(f"x{n + 1:010x}"))
    assert key not in audit._statuses
    assert audit.retained_bytes == base + EXCEPTION_SLOTS * (STATUS_BYTES + charged)  # its reference went with it
    assert _owned_audit_shape(audit) <= audit.retained_bytes
    assert _used_under(audit, same) != key  # its next use is counted under a reference made again
    audit.close()
    assert audit.retained_bytes == 0

    def reserved(owner):
        """``owner``'s budget held by reservations, until no other is made."""
        leases, filler = [], new_exception_reference(1, 1)
        while (lease := owner.reserve_exception(filler)) is not None:
            leases.append(lease)
        return leases

    def filled(owner):
        """``owner``'s budget held whole: reservations first, then references until none is made."""
        leases, made = reserved(owner), 0
        while made < 64 and owner.reference_for(f"x{made + 1:010x}", 2) is not None:
            made += 1
        assert made < 64 and owner.retained_bytes <= MAX_BYTES
        return leases

    full = RequestAudit()
    leases = reserved(full)
    held = full.retained_bytes
    # The room this step is about: a reference fits, its status does not (review M-2). Without it, a reference that
    # cannot be made would pass the two checks below as well, and they would check nothing.
    assert MAX_BYTES - held >= REFERENCE_BYTES
    turn = forward._Turn()
    assert not forward._visible(full, turn, (same,)) and turn.reservations == []  # no room left for its status
    assert same.id not in full._references and full.retained_bytes == held  # a reference made for it is let go
    leases += filled(full)
    held = full.retained_bytes
    assert full.reference_for(same.id, 2) is None and full.retained_bytes == held  # no room left for its reference
    assert not forward._visible(full, turn, (same,)) and turn.reservations == [] and full.retained_bytes == held
    for lease in leases:
        full.abandon(lease)
    full.close()
    assert full.retained_bytes == 0

    # Requests sent under two exceptions of one identity: the first sent while its audit has room, the second (its
    # reference not made yet) refused once the audit is full, 503, nothing of it sent.
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    provider = _Provider()
    runtime = make_runtime(
        Config(identity=IdentityConfig(enabled=False), ner="off"),
        data=tmp_path / "data",
        config_file=tmp_path / "config.yaml",
    )
    try:
        runtime.gateway.upstreams = [Upstream("openai", "/openai", "https://api.openai.com")]
        runtime.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False)
        team, cost = _acme(runtime, _team(), _team("x-cost"))
        audit = runtime.request_audit
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            assert _acme_chat(client, **{"x-team": "data-science"}).status_code == 200
            assert len(provider.received) == 1 and team.id in audit._references
            leases = filled(audit)
            refused = _acme_chat(client, **{"x-cost": "research"})
            assert refused.status_code == 503 and "kept visible" in refused.text
            assert len(provider.received) == 1 and cost.id not in audit._references  # none made, nothing sent
            assert team.id in audit._references
            for lease in leases:
                audit.abandon(lease)
    finally:
        runtime.close()


def test_request_policy_keeps_no_exception_reference_of_its_own():
    """The process-wide references are gone: ``request_policy`` keeps none, and no module asks it for one."""
    import re
    from pathlib import Path

    from latet.gateway import request_policy

    gone = ("_REFERENCES", "_REFERENCES_KEPT", "reference_of")
    assert [name for name in gone if hasattr(request_policy, name)] == []
    source = Path(request_policy.__file__).resolve().parents[1]
    named = re.compile(r"\b(?:_REFERENCES|_REFERENCES_KEPT|reference_of)\b")
    assert [path.name for path in sorted(source.rglob("*.py")) if named.search(path.read_text("utf-8"))] == []


def test_only_the_audit_makes_an_exception_reference():
    """Review M-4: a reference is made by the identity's audit alone (``RequestAudit.reference_for``). A module that
    made its own and reserved under it would bring item 10's shared or split references back, unseen: no module of
    Latet's but ``request_audit`` calls ``ExceptionReference`` or ``new_exception_reference`` (the tests do)."""
    import re
    from pathlib import Path

    from latet.gateway import request_audit

    own = Path(request_audit.__file__).resolve()
    source = own.parents[1]
    made = re.compile(r"\b(?:ExceptionReference|new_exception_reference)\s*\(")
    found = [
        path.relative_to(source).as_posix()
        for path in sorted(source.rglob("*.py"))
        if path.resolve() != own and made.search(path.read_text("utf-8"))
    ]
    assert found == []


def test_a_reference_that_cannot_be_allocated_is_refunded_and_not_kept(monkeypatch):
    """Review M-1: ``reference_for``'s allocation fails (MemoryError making the reference): it answers None, counts one
    miss, keeps nothing and gives its 512 bytes back."""
    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    before, missed = audit.retained_bytes, audit.overflow

    def failed(*args):
        raise MemoryError

    with monkeypatch.context() as patch:
        patch.setattr(module, "ExceptionReference", failed)
        assert audit.reference_for("x0123456789", 2) is None
    assert audit.retained_bytes == before and audit._references == {} and audit.overflow == missed + 1
    assert audit.partial and not audit.protects_idle
    assert audit.reference_for("x0123456789", 2) is not None
    assert audit.retained_bytes == before + module.REFERENCE_BYTES
    audit.close()
    assert audit.retained_bytes == 0


def test_a_closed_audit_frees_at_once_a_reference_whose_row_never_came():
    """Review M-1: an audit closed while one exception's lease is active and another's reference has no row yet (made,
    not reserved): that reference goes at the close, its 512 bytes with it, while the lease and its own reference wait
    for the lease to end."""
    from latet.gateway.request_audit import REFERENCE_BYTES, RequestAudit

    audit = RequestAudit()
    lease = audit.reserve_exception(audit.reference_for("x000000000a", 2))
    assert lease is not None and audit.reference_for("x000000000b", 2) is not None
    before = audit.retained_bytes
    audit.close()
    assert set(audit._references) == {"x000000000a"} and audit.active == 1
    assert audit.retained_bytes == before - REFERENCE_BYTES
    assert audit.reference_for("x000000000b", 2) is None  # closed: none is made any more
    assert audit.abandon(lease)
    assert audit.retained_bytes == 0 and audit._references == {}


def test_audit_prepublishes_whole_delta_rolls_back_denial_and_allocation_failure(monkeypatch):
    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    observations = []

    def deny(added):
        observations.append((added, audit.retained_bytes, len(audit._events), len(audit._statuses)))
        return "full"

    audit.bind_ceiling(deny)
    assert not audit.record(_audit_record())
    assert observations == [(0, 4096 + 1024 + module.EVENT_RESERVE_BYTES, 0, 0)]
    assert audit.retained_bytes == 4096 and not audit._events and not audit._statuses
    audit.bind_ceiling(None)

    def failed_encode(record, sequence):
        assert audit.retained_bytes > 4096
        raise MemoryError

    monkeypatch.setattr(module, "_encode", failed_encode)
    assert not audit.record(_audit_record())
    assert audit.retained_bytes == 4096 and not audit._events and not audit._statuses
    assert audit.overflow == 2
    audit.close()


def test_audit_retained_read_never_takes_audit_lock_and_counters_saturate():
    import threading

    from latet.gateway.request_audit import MAX_COUNTER, RequestAudit

    audit = RequestAudit()
    ready, release = threading.Event(), threading.Event()

    def holder():
        with audit._lock:
            ready.set()
            assert release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(5)
        assert audit.retained_bytes == 4096
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert audit.record(_audit_record())
    row = audit._statuses[0]
    row.uses = row.attempts = row.answers = MAX_COUNTER - 1
    audit._sequence = audit._overflow = MAX_COUNTER - 1
    assert audit.record(_audit_record(attempts=2, answered=2))
    assert (row.uses, row.attempts, row.answers, audit.sequence) == (MAX_COUNTER,) * 4
    assert audit.saturated
    audit.bind_ceiling(lambda added: "full")
    assert not audit.record(_audit_record())
    assert not audit.record(_audit_record())
    assert audit.overflow == MAX_COUNTER and audit.saturated
    audit.close()


@pytest.mark.parametrize("owner", ["echo", "vault", "projects"])
@pytest.mark.parametrize("same_thread", [False, True])
def test_real_owner_pending_lock_rejects_audit_without_wait_or_reentrancy(audit_space, owner, same_thread):
    import threading

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    lock = {
        "echo": runtime.mediator.echoes._lock,
        "vault": runtime.built.vault._lock,
        "projects": runtime.projects._lock,
    }[owner]
    before = audit.retained_bytes
    if same_thread:
        with lock:
            assert not audit.record(_audit_record())
    else:
        ready, release = threading.Event(), threading.Event()

        def pending():
            with lock:
                ready.set()
                assert release.wait(5)

        thread = threading.Thread(target=pending)
        thread.start()
        try:
            assert ready.wait(5)
            assert not audit.record(_audit_record())
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
    assert audit.retained_bytes == before and audit.overflow == 1
    assert audit.record(_audit_record())  # every previously acquired lock released
    assert spaces._retained_past_share(space, 0) is None


@pytest.mark.parametrize("first", ["echo", "audit"])
def test_actual_echo_and_audit_growth_cannot_both_admit_against_stale_weight(audit_space, first):
    import threading

    from latet.gateway.request_audit import EVENT_RESERVE_BYTES
    from latet.server.spaces import SPACE_SHARE

    spaces, space, runtime = audit_space
    audit, echoes = runtime.request_audit, runtime.mediator.echoes
    candidate = 1024 + EVENT_RESERVE_BYTES
    spaces.budget = (space.size - space.extra + candidate) / SPACE_SHARE
    if first == "echo":
        reached, release = threading.Event(), threading.Event()
        original = runtime.retained_ceiling
        results = []

        def check_through_commit(added):
            result = original(added)
            reached.set()
            assert release.wait(5)
            return result

        runtime.retained_ceiling = check_through_commit
        thread = threading.Thread(target=lambda: results.append(echoes.reserve(candidate)))
        thread.start()
        try:
            assert reached.wait(5)  # real reserve still holds E after its check
            assert not audit.record(_audit_record())
        finally:
            release.set()
            thread.join(5)
        assert results == [True] and not thread.is_alive()
        runtime.retained_ceiling = original
        assert not audit.record(_audit_record())
        echoes.release(candidate)
    else:
        reached, release = threading.Event(), threading.Event()
        original, results = audit._ceiling, []

        def published_before_fence(added):
            reached.set()  # A held, full delta visible; no event allocation yet
            assert release.wait(5)
            return original(added)

        audit.bind_ceiling(published_before_fence)
        thread = threading.Thread(target=lambda: results.append(audit.record(_audit_record())))
        thread.start()
        try:
            assert reached.wait(5)
            assert audit.retained_bytes == 4096 + candidate and not audit._events
            assert not echoes.reserve(candidate)  # real E -> Runtime read cannot acquire A
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and results == [True]
        audit.bind_ceiling(original)
    assert spaces._retained_past_share(space, 0) is None


def test_live_audit_delta_reaches_actual_project_and_value_candidate_gates(audit_space):
    from latet.server.spaces import SPACE_SHARE

    spaces, space, runtime = audit_space
    assert runtime.request_audit.record(_audit_record())
    extra = runtime.request_retained_bytes - space.retained
    assert extra > 0
    candidate = Entity("p25-value", "PERSON", EntityClass.NER, ORIGINAL, FAKE, 0, 0.0, "ner")
    value_before = spaces._past_share(space, candidate)
    assert value_before is None
    # Bound equals the old cached fixed weight: real live Audit growth must be
    # included without needing a new _weighed, while exact equality still fits.
    from latet.engine.known import value_chars
    from latet.server.spaces import _values

    projects = runtime.projects.weight
    value_weight = (
        space.fixed
        - space.retained
        + runtime.request_retained_bytes
        + projects
        + _values(runtime.built.vault, 1, value_chars(candidate))
    )
    spaces.budget = value_weight / SPACE_SHARE
    assert spaces._past_share(space, candidate) is None
    spaces.budget = (value_weight - 1) / SPACE_SHARE
    assert spaces._past_share(space, candidate) is not None
    project_weight = space.fixed - space.retained + runtime.request_retained_bytes + _values(runtime.built.vault)
    spaces.budget = project_weight / SPACE_SHARE
    assert spaces._projects_past_share(space, 0) is None
    spaces.budget = (project_weight - 1) / SPACE_SHARE
    assert spaces._projects_past_share(space, 0) is not None


@pytest.mark.parametrize("owner", ["vault", "projects"])
@pytest.mark.parametrize("first", ["owner", "audit"])
def test_actual_value_and_project_check_through_commit_reject_second_growth(audit_space, owner, first):
    import threading

    from latet.engine.known import value_chars
    from latet.projects import _NotBuilt, masker_weight
    from latet.runtime import SpaceFull
    from latet.server.spaces import SPACE_SHARE, _values

    spaces, space, runtime = audit_space
    audit, vault, projects = runtime.request_audit, runtime.built.vault, runtime.projects
    reached, release = threading.Event(), threading.Event()
    failures, results = [], []
    original = ORIGINAL * 20
    candidate = Entity("not-used-as-authority", "P25_CUSTOM", EntityClass.CUSTOM, original, FAKE, 0, 0.0, None)
    if owner == "vault":
        growth = _values(vault, 1, value_chars(candidate)) - _values(vault)
        normal = runtime.new_values.keep

        def checked(entity):
            result = normal(entity)  # real NewValues -> Spaces check, V still held
            if first == "owner":
                reached.set()
                assert release.wait(5)
            return result

        vault._on_insert = checked

        def mutate():
            return vault.intern("P25_CUSTOM", EntityClass.CUSTOM, original, generate=lambda value, drbg: FAKE)

        refusal = SpaceFull
    else:
        growth = masker_weight(runtime.config)
        normal = projects.past_share
        calls = []

        def checked(weight):
            result = normal(weight)
            calls.append((weight, result))
            if first == "owner" and len(calls) == 2:
                reached.set()  # actual second, whole-masker check before _scope's commit
                assert release.wait(5)
            return result

        projects.past_share = checked

        def mutate():
            with projects._lock:
                return projects._scope(runtime.config, True)

        refusal = _NotBuilt
    spaces.budget = (space.size - space.extra + growth) / SPACE_SHARE
    if first == "audit":
        fence, accepted = audit._ceiling, []

        def published_before_fence(added):
            reached.set()  # real A remains held, positive delta already published
            assert release.wait(5)
            return fence(added)

        audit.bind_ceiling(published_before_fence)
        thread = threading.Thread(target=lambda: accepted.append(audit.record(_audit_record())))
        thread.start()
        try:
            assert reached.wait(5)
            assert audit.retained_bytes > 4096 and not audit._events
            with pytest.raises(refusal):
                mutate()  # real V/P candidate reads the prepublished Audit delta
            assert vault.count == 0 and not projects._scopes and not projects._masker_weights
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and accepted == [True]
        audit.bind_ceiling(fence)
    else:

        def pending():
            try:
                results.append(mutate())
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=pending)
        thread.start()
        try:
            assert reached.wait(5)
            assert vault.count == 0 and not projects._masker_weights
            assert not audit.record(_audit_record())  # positive delta rolled back; no lock wait
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not failures and len(results) == 1
        assert (vault.count == 1) if owner == "vault" else bool(projects._masker_weights)
        assert not audit.record(_audit_record())  # committed growth now visible
        assert spaces._retained_past_share(space, 0) is None


@pytest.mark.parametrize("protected", ["reservation", "busy", "approval"])
def test_real_spaces_eviction_protects_active_owner_and_releases_ended_owner(audit_space, monkeypatch, protected):
    from latet.gateway.request_audit import new_exception_reference

    spaces, space, runtime = audit_space
    closed = []
    monkeypatch.setattr(spaces, "_close_later", lambda selected, words=None: closed.extend(selected))
    reservation = None
    if protected == "reservation":
        reservation = runtime.request_audit.reserve_exception(new_exception_reference(1, 1))
        assert reservation is not None
    elif protected == "busy":
        space.busy = 1
    else:
        space.parts.approvals.holding = True
    spaces._weighed(space)
    spaces.budget = space.size - 1
    spaces._fit(keep=None)
    assert spaces._spaces.get(space.identity.id) is space and not closed
    if reservation is not None:
        runtime.request_audit.abandon(reservation)
    space.busy = 0
    space.parts.approvals.holding = False
    spaces.budget = 1
    spaces._fit(keep=None)
    assert space.identity.id not in spaces._spaces and closed == [space]


async def test_cancelled_actual_mediator_worker_keeps_reservation_until_worker_finally(audit_space):
    import threading

    from latet.gateway.request_audit import new_exception_reference

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    reservation = audit.reserve_exception(new_exception_reference(1, 1))
    assert reservation is not None
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def work():
        entered.set()
        try:
            assert release.wait(5)
        finally:
            audit.abandon(reservation)
            ended.set()

    task = asyncio.create_task(runtime.mediator.run(work))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        before = audit.retained_bytes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert audit.active == 1 and audit.retained_bytes == before
        runtime.close()  # actual Mediator shutdown(wait=False) does not end this work
        assert audit.active == 1 and audit.retained_bytes > 0
    finally:
        release.set()
        assert await asyncio.to_thread(ended.wait, 5)
        if not task.done():
            await task
    assert audit.retained_bytes == 0 and audit.active == 0 and reservation._owner is None


def test_maximum_finite_event_encoding_and_compact_sparse_containers_fit_precharge(monkeypatch):
    import sys

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    original = module.json.dumps
    observed = []

    def encoded(value, **kwargs):
        text = original(value, **kwargs)

        def shape(v):
            if type(v) is tuple:
                return sys.getsizeof(v) + sum(shape(x) for x in v)
            return sys.getsizeof(v)

        # Conservative duplicate input scalar accounting plus actual Unicode
        # output and future ASCII allocation, during the full event reservation.
        transient = shape(value) + sys.getsizeof(text) + sys.getsizeof(text.encode("ascii"))
        observed.append((_owned_audit_shape(audit) + transient, audit.retained_bytes))
        return text

    monkeypatch.setattr(module.json, "dumps", encoded)
    record = _audit_record(
        attempts=2,
        answered=2,
        status=599,
        revision=module.MAX_COUNTER,
        reason=31,
        protocol=7,
        method=9,
        route=8,
        profile=4,
        inspected=65536,
        checked=65536,
        removed=65536,
        authorized=65536,
        opaque=65536,
        rules=tuple((code, 1) for code in (*range(1, 7), 8)),  # 7 retired (legacy-0.10), 8 exceptions
    )
    for _ in range(1024):
        assert audit.record(record)
    assert observed and all(native <= charged for native, charged in observed)
    assert _owned_audit_shape(audit) <= audit.retained_bytes <= module.MAX_BYTES
    # Make the dictionary sparse with actual ended evictions, checking retained
    # capacity after every deletion rather than measuring only a full table.
    for slot in list(audit._events)[:-1]:
        audit._discard(slot)
        assert _owned_audit_shape(audit) <= audit.retained_bytes
    audit.close()
    assert audit.retained_bytes == 0


def test_failed_reservation_allocation_and_failed_finalization_preserve_ownership(monkeypatch):
    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    reference = audit.reference_for("x0123456789", 1)
    assert reference is not None
    entry = module._Entry

    def failed_entry(*args):
        raise MemoryError

    with monkeypatch.context() as patch:
        patch.setattr(module, "_Entry", failed_entry)
        assert audit.reserve_exception(reference) is None
    assert audit.retained_bytes == 4096 and audit.active == 0
    assert not audit._statuses and not audit._events
    assert not audit._references  # no row was made for it: its reference went too (review M-1)
    reservation = audit.reserve_exception(audit.reference_for("x0123456789", 1))
    assert reservation is not None
    before = audit.retained_bytes

    def failed_encode(*args):
        raise MemoryError

    with monkeypatch.context() as patch:
        patch.setattr(module, "_encode", failed_encode)
        assert not audit.finalize(reservation, _audit_record())
    assert audit.active == 1 and audit.retained_bytes == before and reservation._owner is audit
    assert audit.finalize(reservation, _audit_record())
    assert reservation._owner is None and not audit.abandon(reservation)
    assert type(next(iter(audit._events.values()))) is entry
    audit.close()


@pytest.mark.parametrize("operation", ["record", "reservation"])
@pytest.mark.parametrize("completion", ["allowed", "denied"])
def test_actual_idle_selection_keeps_positive_pending_audit_until_commit_or_rollback(
    audit_space,
    monkeypatch,
    operation,
    completion,
):
    import threading

    from latet.gateway.request_audit import Reservation, new_exception_reference

    spaces, space, runtime = audit_space
    audit, fence = runtime.request_audit, runtime.request_audit._ceiling
    before, budget = audit.retained_bytes, spaces.budget
    selected, results, failures = [], [], []
    reached, release = threading.Event(), threading.Event()
    reference = new_exception_reference(1, 1)
    monkeypatch.setattr(spaces, "_close_later", lambda rows, words=None: selected.extend(rows))

    def pending(added):
        reached.set()  # real _precharge has already published; A is still held
        assert release.wait(5)
        refused = fence(added)  # same real V/P/E fence and third ceiling
        return refused if refused is not None else ("full" if completion == "denied" else None)

    audit.bind_ceiling(pending)

    def produce():
        try:
            results.append(
                audit.record(_audit_record()) if operation == "record" else audit.reserve_exception(reference)
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=produce)
    thread.start()
    retained_during_selection = None
    protected = False
    try:
        assert reached.wait(5)
        retained_during_selection = audit.retained_bytes
        assert retained_during_selection > before and audit.active == 0
        assert not audit._events and not audit._statuses  # actual preallocation boundary
        spaces.budget = 1
        spaces._fit(keep=None)  # actual weighing/selection, no replaced accounting
        protected = spaces._spaces.get(space.identity.id) is space and not selected
    finally:
        spaces.budget = budget
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        audit.bind_ceiling(fence)
    assert not failures and len(results) == 1
    if completion == "denied":
        assert results == ([False] if operation == "record" else [None])
        assert audit.retained_bytes == before and audit.active == 0
        assert not audit._events and not audit._statuses and audit.overflow == 1
    elif operation == "record":
        assert results == [True] and audit.active == 0 and len(audit._events) == 1
    else:
        assert type(results[0]) is Reservation and audit.active == 1
        assert audit.abandon(results[0]) and audit.active == 0
    # Ended/denied work must no longer pin an idle space. Deferring close is the
    # existing allowed observation boundary; normal Runtime.close is still real.
    spaces.budget = 1
    spaces._fit(keep=None)
    assert space.identity.id not in spaces._spaces and selected == [space]
    runtime.close()
    assert audit.retained_bytes == 0
    assert protected, (
        f"actual Spaces._fit removed {operation} during positive precharge "
        f"({retained_during_selection} bytes), before {completion} finalization"
    )


@pytest.mark.parametrize("operation", ["record", "reservation"])
def test_actual_idle_selection_seals_new_admissions_before_deferred_runtime_close(
    audit_space,
    monkeypatch,
    operation,
):
    from latet.gateway.request_audit import (
        EVENT_RESERVE_BYTES,
        RESERVATION_BYTES,
        STATUS_BYTES,
        Reservation,
        new_exception_reference,
    )

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    selected = []
    monkeypatch.setattr(spaces, "_close_later", lambda rows, words=None: selected.extend(rows))
    before, fitting_budget = audit.retained_bytes, spaces.budget
    candidate = STATUS_BYTES + EVENT_RESERVE_BYTES + RESERVATION_BYTES
    assert spaces._retained_past_share(space, candidate) is None
    spaces.budget = 1
    spaces._fit(keep=None)
    assert space.identity.id not in spaces._spaces and selected == [space]
    assert before == audit.retained_bytes == 4096  # asynchronous Runtime.close deliberately deferred
    spaces.budget = fitting_budget
    assert spaces._retained_past_share(space, candidate) is None
    result = (
        audit.record(_audit_record())
        if operation == "record"
        else audit.reserve_exception(new_exception_reference(1, 1))
    )
    refused = result is False if operation == "record" else result is None
    # Clean the genuinely reached reservation before the final causal assertion,
    # so a RED cannot leave an active owner behind.
    if type(result) is Reservation:
        assert audit.abandon(result)
    runtime.close()
    assert audit.retained_bytes == 0 and audit.active == 0
    assert refused, "idle-selected runtime admitted new Audit ownership before its deferred close"


@pytest.mark.parametrize("field", ["inspected", "checked", "removed", "authorized", "opaque"])
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        (100000, True),
        ((1 << 63) - 1, True),
        (True, False),
        (-1, False),
        (1 << 63, False),
    ],
)
def test_event_component_counters_accept_full_finite_range_without_arbitrary_65536_cap(field, value, accepted):
    import json

    from latet.gateway.request_audit import MAX_EVENT_BYTES, RequestAudit

    audit = RequestAudit()
    try:
        result = audit.record(_audit_record(**{field: value}))
        if accepted:
            assert result, f"finite {field}={value} was rejected"
            [entry] = audit._events.values()
            assert len(entry.payload) <= MAX_EVENT_BYTES
            decoded = json.loads(entry.payload)
            assert decoded[11 + ("inspected", "checked", "removed", "authorized", "opaque").index(field)] == value
            assert not audit.overflow
        else:
            assert not result and not audit._events and not audit._statuses
            assert audit.retained_bytes == 4096 and audit.overflow == 1
    finally:
        audit.close()
    assert audit.retained_bytes == 0


def test_all_maximum_component_counters_fit_actual_native_event_encoding_precharge(monkeypatch):
    import sys

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    original, native = module.json.dumps, []

    def encoded(value, **kwargs):
        text = original(value, **kwargs)

        def shape(v):
            return sys.getsizeof(v) + (sum(shape(item) for item in v) if type(v) is tuple else 0)

        # Same conservative actual held input/output accounting as the first
        # native witness, now with every component at the approved 63-bit max.
        native.append(
            (
                _owned_audit_shape(audit) + shape(value) + sys.getsizeof(text) + sys.getsizeof(text.encode("ascii")),
                audit.retained_bytes,
            )
        )
        return text

    monkeypatch.setattr(module.json, "dumps", encoded)
    record = _audit_record(
        **{field: module.MAX_COUNTER for field in ("inspected", "checked", "removed", "authorized", "opaque")}
    )
    try:
        assert audit.record(record), "all finite MAX_COUNTER components must be admitted"
        assert native and all(held <= charged for held, charged in native)
        [entry] = audit._events.values()
        assert len(entry.payload) <= module.MAX_EVENT_BYTES
        assert _owned_audit_shape(audit) <= audit.retained_bytes <= module.MAX_BYTES
    finally:
        audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize("operation", ["record", "reservation"])
def test_actual_idle_retirement_arbitrates_worker_started_after_false_protection_read(
    audit_space,
    monkeypatch,
    operation,
):
    import threading

    from latet.gateway.request_audit import Reservation, new_exception_reference
    from latet.server import spaces as spaces_module

    spaces, space, runtime = audit_space
    audit, budget = runtime.request_audit, spaces.budget
    fence, protection_read = audit._ceiling, spaces_module._audit_active
    reached, release = threading.Event(), threading.Event()
    selected, results, failures = [], [], []
    reference = new_exception_reference(1, 1)
    monkeypatch.setattr(spaces, "_close_later", lambda rows, words=None: selected.extend(rows))

    def pending(added):
        reached.set()  # actual positive publication, with genuine A still held
        assert release.wait(5)
        return fence(added)

    audit.bind_ceiling(pending)

    def produce():
        try:
            results.append(
                audit.record(_audit_record()) if operation == "record" else audit.reserve_exception(reference)
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=produce)
    started = False

    def observe_then_begin(candidate):
        nonlocal started
        observed = protection_read(candidate)
        assert observed is False  # real idle observation, before the producer starts
        started = True
        thread.start()
        assert reached.wait(5)
        assert audit.retained_bytes > 4096 and audit.active == 0
        return observed  # preserve the actual read, made stale by the deterministic interleaving

    monkeypatch.setattr(spaces_module, "_audit_active", observe_then_begin)
    protected = False
    try:
        spaces.budget = 1
        spaces._fit(keep=None)  # actual next step must nonblockingly arbitrate A
        protected = spaces._spaces.get(space.identity.id) is space and not selected
    finally:
        monkeypatch.setattr(spaces_module, "_audit_active", protection_read)
        spaces.budget = budget
        release.set()
        if started:
            thread.join(5)
            assert not thread.is_alive()
        audit.bind_ceiling(fence)
    assert started and not failures and len(results) == 1
    if operation == "record":
        assert results == [True]
    else:
        assert type(results[0]) is Reservation
        assert audit.abandon(results[0])
    spaces.budget = 1
    spaces._fit(keep=None)
    assert space.identity.id not in spaces._spaces and selected == [space]
    runtime.close()
    assert audit.retained_bytes == 0
    assert protected, "actual idle retirement ignored A acquired after its earlier false protection read"


def test_snapshot_work_reserves_same_budget_once_and_keeps_local_numeric_status(audit_space):
    from latet.gateway.request_audit import SNAPSHOT_WORK_BYTES

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    assert audit.record(_audit_record())
    before = audit.retained_bytes
    work = audit.reserve_snapshot()
    assert work is not None and SNAPSHOT_WORK_BYTES == 768 * 1024
    assert audit.retained_bytes == before + SNAPSHOT_WORK_BYTES
    assert audit.active == 0 and audit.protects_idle
    assert audit.reserve_snapshot() is None
    assert work.build(removed_headers=7)
    status = json.loads(work.encoded)
    assert status["schema"] == 1 and status["local"]["removed_headers"] == 7
    assert status["local"]["counts"] == [[0, 0, 0, 1, 1, 1, 0]]
    assert status["server"] is None and status["local"]["reset"] is True
    assert TOOL not in work.encoded.decode() and len(work.encoded) <= 32768
    work.close()
    work.close()
    assert audit.retained_bytes == before and not audit.protects_idle
    assert work.encoded is None and work._projection is None and work._owner is None
    spaces._weighed(space)


def test_snapshot_unavailable_never_allocates_or_publishes_uncharged_work(audit_space):
    from latet.gateway.request_audit import new_exception_reference

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    reservations = []
    reference = new_exception_reference(1, 1)
    try:
        while (reservation := audit.reserve_exception(reference)) is not None:
            reservations.append(reservation)
        before = audit.retained_bytes
        assert audit.reserve_snapshot() is None and audit._snapshot is None
        assert audit.retained_bytes == before and audit.active == len(reservations)
    finally:
        for reservation in reservations:
            audit.abandon(reservation)
    audit.bind_ceiling(lambda added: "full")
    before = audit.retained_bytes
    assert audit.reserve_snapshot() is None and audit._snapshot is None
    assert audit.retained_bytes == before


def test_snapshot_full_schema_native_raw_decoded_output_and_integer_shapes_fit(monkeypatch):
    import sys
    import tracemalloc

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    for _ in range(128):
        reservation = audit.reserve_exception(module.new_exception_reference(module.MAX_COUNTER, 7))
        assert reservation is not None and audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for row in audit._statuses.values():
        row.uses = row.attempts = row.answers = module.MAX_COUNTER
    audit._sequence = audit._overflow = module.MAX_COUNTER
    _owned_audit_shape(audit)  # load exact helper imports before allocation tracing
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    work = audit.reserve_snapshot()
    assert work is not None
    observed = []
    original = module.json.dumps

    def encoded(value, **kwargs):
        text = original(value, **kwargs)
        observed.append(
            (
                _owned_audit_shape(audit) + sys.getsizeof(text) + sys.getsizeof(text.encode("ascii")),
                audit.retained_bytes,
            )
        )
        return text

    monkeypatch.setattr(module.json, "dumps", encoded)
    try:
        assert work.build(removed_headers=module.MAX_COUNTER)
        assert observed and all(native <= charged for native, charged in observed)
        assert len(work._projection["local"]["counts"]) == 136 and len(work.encoded) <= 32768
        assert _owned_audit_shape(audit) <= audit.retained_bytes <= module.MAX_BYTES
        decoded = work.decode()
        assert decoded is work._decoded and len(decoded["local"]["counts"]) == 136
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        # Final encoded output is a separate owned stage, not a detached return.
        assert work.encode_output(decoded)
        assert work.output is not None and len(work.output) <= 32768
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        del decoded
        held = _owned_audit_shape(audit)
        current, peak = tracemalloc.get_traced_memory()
        assert held + max(0, peak - current) <= audit.retained_bytes
    finally:
        tracemalloc.stop()
        work.close()
        audit.close()
    assert audit.retained_bytes == 0


def test_snapshot_protects_real_idle_selection_and_close_until_consumer_ends(audit_space, monkeypatch):
    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    work = audit.reserve_snapshot()
    assert work is not None and work.build()
    selected = []
    monkeypatch.setattr(spaces, "_close_later", lambda rows, words=None: selected.extend(rows))
    spaces.budget = 1
    spaces._fit(keep=None)
    assert spaces._spaces.get(space.identity.id) is space and not selected
    assert audit.active == 0 and audit.protects_idle and not audit.try_retire_if_idle()
    before = audit.retained_bytes
    runtime.close()
    assert audit.retained_bytes > 0 and audit.protects_idle and audit.reserve_snapshot() is None
    assert work.encoded is not None  # returned lease content still genuinely owned
    work.close()
    assert audit.retained_bytes == 0 and not audit.protects_idle
    assert before > 4096


@pytest.mark.parametrize("stage", ["queued", "running"])
async def test_cancelled_real_snapshot_worker_keeps_work_until_native_future_done(audit_space, stage):
    import threading

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    blocker = None
    if stage == "queued":

        def blocking():
            entered.set()
            assert release.wait(5)

        blocker = runtime.mediator.executor.submit(blocking)
        assert await asyncio.to_thread(entered.wait, 5)

    def build():
        result = work.build()
        if stage == "running":
            entered.set()
            assert release.wait(5)
        return result  # never detach raw status/output bytes into a Future

    future = runtime.mediator.executor.submit(build)

    def finished(completed):
        work.worker_finished()  # actual concurrent Future terminal callback
        done.set()

    future.add_done_callback(finished)

    async def await_result():
        return await asyncio.shield(asyncio.wrap_future(future))

    waiter = asyncio.create_task(await_result())
    try:
        if stage == "running":
            assert await asyncio.to_thread(entered.wait, 5)
        else:
            assert not future.running() and not future.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        work.close()  # cancellation ends the waiter, not queued/running ownership
        before = audit.retained_bytes
        runtime.close()
        assert audit.retained_bytes > 0 and audit.protects_idle and not future.done()
        assert audit.retained_bytes <= before
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 5)
        if blocker is not None:
            blocker.result(timeout=5)
        future.result(timeout=5)
    assert audit.retained_bytes == 0 and not audit.protects_idle
    assert work._owner is None and work.encoded is None and work._projection is None


def test_snapshot_encoding_failure_retains_work_until_explicit_finalizer(monkeypatch):
    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    work = audit.reserve_snapshot()
    assert work is not None
    before = audit.retained_bytes

    def failed(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(module.json, "dumps", failed)
    assert not work.build()
    assert audit.retained_bytes == before and audit.protects_idle and audit.active == 0
    work.close()
    assert audit.retained_bytes == 4096 and not audit.protects_idle
    audit.close()


def test_returned_real_future_snapshot_lease_keeps_every_alias_charged_until_consumer_close(audit_space):
    import threading

    _spaces, _space, runtime = audit_space
    done = threading.Event()
    audit = runtime.request_audit
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker()

    def build():
        assert work.build(removed_headers=11)
        return work  # transfer one owned lease, never detached raw/decoded bytes

    future = runtime.mediator.executor.submit(build)

    def finished(completed):
        work.worker_finished()
        done.set()

    future.add_done_callback(finished)
    returned = future.result(timeout=5)
    assert done.wait(5)
    assert returned is work and future.result() is returned
    assert returned.encoded is not None and audit.active == 0 and audit.protects_idle
    runtime.close()
    assert audit.retained_bytes > 0 and returned.encoded is not None
    returned.close()
    assert audit.retained_bytes == 0 and returned._owner is None
    assert future.result() is returned and future.result().encoded is None


def _status_asgi_scope(dashboard, *, authorized=True):
    from latet.api.rest import API

    sid = dashboard.sign_ins.open(dashboard._subject()) if authorized else "invalid"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": API + "/me",
        "raw_path": (API + "/me").encode(),
        "query_string": b"identity=not-an-authority",
        "root_path": "",
        "headers": [(b"cookie", (dashboard.cookie + "=" + sid).encode())],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 5283),
    }
    if authorized and dashboard.rt.gateway.on_server is not None:
        scope["latet.identity"] = dashboard.rt.gateway.on_server.identity
    return scope


async def _status_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize("server", [False, True])
async def test_authenticated_me_preserves_fields_actual_d4_and_honest_local_status(audit_space, server):
    from types import SimpleNamespace

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard
    from latet.gateway.app import OnServer

    _spaces, _space, runtime = audit_space
    if server:
        runtime.identity = SimpleNamespace(id="u_aaaaaaaaaa", name="Visible account", kind="person")
        runtime.gateway.on_server = OnServer("https://p25.invalid", "u_aaaaaaaaaa")
    dashboard = Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())
    assert runtime.request_audit.record(_audit_record())
    runtime.mediator.envelope_removed_headers = 23
    messages = []

    async def send(message):
        messages.append(message)

    await app(_status_asgi_scope(dashboard, authorized=False), _status_receive, send)
    assert messages[0]["status"] == 401 and runtime.request_audit._snapshot is None
    messages.clear()
    await app(_status_asgi_scope(dashboard), _status_receive, send)
    assert messages[0]["status"] == 200
    payload = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert set(payload) == {
        "version",
        "profile",
        "port",
        "data_dir",
        "recording",
        "tutorial",
        "server",
        "docs",
        "request_status",
    }
    assert payload["profile"] == runtime.config.profile.value and payload["port"] == runtime.gateway.port
    assert payload["data_dir"] == ("" if server else str(runtime.data))
    assert payload["recording"] is False and payload["tutorial"] is None
    status = payload["request_status"]
    assert status["schema"] == 1 and status["local"]["removed_headers"] == 23
    assert status["local"]["counts"] == [[0, 0, 0, 1, 1, 1, 0]]
    assert TOOL not in json.dumps(status)
    if server:
        assert status["server"] == {
            "available": False,
            "complete": False,
            "contributors": 0,
            "counts": None,
            "overflow": None,
            "saturated": False,
            "published": False,
            "removed_headers": None,
        }
        assert payload["server"]["identity"]["id"] == runtime.identity.id
    else:
        assert status["server"] is None and payload["server"] is None
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert runtime.request_audit._snapshot is None


@pytest.mark.parametrize("unavailable", ["busy", "large-existing-field", "submission", "rendering"])
async def test_me_unavailable_audit_preserves_success_and_all_existing_fields(audit_space, monkeypatch, unavailable):
    from pathlib import Path

    from starlette.applications import Starlette

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    held = audit.reserve_snapshot() if unavailable == "busy" else None
    if unavailable == "large-existing-field":
        runtime.data = Path("P25_EXISTING_DATA_PATH_" * 4000)
    elif unavailable == "submission":
        monkeypatch.setattr(
            runtime.mediator.executor, "submit", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(TOOL))
        )
    elif unavailable == "rendering":
        monkeypatch.setattr(rest._RequestStatusResponse, "render", lambda *args: (_ for _ in ()).throw(MemoryError()))
    dashboard = rest.Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())
    messages = []

    async def send(message):
        messages.append(message)

    try:
        await app(_status_asgi_scope(dashboard), _status_receive, send)
        assert messages[0]["status"] == 200
        payload = json.loads(b"".join(message.get("body", b"") for message in messages))
        assert payload["data_dir"] == str(runtime.data) and payload["request_status"] is None
        assert payload["profile"] == runtime.config.profile.value and payload["recording"] is False
        assert audit._snapshot is held
    finally:
        if held is not None:
            held.close()


@pytest.mark.parametrize("ending", ["normal", "cancel", "exception"])
async def test_actual_me_asgi_send_finally_owns_status_through_stall_close_and_all_endings(audit_space, ending):
    from starlette.applications import Starlette

    from latet.api.rest import Dashboard

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    dashboard = Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            assert json.loads(message["body"])["request_status"]["local"]["epoch"]
            entered.set()
            await release.wait()
            if ending == "exception":
                raise RuntimeError(TOOL)

    task = asyncio.create_task(app(_status_asgi_scope(dashboard), _status_receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        work = audit._snapshot
        assert work is not None and work._body and audit.protects_idle and audit.active == 0
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        assert not audit.try_retire_if_idle()
        runtime.close()
        assert audit.retained_bytes > 0 and audit._snapshot is work
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if ending == "exception":
                with pytest.raises(RuntimeError, match=TOOL):
                    await task
            else:
                await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert audit.retained_bytes == 0 and audit._snapshot is None
    assert work._owner is None and work._body is None and work._response is None


@pytest.mark.parametrize("stage", ["queued", "running"])
async def test_me_cancelled_waiter_keeps_actual_native_submission_until_future_ends(audit_space, monkeypatch, stage):
    import threading

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard
    from latet.gateway.request_audit import RequestAudit, SnapshotWork

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    created = asyncio.Event()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    captured = []
    reserve = RequestAudit.reserve_snapshot

    def reserving(owner):
        work = reserve(owner)
        if owner is audit:
            captured.append(work)
            created.set()
        return work

    monkeypatch.setattr(RequestAudit, "reserve_snapshot", reserving)
    original = SnapshotWork.build

    def building(work, **kwargs):
        result = original(work, **kwargs)
        if stage == "running":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(SnapshotWork, "build", building)
    blocker = None
    if stage == "queued":

        def blocked():
            entered.set()
            assert release.wait(5)

        blocker = runtime.mediator.executor.submit(blocked)
        assert await asyncio.to_thread(entered.wait, 5)
    dashboard = Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())

    async def send(message):
        raise AssertionError("cancelled waiter must not send a response")

    task = asyncio.create_task(app(_status_asgi_scope(dashboard), _status_receive, send))
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        future = work._submission.future
        future.add_done_callback(lambda completed: done.set())
        if stage == "running":
            assert await asyncio.to_thread(entered.wait, 5)
        else:
            assert not future.running() and not future.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        runtime.close()
        assert audit.retained_bytes > 0 and audit._snapshot is work and audit.protects_idle
        assert not future.done()
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 5)
        if blocker is not None:
            blocker.result(timeout=5)
        future.result(timeout=5)
        if not task.done():
            task.cancel()
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert audit.retained_bytes == 0 and work._owner is None
    assert work._submission is None and work._body is None and work._response is None


async def test_maximum_me_snapshot_future_response_and_encoding_combined_native_shape(audit_space):
    import tracemalloc
    from types import SimpleNamespace

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard
    from latet.gateway.request_audit import MAX_COUNTER, new_exception_reference

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    for _ in range(128):
        reservation = audit.reserve_exception(new_exception_reference(MAX_COUNTER, 7))
        assert reservation is not None and audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for row in audit._statuses.values():
        row.uses = row.attempts = row.answers = MAX_COUNTER
    audit._sequence = audit._overflow = runtime.mediator.envelope_removed_headers = MAX_COUNTER
    # Existing authorized me field is large but still fitting; it is not a safe
    # status field, and must survive without truncation or an endpoint refusal.
    dashboard = Dashboard(
        runtime,
        tutorial=SimpleNamespace(
            session=SimpleNamespace(me=lambda: {"chapter": "x" * 60000}), routes=lambda guarded: []
        ),
    )
    _owned_audit_shape(audit)  # warm additive helper imports before tracing
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def send(message):
        if message["type"] == "http.response.body":
            payload = json.loads(message["body"])
            assert len(payload["tutorial"]["chapter"]) == 60000
            assert len(payload["request_status"]["local"]["counts"]) == 136
            del payload
            work = audit._snapshot
            decoded = work.decode()
            assert work.encode_output(decoded)
            del decoded
            observed.append((_owned_audit_shape(audit), audit.retained_bytes))
            entered.set()
            await release.wait()

    app = Starlette(routes=dashboard.routes())
    assert not tracemalloc.is_tracing()
    baseline_owned = _owned_audit_shape(audit)
    tracemalloc.start()
    task = asyncio.create_task(app(_status_asgi_scope(dashboard), _status_receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        held = _owned_audit_shape(audit)
        current, peak = tracemalloc.get_traced_memory()
        assert held + max(0, peak - current) <= audit.retained_bytes
        assert baseline_owned + peak <= audit.retained_bytes
        assert observed and all(native <= charged for native, charged in observed)
    finally:
        tracemalloc.stop()
        release.set()
        await task
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert audit._snapshot is None


async def test_me_native_callback_registration_failure_does_not_strand_worker_claim(audit_space, monkeypatch):
    from concurrent.futures import Future

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard, _StatusTerminal

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    dashboard = Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())
    original = Future.add_done_callback
    injected = []

    def registering(future, callback):
        work = audit._snapshot
        if (
            not injected
            and work is not None
            and work._submission is not None
            and work._submission.future is future
            and type(callback) is _StatusTerminal
        ):
            injected.append((future, work))
            raise MemoryError("owned callback registration unavailable")
        return original(future, callback)  # asyncio's real completion callback still registers

    monkeypatch.setattr(Future, "add_done_callback", registering)
    messages = []

    async def send(message):
        messages.append(message)

    work = None
    try:
        await app(_status_asgi_scope(dashboard), _status_receive, send)
        assert len(injected) == 1, "must reach the product's actual native callback registration"
        future, work = injected[0]
        assert await asyncio.to_thread(future.result, 5) is None
        await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
        assert future.done() and messages[0]["status"] == 200
        assert json.loads(b"".join(message.get("body", b"") for message in messages))["request_status"] is not None
        assert audit._snapshot is None, "completed native Future and completed ASGI send must release the lease"
        assert not audit.protects_idle and work._owner is None
    finally:
        # Test cleanup follows the causal release assertion; it cannot make that
        # observation pass or replace the real product terminal callback.
        if work is None and injected:
            work = injected[0][1]
        if work is not None:
            work.worker_finished()
            work.close()


@pytest.mark.parametrize("stage", ["queued", "running"])
@pytest.mark.parametrize("consumer_exit", ["cancel", "wrap-failure"])
async def test_callback_fault_keeps_same_request_owner_until_native_terminal(
    audit_space, monkeypatch, stage, consumer_exit
):
    import threading
    from concurrent.futures import Future

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard, _StatusTerminal
    from latet.gateway.request_audit import SnapshotWork

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    entered, release = threading.Event(), threading.Event()
    injected, messages = [], []
    original_callback, original_build = Future.add_done_callback, SnapshotWork.build

    def registering(future, callback):
        work = audit._snapshot
        if (
            not injected
            and work is not None
            and work._submission is not None
            and work._submission.future is future
            and type(callback) is _StatusTerminal
        ):
            injected.append((future, work))
            raise MemoryError("owned callback registration unavailable")
        return original_callback(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", registering)

    def building(work, **kwargs):
        result = original_build(work, **kwargs)
        if stage == "running":
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(SnapshotWork, "build", building)
    blocker = None
    if stage == "queued":

        def blocked():
            entered.set()
            assert release.wait(5)

        blocker = runtime.mediator.executor.submit(blocked)
        assert await asyncio.to_thread(entered.wait, 5)
    wrap_calls = []
    if consumer_exit == "wrap-failure":
        original_wrap = asyncio.wrap_future

        def wrapping(future, **kwargs):
            if injected and future is injected[0][0] and not wrap_calls:
                wrap_calls.append(future)
                raise MemoryError("asyncio completion registration unavailable")
            return original_wrap(future, **kwargs)

        monkeypatch.setattr(asyncio, "wrap_future", wrapping)
    dashboard = Dashboard(runtime)
    app = Starlette(routes=dashboard.routes())

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(app(_status_asgi_scope(dashboard), _status_receive, send))
    future = work = None
    try:
        for _ in range(500):
            if injected:
                break
            await asyncio.sleep(0.01)
        assert len(injected) == 1
        future, work = injected[0]
        if stage == "running":
            assert await asyncio.to_thread(entered.wait, 5)
        else:
            assert not future.running()
        assert not future.done()
        if consumer_exit == "cancel":
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done(), "fault fallback must remain the actual request owner"
            task.cancel()  # a second cancellation cannot cut short native ownership
        else:
            assert wrap_calls == [future]
        await asyncio.sleep(0.02)
        assert not task.done() and not messages
        assert work._ending and audit.protects_idle and audit._snapshot is work
        runtime.close()
        assert audit.retained_bytes > 0 and work._owner is audit and not future.done()
        release.set()
        assert await asyncio.to_thread(future.result, 5) is None
        await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
        if consumer_exit == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert not messages
        else:
            await asyncio.wait_for(task, 5)
            assert messages[0]["status"] == 200
            assert json.loads(b"".join(item.get("body", b"") for item in messages))["request_status"] is None
        assert audit.retained_bytes == 0 and audit._snapshot is None and not audit.protects_idle
        assert work._owner is None and work._submission is None
    finally:
        release.set()
        if blocker is not None:
            await asyncio.to_thread(blocker.result, 5)
        if future is not None:
            await asyncio.to_thread(future.result, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        if work is not None:
            work.worker_finished()
            work.close()


def test_d2_numeric_wire_maximum_legal_schema_and_exact_large_integers():
    from latet.gateway.request_audit import MAX_COUNTER, _d2_decode, _d2_encode

    rows = [[n, 0, 0, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER] for n in range(8)]
    rows += [[f"{n:032x}", MAX_COUNTER, 7, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER] for n in range(128)]
    local = {
        "epoch": "a" * 32,
        "sequence": MAX_COUNTER,
        "counts": rows,
        "overflow": MAX_COUNTER,
        "partial": True,
        "reset": True,
        "removed_headers": MAX_COUNTER,
        "saturated": True,
    }
    payload = _d2_encode(local)
    assert payload is not None and len(payload) + 14 <= 32768
    assert len(payload) == 7604 and payload[:4] == bytes((2, 3, 8, 128))
    raw = b"0000000030000\n" + payload
    deadline, decoded = _d2_decode(raw)
    assert deadline == 30000 and decoded == local
    assert len(raw) == 7618  # actual MAXLEGAL binaryv2 schema, not padded32768
    assert decoded["counts"][-1][3] == MAX_COUNTER and decoded["sequence"] == MAX_COUNTER
    assert TOOL.encode() not in payload and b"counts" not in payload
    assert _d2_decode(b"x" * 32769) is None
    assert _d2_decode(b"0000000030000\n" + payload[:-1] + b",null]") is None


@pytest.mark.parametrize("bad", [True, -1, 1 << 63, 1.5, TOOL])
def test_d2_wire_rejects_noninteger_counter_before_encoding(bad):
    from latet.gateway.request_audit import _d2_encode

    local = {
        "epoch": "a" * 32,
        "sequence": bad,
        "counts": [],
        "overflow": 0,
        "partial": False,
        "reset": True,
        "removed_headers": 0,
        "saturated": False,
    }
    assert _d2_encode(local) is None


def test_d2_exact_fold_replaces_own_remote_and_explicitly_bounds_exception_rows():
    from latet.gateway.request_audit import MAX_COUNTER, _D2Counts

    def local(rows, **changes):
        return {
            "epoch": "a" * 32,
            "sequence": 0,
            "counts": rows,
            "overflow": 0,
            "partial": False,
            "reset": True,
            "removed_headers": 0,
            "saturated": False,
            **changes,
        }

    own = "a" * 12
    folded = _D2Counts(own, local([[1, 0, 0, (1 << 53) + 1, 1, 1, 0]], removed_headers=(1 << 53) + 1))
    assert not folded.add(
        own, local([[1, 0, 0, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER, 0]], removed_headers=MAX_COUNTER)
    )
    assert folded.add("b" * 12, local([[1, 0, 0, 7, 2, 1, 0]], removed_headers=7))
    result = folded.result(published=True, complete=True)
    assert result["contributors"] == 2 and result["counts"] == [[1, 0, 0, (1 << 53) + 8, 3, 2, 0]]
    assert result["complete"] and not result["saturated"]
    assert result["removed_headers"] == (1 << 53) + 8
    assert folded.add(
        "c" * 12, local([[1, 0, 0, MAX_COUNTER, 0, 0, 0]], overflow=MAX_COUNTER, removed_headers=MAX_COUNTER)
    )
    assert folded.add("d" * 12, local([[1, 0, 0, 1, 0, 0, 0]], overflow=1))
    result = folded.result(published=True, complete=True)
    assert result["counts"][0][3] == MAX_COUNTER and result["overflow"] == MAX_COUNTER and result["saturated"]
    assert result["removed_headers"] == MAX_COUNTER
    for n in range(128):
        assert folded.add(f"{n + 4:012x}", local([[f"{n:032x}", 1, 1, 1, 0, 0, 0]])) is (n < 60)
    result = folded.result(published=True, complete=True)
    assert result["contributors"] == 64 and not result["complete"] and result["overflow"] == MAX_COUNTER
    bounded = _D2Counts(own, local([]))
    for instance in ("b" * 12, "c" * 12):
        assert bounded.add(instance, local([[f"{instance[0]}{n:031x}", 1, 1, 1, 0, 0, 0] for n in range(128)]))
    result = bounded.result(published=True, complete=True)
    assert len(result["counts"]) == 128 and not result["complete"] and result["overflow"] >= 128


def test_d2_directory_fixed22_record_bound_and_order():
    from latet.gateway.request_audit import _d2_directory

    raw = b"".join(f"{n:012x}00000000rs".encode() for n in range(64))
    rows = _d2_directory(raw)
    assert len(raw) == 1408 and len(rows) == 64 and rows[0] == ("0" * 12, 1000)
    assert _d2_directory(raw + b"f" * 22) is None
    assert _d2_directory(raw[:22] + raw[:22]) is None
    assert _d2_directory(raw[::-1]) is None


def test_d2_full_work_raw_decoded_fold_and_combined_output_native_shape():
    import tracemalloc

    from latet.gateway.request_audit import (
        MAX_COUNTER,
        RequestAudit,
        _d2_decode,
        _d2_encode,
        _D2Counts,
        new_exception_reference,
    )

    audit = RequestAudit()
    for _ in range(128):
        reservation = audit.reserve_exception(new_exception_reference(MAX_COUNTER, 7))
        assert reservation is not None and audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for row in audit._statuses.values():
        row.uses = row.attempts = row.answers = MAX_COUNTER
    audit._sequence = audit._overflow = MAX_COUNTER
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker() and work.build(removed_headers=MAX_COUNTER, server=True)
    assert not tracemalloc.is_tracing()
    baseline = _owned_audit_shape(audit)
    tracemalloc.start()
    decoded = server = None
    try:
        work._d2_payload = _d2_encode(work._projection["local"])
        remote = {
            **work._projection["local"],
            "counts": [[*row[:6], MAX_COUNTER] for row in work._projection["local"]["counts"]],
        }
        work._d2_raw = b"0000000030000\n" + _d2_encode(remote)
        remote = None
        assert len(work._d2_raw) == 7618
        work._d2_decoded = _d2_decode(work._d2_raw)
        assert work._d2_decoded is not None
        work._d2_counts = _D2Counts("a" * 12, work._projection["local"])
        assert work._d2_counts.add("b" * 12, work._d2_decoded[1])
        server = work._d2_counts.result(published=True, complete=True)
        assert server["removed_headers"] == MAX_COUNTER and server["saturated"]
        assert work._server_projection(server) and len(work.encoded) <= 32768
        assert not work._projection["server"]["complete"]  # combined encoded bound/overflow is explicit
        assert work._projection["server"]["overflow"] == MAX_COUNTER
        decoded = work.decode()
        assert decoded is not None and work.encode_output(decoded)
        held = _owned_audit_shape(audit)
        current, peak = tracemalloc.get_traced_memory()
        assert held <= audit.retained_bytes
        assert held + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
    finally:
        decoded = server = None
        tracemalloc.stop()
        work.worker_finished()
        work.close()
        audit.close()
    assert audit.retained_bytes == 0 and work._d2_raw is None and work._d2_counts is None


def _d2_maximum_local(distinct=False):
    from latet.gateway.request_audit import MAX_COUNTER

    rows = [[n, 0, 0, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER] for n in range(8)]
    rows += [[f"{n:032x}", MAX_COUNTER, 7, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER, MAX_COUNTER] for n in range(128)]
    if distinct:
        for n, row in enumerate(rows):
            if type(row[0]) is str:
                row[1] = MAX_COUNTER - 1000 - n
            for field in range(3, 7):
                row[field] = MAX_COUNTER - 4 * n - field
    return {
        "epoch": "a" * 32,
        "sequence": MAX_COUNTER,
        "counts": rows,
        "overflow": MAX_COUNTER,
        "partial": True,
        "reset": True,
        "removed_headers": MAX_COUNTER,
        "saturated": True,
    }


def test_d2_distinct_near_max_tokens_preserve_largest_wire_and_precision():
    from latet.gateway.request_audit import _d2_decode, _d2_encode

    local = _d2_maximum_local(True)
    payload = _d2_encode(local)
    raw = b"0000000030000\n" + payload
    assert len(raw) == 7618
    assert _d2_decode(raw) == (30000, local)
    numbers = [value for row in local["counts"] for value in row[3:]]
    assert len(set(numbers)) == 136 * 4 and all(len(str(value)) == 19 for value in numbers)


@pytest.mark.parametrize("prefix", ["p" * 129, "nonascii-\u00e9", 42])
def test_d2_unsupported_prefix_stops_before_client_key_or_formatting_hooks(prefix):
    from latet.server.live import ValkeyRequestStatus

    seen = []

    class Bus:
        @property
        def prefix(self):
            seen.append("prefix")
            return prefix

        @property
        def client(self):
            raise AssertionError("unsupported prefix reached client")

        def key(self, value):
            raise AssertionError("prefix must never be reread through bus.key")

    with pytest.raises(ValueError, match="unavailable request status prefix"):
        ValkeyRequestStatus(Bus(), "u_aaaaaaaaaa", "0123456789ab")
    assert seen == ["prefix"]


def test_d2_prefix_exact_type_and_128_boundary_capture_once():
    from latet.server.live import ValkeyRequestStatus

    class Foreign(str):
        def __str__(self):
            raise AssertionError("foreign prefix formatted")

        def __len__(self):
            raise AssertionError("foreign prefix sized")

        def encode(self, *args, **kwargs):
            raise AssertionError("foreign prefix encoded")

    class Bus:
        def __init__(self, value):
            self.value, self.reads, self.scripts = value, 0, []

        @property
        def prefix(self):
            self.reads += 1
            return self.value

        @property
        def client(self):
            return self

        def register_script(self, script):
            self.scripts.append(script)
            return object()

        def key(self, value):
            raise AssertionError("captured prefix must not be reread")

    bad = Bus(Foreign("small"))
    with pytest.raises(ValueError, match="unavailable request status prefix"):
        ValkeyRequestStatus(bad, "u_aaaaaaaaaa", "0123456789ab")
    assert bad.reads == 1 and not bad.scripts
    good = Bus("p" * 128)
    transport = ValkeyRequestStatus(good, "u_aaaaaaaaaa", "0123456789ab")
    assert good.reads == 1 and len(good.scripts) == 1
    assert len(transport._keys) == 4 and max(map(len, transport._keys)) <= 173
    assert all(key.startswith("p" * 128) for key in transport._keys)
    transport._end()
    for prefix in ("latet:", "latet_t_0123456789:"):
        current = Bus(prefix)
        transport = ValkeyRequestStatus(current, "u_aaaaaaaaaa", "0123456789ab")
        assert current.reads == 1 and len(current.scripts) == 1
        assert all(key.startswith(prefix + "request-status:") for key in transport._keys)
        transport._end()


def test_d2_unavailable_keeps_exact_ack_flag_synchronized_with_encoding():
    from latet.gateway.request_audit import RequestAudit

    audit = RequestAudit()
    work = audit.reserve_snapshot()
    assert work is not None and work.build(removed_headers=7, server=True)
    try:
        assert work.decode()["server"]["published"] is False
        for published in (True, False):
            assert work._server_unavailable(published)
            value = work.decode()
            assert value["local"]["removed_headers"] == 7
            assert value["server"] == {
                "available": False,
                "complete": False,
                "contributors": 0,
                "counts": None,
                "overflow": None,
                "removed_headers": None,
                "saturated": False,
                "published": published,
            }
    finally:
        work.close()
        audit.close()


def _attach_d2_protocol_fixture(runtime, *, failure=None, maximum=False, prefix="latet_t_0123456789:"):
    """Actual transport/worker with a finite script-protocol double, never real Lua."""
    import threading
    from types import SimpleNamespace

    from latet.gateway.app import OnServer
    from latet.gateway.request_audit import _d2_encode

    runtime.identity = SimpleNamespace(id="u_aaaaaaaaaa", name="Visible account", kind="person")
    runtime.gateway.on_server = OnServer("https://p25.invalid", runtime.identity.id)
    audit = runtime.request_audit
    loop_thread = threading.get_ident()
    raw_directory = b"0000000000000000000n5c1111111111110000000n5c"
    remote = (
        _d2_maximum_local(True)
        if maximum
        else {
            "epoch": "b" * 32,
            "sequence": 2,
            "counts": [[1, 0, 0, 2, 2, 2, 0]],
            "overflow": 0,
            "partial": False,
            "reset": True,
            "removed_headers": 7,
            "saturated": False,
        }
    )
    remote_raw = b"0000000030000\n" + _d2_encode(remote)
    calls, threads = [], []

    class Script:
        def __init__(self):
            self.sha = "0" * 40

        def __call__(self, *, keys, args):
            assert threading.get_ident() != loop_thread
            assert not audit._lock._is_owned()
            assert not runtime.built.vault._lock._is_owned()
            assert not runtime.projects._lock._is_owned()
            assert not runtime.mediator.echoes._lock._is_owned()
            mode = args[0]
            calls.append(mode)
            threads.append(threading.current_thread().name)
            if mode == "p":
                if failure == "lost-ack":
                    raise ConnectionError("owned lost reply")
                return [1]
            if failure in ("read-after-ack", "lost-ack"):
                raise ConnectionError("owned unavailable read")
            if mode == "d":
                return [1, 1, raw_directory]
            assert mode == "r"
            return [1, bytes(bytearray(remote_raw))]  # distinct actual transfer-sized raw

    class Client:
        def register_script(self, source):
            if failure == "script-registration":
                raise MemoryError("owned Script construction failure")
            assert prefix != "p" * 129, "unsupported prefix reached client"
            return Script()

    bus = SimpleNamespace(prefix=prefix, client=Client())
    assert audit.bind_status(bus, runtime.identity.id, "000000000000")
    return bus, calls, threads, None


def test_bound_status_descriptor_fits_standing_base_and_stays_borrowed(audit_space):
    from types import SimpleNamespace

    from latet.gateway.request_audit import BASE_BYTES

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    bus = SimpleNamespace(prefix="latet_t_0123456789:", client=object())
    assert audit.bind_status(bus, "u_aaaaaaaaaa", "0123456789ab")
    assert audit.retained_bytes == BASE_BYTES == runtime.request_retained_bytes == 4096
    assert _owned_audit_shape(audit) <= BASE_BYTES
    spaces._weighed(space)
    assert space.retained == BASE_BYTES
    assert not audit.bind_status(bus, "u_aaaaaaaaaa", "0123456789ab")  # descriptor cannot be rebound live
    audit.close()
    assert audit.retained_bytes == 0 and audit._status_owner is None
    assert not audit.bind_status(bus, "u_aaaaaaaaaa", "0123456789ab")


@pytest.mark.parametrize(
    "failure", [None, "unsupported-prefix", "script-registration", "build", "read-after-ack", "lost-ack"]
)
async def test_authenticated_me_attaches_per_work_d2_and_preserves_ack_and_local(audit_space, monkeypatch, failure):
    from starlette.applications import Starlette

    from latet.api.rest import Dashboard

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    _bus, calls, threads, _events = _attach_d2_protocol_fixture(
        runtime,
        failure=failure,
        prefix="p" * 129 if failure == "unsupported-prefix" else "latet_t_0123456789:",
    )
    assert audit.record(_audit_record(), bucket=1)
    runtime.mediator.envelope_removed_headers = 23
    reads = []
    original_get = Mediator.__getattribute__

    def reading(mediator, name):
        if mediator is runtime.mediator and name == "envelope_removed_headers":
            reads.append(name)
        return original_get(mediator, name)

    monkeypatch.setattr(Mediator, "__getattribute__", reading)
    if failure == "build":
        from latet.gateway.request_audit import SnapshotWork

        monkeypatch.setattr(SnapshotWork, "build", lambda *args, **kwargs: False)
    app = Starlette(routes=(dashboard := Dashboard(runtime)).routes())
    messages = []

    async def send(message):
        messages.append(message)

    await app(_status_asgi_scope(dashboard, authorized=False), _status_receive, send)
    assert messages[0]["status"] == 401 and calls == []
    messages.clear()
    await app(_status_asgi_scope(dashboard), _status_receive, send)
    assert messages[0]["status"] == 200
    payload = json.loads(b"".join(item.get("body", b"") for item in messages))
    assert set(payload) == {
        "version",
        "profile",
        "port",
        "data_dir",
        "recording",
        "tutorial",
        "server",
        "docs",
        "request_status",
    }
    assert payload["server"]["identity"]["id"] == runtime.identity.id and payload["data_dir"] == ""
    status = payload["request_status"]
    assert reads == ["envelope_removed_headers"]
    if failure == "build":
        assert status is None and audit._snapshot is None and calls == []
        return
    assert status["local"]["removed_headers"] == 23 and status["local"]["counts"] == [[1, 0, 0, 1, 1, 1, 0]]
    if failure is None:
        assert status["server"]["available"] and status["server"]["complete"]
        assert status["server"]["contributors"] == 2 and status["server"]["removed_headers"] == 30
        assert status["server"]["counts"] == [[1, 0, 0, 3, 3, 3, 0]]
        assert calls == ["p", "d", "r", "d"]
    else:
        assert status["server"] == {
            "available": False,
            "complete": False,
            "contributors": 0,
            "counts": None,
            "overflow": None,
            "removed_headers": None,
            "saturated": False,
            "published": failure == "read-after-ack",
        }
        assert calls == ([] if failure in ("unsupported-prefix", "script-registration") else ["p", "d"])
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert audit._snapshot is None and TOOL not in json.dumps(status)
    assert all(name.startswith("latet-engine") for name in threads)


@pytest.mark.parametrize("ending", ["normal", "cancel", "send-error"])
async def test_attached_me_d2_transport_lives_through_actual_asgi_finally(audit_space, ending):
    from starlette.applications import Starlette

    from latet.api.rest import Dashboard

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    _bus, _calls, _threads, _events = _attach_d2_protocol_fixture(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    dashboard = Dashboard(runtime)

    async def send(message):
        if message["type"] == "http.response.body":
            status = json.loads(message["body"])["request_status"]
            assert status["server"]["available"] and status["server"]["contributors"] == 2
            entered.set()
            await release.wait()
            if ending == "send-error":
                raise RuntimeError("owned send error")

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    work = transport = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        work = audit._snapshot
        transport = work._d2_transport
        assert transport is not None and not transport._closed and work._d2_counts is not None
        assert work._submission.future.done() and audit.protects_idle and not audit.try_retire_if_idle()
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        runtime.close()
        assert work._owner is audit and audit.retained_bytes > 0
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if ending == "send-error":
                with pytest.raises(RuntimeError, match="owned send error"):
                    await task
            else:
                await task
        await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
        assert transport._closed and transport._keys == () and transport._script is transport._bus is None
        assert audit.retained_bytes == 0 and audit._snapshot is None and work._d2_transport is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("stage", ["queued", "running"])
@pytest.mark.parametrize("registration_fault", [False, True])
async def test_attached_d2_native_future_cancel_and_callback_fault_keep_ownership(
    audit_space,
    monkeypatch,
    stage,
    registration_fault,
):
    import threading
    from concurrent.futures import Future

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard
    from latet.gateway.request_audit import RequestAudit
    from latet.server.live import ValkeyRequestStatus

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    _bus, calls, _threads, _events = _attach_d2_protocol_fixture(runtime)
    entered, release = threading.Event(), threading.Event()
    created = asyncio.Event()
    captured = []
    reserve = RequestAudit.reserve_snapshot

    def reserving(owner):
        work = reserve(owner)
        if owner is audit:
            captured.append(work)
            created.set()
        return work

    monkeypatch.setattr(RequestAudit, "reserve_snapshot", reserving)
    original_call = ValkeyRequestStatus._call

    def called(transport, mode, argument):
        if mode == "p" and stage == "running":
            entered.set()
            assert release.wait(5)
        return original_call(transport, mode, argument)

    monkeypatch.setattr(ValkeyRequestStatus, "_call", called)
    original_registration = Future.add_done_callback
    faults = []

    def registering(future, callback):
        work = audit._snapshot
        if (
            registration_fault
            and not faults
            and work is not None
            and work._submission.future is future
            and callback.__module__ == "latet.api.rest"
        ):
            faults.append(future)
            raise MemoryError("owned product callback registration failure")
        return original_registration(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", registering)
    blocker = future = work = None
    if stage == "queued":
        blocker = runtime.mediator.executor.submit(lambda: (entered.set(), release.wait(5)))
        assert await asyncio.to_thread(entered.wait, 5)
    dashboard = Dashboard(runtime)
    messages = []

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        future = work._submission.future
        terminal = threading.Event()
        future.add_done_callback(lambda completed: terminal.set())
        if stage == "running":
            assert await asyncio.to_thread(entered.wait, 5)
        else:
            assert not future.running() and not future.done()
        task.cancel()
        if registration_fault:
            await asyncio.sleep(0.02)
            assert not task.done() and faults == [future]
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        runtime.close()
        assert work._owner is audit and audit.retained_bytes > 0 and not future.done()
        release.set()
        await asyncio.to_thread(future.result, 5)
        assert await asyncio.to_thread(terminal.wait, 5)
        if registration_fault:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
        assert not messages and work._owner is None and audit.retained_bytes == 0
        assert calls == ([] if stage == "queued" else ["p"])
    finally:
        release.set()
        if blocker is not None:
            await asyncio.to_thread(blocker.result, 5)
        if future is not None:
            await asyncio.to_thread(future.result, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        if work is not None:
            work.worker_finished()
            work.close()


async def test_attached_maximum_d2_me_future_asgi_and_all_representations_native_shape(audit_space):
    import tracemalloc
    from types import SimpleNamespace

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard
    from latet.gateway.request_audit import MAX_COUNTER, new_exception_reference

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    for _ in range(128):
        reservation = audit.reserve_exception(new_exception_reference(MAX_COUNTER, 7))
        assert reservation is not None and audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for row in audit._statuses.values():
        row.uses = row.attempts = row.answers = MAX_COUNTER
    audit._sequence = audit._overflow = runtime.mediator.envelope_removed_headers = MAX_COUNTER
    _bus, calls, _threads, _events = _attach_d2_protocol_fixture(runtime, maximum=True)
    dashboard = Dashboard(
        runtime,
        tutorial=SimpleNamespace(
            session=SimpleNamespace(me=lambda: {"chapter": "x" * 60000}), routes=lambda guarded: []
        ),
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            parsed = json.loads(message["body"])
            assert (
                len(parsed["tutorial"]["chapter"]) == 60000 and len(parsed["request_status"]["local"]["counts"]) == 136
            )
            assert parsed["request_status"]["server"]["contributors"] == 2
            assert parsed["request_status"]["server"]["removed_headers"] == MAX_COUNTER
            assert not parsed["request_status"]["server"]["complete"]
            del parsed
            work = audit._snapshot
            decoded = work.decode()
            assert work.encode_output(decoded)
            del decoded
            assert work._submission.future.done() and work._d2_payload is not None and work._d2_counts is not None
            assert work._d2_raw is work._d2_decoded is None  # row aliases ended before final directory read
            entered.set()
            await release.wait()

    baseline = _owned_audit_shape(audit)
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        held = _owned_audit_shape(audit)
        current, peak = tracemalloc.get_traced_memory()
        assert held + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
        assert len(audit._snapshot._body) <= 98304 and len(audit._snapshot.encoded) <= 32768
    finally:
        tracemalloc.stop()
        release.set()
        await task
    await asyncio.to_thread(runtime.mediator.executor.shutdown, wait=True)
    assert audit._snapshot is None and calls == ["p", "d", "r", "d"]


@pytest.mark.parametrize("state", ["open", "closed", "retired", "busy", "unsupported-prefix"])
async def test_actual_spaces_check_publishes_in_existing_worker_only_for_open_owner(audit_space, monkeypatch, state):
    from types import SimpleNamespace

    from latet.server import spaces as module

    spaces, space, runtime = audit_space
    audit = runtime.request_audit
    bus, calls, threads, _events = _attach_d2_protocol_fixture(
        runtime,
        prefix="p" * 129 if state == "unsupported-prefix" else "latet_t_0123456789:",
    )
    space.identity = SimpleNamespace(id=runtime.identity.id, vault_limit=None)
    spaces._spaces = {space.identity.id: space}
    spaces.server = SimpleNamespace(bus=bus, instance="000000000000", db=object(), shared=SimpleNamespace(count=0))
    state_row = module._State(True, False, 0, 0, frozenset())

    def states(db, ids):
        return {space.identity.id: state_row, "": state_row}

    monkeypatch.setattr(module, "_states", states)
    runtime.mediator.envelope_removed_headers = 11
    held = audit.reserve_snapshot() if state == "busy" else None
    if state == "closed":
        audit.close()
    elif state == "retired":
        assert audit.try_retire_if_idle()
    try:
        assert await spaces.check() == []
        spaces.kick_status()
        await _wait_sweep_disposed(spaces)
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        assert calls == (["p"] if state == "open" else [])
        assert all(name.startswith("latet-space") for name in threads)
        assert audit._snapshot is held
        if state == "open":
            assert audit.retained_bytes == 4096
    finally:
        if held is not None:
            held.close()


@pytest.mark.parametrize("registration_fault", [False, True])
async def test_beat_native_cancel_keeps_work_until_actual_future_terminal(audit_space, monkeypatch, registration_fault):
    import threading
    from concurrent.futures import Future

    from latet.gateway.request_audit import RequestAudit
    from latet.server.live import ValkeyRequestStatus

    spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    _bus, calls, threads, _events = _attach_d2_protocol_fixture(runtime)
    created = asyncio.Event()
    entered, release, terminal = threading.Event(), threading.Event(), threading.Event()
    captured, faults = [], []
    original_reserve = RequestAudit.reserve_snapshot

    def reserving(owner):
        work = original_reserve(owner)
        if owner is audit:
            captured.append(work)
            created.set()
        return work

    monkeypatch.setattr(RequestAudit, "reserve_snapshot", reserving)
    original_call = ValkeyRequestStatus._call

    def called(transport, mode, argument):
        assert mode == "p"
        entered.set()
        assert release.wait(5)
        return original_call(transport, mode, argument)

    monkeypatch.setattr(ValkeyRequestStatus, "_call", called)
    original_registration = Future.add_done_callback

    def registering(future, callback):
        work = audit._snapshot
        if (
            registration_fault
            and not faults
            and work is not None
            and work._submission.future is future
            and callback.__module__ == "latet.api.rest"
        ):
            faults.append(future)
            raise MemoryError("owned beat callback registration failure")
        return original_registration(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", registering)

    async def old_consumer_contract():
        # The old private beat waiter contract remains an API native-consumer
        # infrastructure control; the new sweep has no waiter/Future callback.
        from latet.api.rest import _status_in_pool

        owned = audit.reserve_snapshot()
        assert owned is not None
        try:
            await _status_in_pool(owned, runtime.mediator, True, spaces._executor, aggregate=False)
        finally:
            owned.close()

    task = asyncio.create_task(old_consumer_contract())
    work = future = None
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        future = work._submission.future
        future.add_done_callback(lambda completed: terminal.set())
        assert await asyncio.to_thread(entered.wait, 5)
        assert not audit.try_retire_if_idle()
        task.cancel()
        if registration_fault:
            await asyncio.sleep(0.02)
            assert not task.done() and faults == [future]
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        runtime.close()
        assert work._owner is audit and audit.retained_bytes > 0 and not future.done()
        release.set()
        await asyncio.to_thread(future.result, 5)
        assert await asyncio.to_thread(terminal.wait, 5)
        if registration_fault:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        assert calls == ["p"] and all(name.startswith("latet-space") for name in threads)
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        assert audit.retained_bytes == 0 and work._owner is None and work._d2_transport is None
    finally:
        release.set()
        if future is not None:
            await asyncio.to_thread(future.result, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        if work is not None:
            work.worker_finished()
            work.close()


@pytest.mark.parametrize(
    "malformed",
    [
        "version",
        "flags",
        "builtin-count",
        "exception-count",
        "class-count-mismatch",
        "builtin-tag",
        "exception-tag-zero",
        "exception-tag-eight",
        "sequence-topbit",
        "overflow-topbit",
        "d4-topbit",
        "builtin-counter-topbit",
        "revision-topbit",
        "revision-zero",
        "exception-counter-topbit",
        "duplicate-builtin",
        "duplicate-exception",
        "truncated",
        "trailing",
        "structural-7619",
        "normative-32769",
        "old-json",
        "foreign-bytes-type",
    ],
)
def test_d2_binary_v2_rejects_malformed_without_precision_or_shape_relaxation(malformed):
    from latet.gateway.request_audit import _d2_decode, _d2_encode

    payload = bytearray(_d2_encode(_d2_maximum_local(True)))
    first_exception = 44 + 8 * 33
    if malformed == "version":
        payload[0] = 1
    elif malformed == "flags":
        payload[1] = 4
    elif malformed == "builtin-count":
        payload[2] = 9
    elif malformed == "exception-count":
        payload[3] = 129
    elif malformed == "class-count-mismatch":
        payload[2], payload[3] = 7, 128
    elif malformed == "builtin-tag":
        payload[44] = 8
    elif malformed == "exception-tag-zero":
        payload[first_exception] = 0x80
    elif malformed == "exception-tag-eight":
        payload[first_exception] = 0x88
    elif malformed in (
        "sequence-topbit",
        "overflow-topbit",
        "d4-topbit",
        "builtin-counter-topbit",
        "revision-topbit",
        "exception-counter-topbit",
    ):
        offset = {
            "sequence-topbit": 20,
            "overflow-topbit": 28,
            "d4-topbit": 36,
            "builtin-counter-topbit": 45,
            "revision-topbit": first_exception + 17,
            "exception-counter-topbit": first_exception + 25,
        }[malformed]
        payload[offset] |= 0x80
    elif malformed == "revision-zero":
        payload[first_exception + 17 : first_exception + 25] = bytes(8)
    elif malformed == "duplicate-builtin":
        payload[77] = payload[44]
    elif malformed == "duplicate-exception":
        payload[first_exception + 57 : first_exception + 57 + 25] = payload[first_exception : first_exception + 25]
    elif malformed == "truncated":
        del payload[-1]
    elif malformed == "trailing":
        payload += b"\x00"
    raw = b"0000000030000\n" + bytes(payload)
    if malformed == "structural-7619":
        raw = b"0000000030000\n" + _d2_encode(_d2_maximum_local()) + b"\x00"
        assert len(raw) == 7619
    elif malformed == "normative-32769":
        raw = b"x" * 32769
    elif malformed == "old-json":
        raw = b'0000000030000\n[1,"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",0,0,0,0,0,[]]'
    elif malformed == "foreign-bytes-type":
        raw = bytearray(raw)
    assert _d2_decode(raw) is None


def test_d2_binary_v2_preserves_order_same_id_distinct_revision_scope_and_independent_u63():
    from latet.gateway.request_audit import MAX_COUNTER, _d2_decode, _d2_encode

    local = {
        "epoch": "01" * 16,
        "sequence": (1 << 53) + 1,
        "overflow": 0,
        "partial": False,
        "saturated": False,
        "reset": True,
        "removed_headers": MAX_COUNTER,
        "counts": [
            ["00" * 16, 1, 1, 0, 1, MAX_COUNTER, (1 << 53) + 1],
            [7, 0, 0, MAX_COUNTER, 0, 1, 0],
            ["00" * 16, 2, 1, 1, 0, 0, 0],
            ["00" * 16, 1, 2, 0, 0, 0, MAX_COUNTER],
        ],
    }
    payload = _d2_encode(local)
    assert payload[:4] == bytes((2, 0, 1, 3)) and len(payload) == 44 + 33 + 3 * 57
    assert _d2_decode(b"0000000030000\n" + payload) == (30000, local)
    empty = {**local, "counts": []}
    empty_payload = _d2_encode(empty)
    assert len(empty_payload) == 44 and len(b"0000000030000\n" + empty_payload) == 58
    assert _d2_decode(b"0000000030000\n" + empty_payload) == (30000, empty)


@pytest.mark.parametrize("ending", ["cancel", "execute"])
async def test_actual_submit_start_failure_keeps_genuinely_enqueued_work_charged(audit_space, monkeypatch, ending):
    """Actual CPython enqueue THEN Thread.start fault, never a fake submit."""
    import concurrent.futures.thread as native
    import threading

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard, _StatusInput

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    pool = runtime.mediator.executor
    assert not pool._threads and pool._work_queue.qsize() == 0
    original_start = threading.Thread.start
    captured = []

    def starting(thread):
        if thread.name == pool._thread_name_prefix + "_0" and thread._target is native._worker:
            assert not captured and pool._work_queue.qsize() == 1 and not pool._threads
            item = pool._work_queue.get_nowait()  # bounded exact identity observation
            pool._work_queue.put(item)  # restore SAME actual queued WorkItem
            work = audit._snapshot
            fn, args, kwargs = thread_pools.call(item)
            assert type(item) is native._WorkItem and type(fn) is _StatusInput and fn._work is work
            assert args == () and kwargs == {}
            assert work is not None and work._worker and work._owner is audit
            captured.append((item, work))
            raise RuntimeError("owned actual worker start fault")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", starting)
    dashboard = Dashboard(runtime)
    messages = []

    async def send(message):
        messages.append(message)

    item = work = future = None

    def finish():
        # Restoring the exact hook is required even after a failed causal assertion.
        monkeypatch.setattr(threading.Thread, "start", original_start)
        if future is not None and not future.done():
            if ending == "execute":
                pool.submit(lambda: None).result(timeout=5)  # genuine later queue execution, SAME executor
                assert future.done()
            else:
                pool.shutdown(wait=True, cancel_futures=True)  # real withdrawal/cancellation
                assert future.cancelled()

    try:
        await Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
        assert len(captured) == 1
        item, work = captured[0]
        assert messages[0]["status"] == 200
        assert json.loads(b"".join(message.get("body", b"") for message in messages))["request_status"] is None
        fn, args, kwargs = thread_pools.call(item)
        assert (
            pool._work_queue.qsize() == 1 and not item.future.done() and type(fn) is _StatusInput and fn._work is work
        )
        assert args == () and kwargs == {}
        fn = args = kwargs = None
        audit.close()
        assert pool._work_queue.qsize() == 1 and not item.future.done()  # actual queued owner still live
        assert work._owner is audit and work._worker and audit.retained_bytes > 0
        future = item.future  # minimal terminal observation, no queued arguments
        captured.clear()
        item = work = None  # ALL test borrows of actual WorkItem/args end first
        finish()
        assert future.done()
        future = None  # executed Future result may itself borrow the returned Work
        assert audit.retained_bytes == 0 and audit._snapshot is None
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_start)
        if future is None and captured:
            future = captured[0][0].future
        captured.clear()
        item = work = None
        finish()
        pool.shutdown(wait=True, cancel_futures=True)
        future = None
        # Test-only fallback cleanup AFTER causal assertion and true native
        # terminal/queue withdrawal; cannot make the observation above pass.
        fallback = audit._snapshot
        if fallback is not None:
            fallback.worker_finished()
            fallback.close()
        fallback = None


def _observe_status_lease(monkeypatch, audit):
    from latet.gateway.request_audit import RequestAudit

    created, captured = asyncio.Event(), []
    original = RequestAudit.reserve_snapshot

    def reserving(owner):
        work = original(owner)
        if owner is audit:
            captured.append(work)
            created.set()
        return work

    monkeypatch.setattr(RequestAudit, "reserve_snapshot", reserving)
    return created, captured


async def test_known_cancelled_native_future_keeps_actual_queued_input_claim(audit_space, monkeypatch):
    import threading

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    entered, release = threading.Event(), threading.Event()
    blocker = pool.submit(lambda: (entered.set(), release.wait(5)))
    assert await asyncio.to_thread(entered.wait, 5)
    created, captured = _observe_status_lease(monkeypatch, audit)
    dashboard = Dashboard(runtime)

    async def send(message):
        raise AssertionError("cancelled status consumer must not send")

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        future = work._submission.future
        assert pool._work_queue.qsize() == 1 and future.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        audit.close()
        assert future.cancelled() and not work._native and work._queued and work._worker
        assert audit._snapshot is work and audit.retained_bytes > 0 and not audit.try_retire_if_idle()
        release.set()
        await asyncio.to_thread(blocker.result, 5)
        await asyncio.to_thread(pool.submit(lambda: None).result, 5)  # worker passed actual cancelled input disposal
        assert audit.retained_bytes == 0 and work._owner is None
    finally:
        release.set()
        await asyncio.to_thread(blocker.result, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


async def test_native_terminal_callback_does_not_end_still_live_worker_input(audit_space, monkeypatch):
    import threading

    from starlette.applications import Starlette

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    _attach_d2_protocol_fixture(runtime)
    entered, release = threading.Event(), threading.Event()
    body_entered, run_body = threading.Event(), threading.Event()
    original_body = rest._read_request_status

    def body(*args, **kwargs):
        body_entered.set()
        assert run_body.wait(5)
        return original_body(*args, **kwargs)

    monkeypatch.setattr(rest, "_read_request_status", body)
    created, captured = _observe_status_lease(monkeypatch, audit)
    original = rest._StatusTerminal.__call__

    def terminal(handle, future):
        original(handle, future)
        entered.set()
        assert release.wait(5)  # real _WorkItem.run/set_result has not disposed fn

    monkeypatch.setattr(rest._StatusTerminal, "__call__", terminal)
    dashboard = rest.Dashboard(runtime)

    async def send(message):
        raise AssertionError("cancelled native observer must not send")

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        assert await asyncio.to_thread(body_entered.wait, 5)
        assert any(type(handle) is rest._StatusTerminal for handle in work._submission.future._done_callbacks)
        run_body.set()
        assert await asyncio.to_thread(entered.wait, 5)
        future = work._submission.future
        assert future.done() and future.result() is None and not work._native and work._queued
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        audit.close()
        assert work._owner is audit and work._worker and audit.retained_bytes > 0
        release.set()
        await asyncio.to_thread(pool.submit(lambda: None).result, 5)
        assert audit.retained_bytes == 0 and work._owner is None
    finally:
        run_body.set()
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


async def test_actual_initializer_failure_keeps_input_until_real_drain_disposal(audit_space, monkeypatch):
    import threading
    from concurrent.futures import Future

    from starlette.applications import Starlette

    from latet.api.rest import Dashboard

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    initializing, fail = threading.Event(), threading.Event()
    terminal, release = threading.Event(), threading.Event()

    def initializer():
        initializing.set()
        assert fail.wait(5)
        raise RuntimeError("owned fixed initializer failure")

    thread_pools.initialize_with(pool, initializer, monkeypatch)
    created, captured = _observe_status_lease(monkeypatch, audit)
    original = Future.set_exception

    def failing(future, error):
        original(future, error)
        if captured and captured[0]._submission.future is future:
            terminal.set()
            assert release.wait(5)  # actual _initializer_failed still owns drain-local item

    monkeypatch.setattr(Future, "set_exception", failing)
    dashboard, messages = Dashboard(runtime), []

    async def send(message):
        messages.append(message)

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        await asyncio.wait_for(created.wait(), 5)
        assert await asyncio.to_thread(initializing.wait, 5)
        work = captured[0]
        future = work._submission.future
        fail.set()
        assert await asyncio.to_thread(terminal.wait, 5)
        await asyncio.wait_for(task, 5)
        assert (
            messages[0]["status"] == 200
            and json.loads(b"".join(m.get("body", b"") for m in messages))["request_status"] is None
        )
        assert future.done() and type(future.exception()).__name__ == "BrokenThreadPool"
        audit.close()
        assert not work._native and work._queued and work._worker and work._owner is audit and audit.retained_bytes > 0
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        assert audit.retained_bytes == 0 and work._owner is None
    finally:
        fail.set()
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


async def test_status_body_baseexception_is_sanitized_and_leaves_no_future_input_cycle(
    audit_space, monkeypatch, caplog
):
    import threading
    import weakref

    from starlette.applications import Starlette

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    created, captured = _observe_status_lease(monkeypatch, audit)
    entered, release = asyncio.Event(), asyncio.Event()
    body_entered, fail_body = threading.Event(), threading.Event()

    class BodyFault(BaseException):
        pass

    def body(*args, **kwargs):
        body_entered.set()
        assert fail_body.wait(5)
        raise BodyFault(TOOL)

    monkeypatch.setattr(rest, "_read_request_status", body)
    dashboard, messages = rest.Dashboard(runtime), []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            entered.set()
            await release.wait()

    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    future = reference = None
    try:
        await asyncio.wait_for(created.wait(), 5)
        work = captured[0]
        reference = weakref.ref(work)
        future = work._submission.future
        assert await asyncio.to_thread(body_entered.wait, 5)
        fail_body.set()
        await asyncio.wait_for(entered.wait(), 5)
        assert future.done() and future.result() is None and future._exception is None
        assert (
            messages[0]["status"] == 200
            and json.loads(b"".join(m.get("body", b"") for m in messages))["request_status"] is None
        )
        release.set()
        await task
        await asyncio.to_thread(pool.submit(lambda: None).result, 5)
        work = None
        captured.clear()
        assert audit._snapshot is None and reference() is None
        assert TOOL not in caplog.text  # no raw traceback/content retained by native Future/log
    finally:
        fail_body.set()
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


async def test_carrier_clears_foreign_context_and_mediator_outside_a_before_q_release(audit_space):
    import contextvars
    import threading

    from latet.api.rest import _StatusInput, _StatusSubmission, _StatusTerminal

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    entered, release = threading.Event(), threading.Event()
    blocker = pool.submit(lambda: (entered.set(), release.wait(5)))
    assert await asyncio.to_thread(entered.wait, 5)
    cleared = []

    class Foreign:
        def __init__(self, name):
            self.name = name

        def __del__(self):
            cleared.append((self.name, audit._lock._is_owned(), audit.retained_bytes))

    value = contextvars.ContextVar("p25-owned-status-input")
    marker = Foreign("context")
    token = value.set(marker)
    work = audit.reserve_snapshot()
    assert work is not None
    mediator = Foreign("mediator")
    carrier = _StatusInput(work, mediator, False, True)
    value.reset(token)
    marker = mediator = None
    assert work.claim_worker(queued=True)
    carrier.arm()
    submission = _StatusSubmission()
    work._submission = submission
    future = submission.future = pool.submit(carrier)
    future.add_done_callback(_StatusTerminal(work))
    carrier = None
    work.close()
    audit.close()
    try:
        assert future.cancel()
        assert not work._native and work._queued and not cleared and audit.retained_bytes > 0
        pool.shutdown(wait=False, cancel_futures=True)  # real withdrawal; static proof covers pool-lock->A
        assert sorted(name for name, _owned, _charged in cleared) == ["context", "mediator"]
        assert all(not owned and charged > 0 for _name, owned, charged in cleared)
        assert work._owner is None and audit.retained_bytes == 0
    finally:
        carrier = marker = mediator = None
        release.set()
        await asyncio.to_thread(blocker.result, 5)
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
        work.worker_finished()
        work.close()


async def test_maximum_owned_carrier_queue_callback_and_asgi_native_graph(audit_space, monkeypatch):
    import threading
    import tracemalloc
    import weakref
    from types import SimpleNamespace

    from starlette.applications import Starlette

    from latet.api import rest
    from latet.gateway.request_audit import MAX_COUNTER, new_exception_reference

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    for _ in range(128):
        reservation = audit.reserve_exception(new_exception_reference(MAX_COUNTER, 7))
        assert reservation is not None and audit.finalize(reservation, _audit_record())
    for bucket in range(8):
        assert audit.record(_audit_record(), bucket=bucket)
    for row in audit._statuses.values():
        row.uses = row.attempts = row.answers = MAX_COUNTER
    audit._sequence = audit._overflow = runtime.mediator.envelope_removed_headers = MAX_COUNTER
    _attach_d2_protocol_fixture(runtime, maximum=True)
    references = []
    original_input = rest._StatusInput.__init__

    def constructed(carrier, *args, **kwargs):
        original_input(carrier, *args, **kwargs)
        references.append(weakref.ref(carrier))

    monkeypatch.setattr(rest._StatusInput, "__init__", constructed)
    terminal, release_terminal = threading.Event(), threading.Event()
    body_entered, run_body = threading.Event(), threading.Event()
    original_body = rest._read_request_status

    def body(*args, **kwargs):
        body_entered.set()
        assert run_body.wait(5)
        return original_body(*args, **kwargs)

    monkeypatch.setattr(rest, "_read_request_status", body)
    original_terminal = rest._StatusTerminal.__call__

    def completed(handle, future):
        original_terminal(handle, future)
        terminal.set()
        assert release_terminal.wait(5)

    monkeypatch.setattr(rest._StatusTerminal, "__call__", completed)
    entered, release = asyncio.Event(), asyncio.Event()
    dashboard = rest.Dashboard(
        runtime,
        tutorial=SimpleNamespace(
            session=SimpleNamespace(me=lambda: {"chapter": "x" * 60000}), routes=lambda guarded: []
        ),
    )

    async def send(message):
        if message["type"] == "http.response.body":
            parsed = json.loads(message["body"])
            assert (
                len(parsed["tutorial"]["chapter"]) == 60000 and parsed["request_status"]["server"]["contributors"] == 2
            )
            del parsed
            decoded = audit._snapshot.decode()
            assert audit._snapshot.encode_output(decoded)
            del decoded
            entered.set()
            await release.wait()

    baseline = _owned_audit_shape(audit)
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    task = asyncio.create_task(
        Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    )
    try:
        assert await asyncio.to_thread(body_entered.wait, 5)
        assert any(
            type(handle) is rest._StatusTerminal for handle in audit._snapshot._submission.future._done_callbacks
        )
        run_body.set()
        assert await asyncio.to_thread(terminal.wait, 5)
        assert len(references) == 1
        carrier = references[0]()
        work = audit._snapshot
        assert carrier is not None and carrier._work is work and not work._native and work._queued
        held = _owned_audit_shape(audit, carrier)
        current, peak = tracemalloc.get_traced_memory()
        assert held + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
        carrier = None  # no test queue-input borrow survives the Q-ending observation
        release_terminal.set()
        await asyncio.wait_for(entered.wait(), 5)
        held = _owned_audit_shape(audit, references[0]())
        current, peak = tracemalloc.get_traced_memory()
        assert held + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
        assert len(work._body) <= 98304 and len(work.encoded) <= 32768
    finally:
        run_body.set()
        tracemalloc.stop()
        carrier = None
        release_terminal.set()
        release.set()
        await task
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
    assert audit._snapshot is None and references[0]() is None


@pytest.mark.parametrize("failure", ["construction", "pre-enqueue", "callback-after-install"])
async def test_status_carrier_partial_construction_and_submit_rejection_end_exact_claims(
    audit_space, monkeypatch, failure
):
    from concurrent.futures import Future

    from starlette.applications import Starlette

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    if failure == "construction":
        original = rest._StatusInput.__init__

        def constructing(carrier, *args, **kwargs):
            original(carrier, *args, **kwargs)  # real unarmed input/context exists
            raise MemoryError("owned partial input construction")

        monkeypatch.setattr(rest._StatusInput, "__init__", constructing)
    elif failure == "pre-enqueue":
        pool.shutdown(wait=True)
    else:
        original = Future.add_done_callback
        injected = []

        def registering(future, callback):
            result = original(future, callback)  # callback ACTUALLY installed or ran
            if type(callback) is rest._StatusTerminal and not injected:
                injected.append(future)
                raise MemoryError("owned registration failure after installation")
            return result

        monkeypatch.setattr(Future, "add_done_callback", registering)
    dashboard, messages = rest.Dashboard(runtime), []

    async def send(message):
        messages.append(message)

    await Starlette(routes=dashboard.routes())(_status_asgi_scope(dashboard), _status_receive, send)
    await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
    assert messages[0]["status"] == 200
    status = json.loads(b"".join(m.get("body", b"") for m in messages))["request_status"]
    assert (status is None) is (failure != "callback-after-install")
    if failure == "callback-after-install":
        assert len(injected) == 1 and injected[0].result() is None
    assert audit._snapshot is None and not audit.protects_idle and audit.retained_bytes == 4096


def _producer_events(audit):
    return [json.loads(entry.payload) for entry in audit._events.values() if entry.payload is not None]


def test_prepaid_projection_scope_reserves_real_slot_close_and_idle_lifetime():
    from latet.gateway.request_audit import EVENT_RESERVE_BYTES, STATUS_BYTES, RequestAudit

    audit = RequestAudit()
    scope = audit._reserve_projection(1)
    assert scope is not None and len(audit._events) == 1 and audit.active == 0
    assert audit.retained_bytes == 4096 + EVENT_RESERVE_BYTES + STATUS_BYTES
    assert audit.protects_idle and not audit.try_retire_if_idle()
    assert audit._reserve_projection(1) is None
    assert audit.sequence == 0 and not _producer_events(audit)
    audit.close()
    assert audit.retained_bytes > 0 and not scope.commit(_audit_record())
    scope.close()
    scope.close()
    assert audit.retained_bytes == 0 and not audit._events and not audit.protects_idle


def test_prepaid_projection_commit_holds_escrow_until_projected_aliases_end():
    from latet.gateway.request_audit import EVENT_RESERVE_BYTES, STATUS_BYTES, RequestAudit

    audit = RequestAudit()
    scope = audit._reserve_projection(1)
    assert scope is not None
    record = _audit_record()
    held = audit.retained_bytes
    assert scope.commit(record)
    assert audit.retained_bytes == held == 4096 + EVENT_RESERVE_BYTES + STATUS_BYTES
    assert audit.sequence == 1 and len(_producer_events(audit)) == 1
    record = None  # projected caller/dataclass aliases end OUTSIDE A
    scope.close()
    assert audit.retained_bytes < held and not audit.protects_idle
    assert audit._statuses[1].uses == 1
    audit.close()
    assert audit.retained_bytes == 0


def test_prepaid_scope_slot_is_not_evicted_by_other_record_or_exception_admission():
    from latet.gateway.request_audit import MAX_EVENTS, RequestAudit, new_exception_reference

    audit = RequestAudit()
    scope = audit._reserve_projection(1)
    assert scope is not None
    slot = scope._slot
    for _ in range(MAX_EVENTS + 8):
        assert audit.record(_audit_record(), bucket=0)
    assert len(audit._events) == MAX_EVENTS and audit._events[slot].reservation is scope
    reservation = audit.reserve_exception(new_exception_reference(1, 1))
    assert reservation is not None and audit._events[slot].reservation is scope
    audit.abandon(reservation)
    audit.close()
    assert audit.retained_bytes > 0
    scope.close()
    assert audit.retained_bytes == 0


def test_prepaid_projection_maximum_record_json_and_failure_alias_native_shape(monkeypatch):
    import tracemalloc

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    baseline = _owned_audit_shape(audit)
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    scope = record = None
    try:
        scope = audit._reserve_projection(1)
        assert scope is not None
        record = module.SafeDecisionRecord(
            source=1,
            outcome=2,
            coverage=1,
            protocol=2,
            method=9,
            route=8,
            profile=2,
            revision=module.MAX_COUNTER,
            reason=31,
            inspected=module.MAX_COUNTER,
            checked=module.MAX_COUNTER,
            removed=module.MAX_COUNTER,
            authorized=module.MAX_COUNTER,
            opaque=module.MAX_COUNTER,
            rules=tuple((code, 1) for code in (*range(1, 7), 8)),  # 7 retired (legacy-0.10), 8 exceptions
            attempts=2,
            answered=2,
            status=599,
        )
        assert scope.commit(record)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()  # graph-inspection helper allocations are not producer-owned
        native = (
            _owned_audit_shape(audit) + __import__("sys").getsizeof(record) + __import__("sys").getsizeof(record.rules)
        )
        native += sum(__import__("sys").getsizeof(pair) for pair in record.rules)
        assert native + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
    finally:
        tracemalloc.stop()
        record = None
        if scope is not None:
            scope.close()
        audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize(
    "mode", ["response", "send-error", "no-mediator", "before-decision", "full-owner", "closed-owner"]
)
async def test_actual_http_finalizer_records_only_actual_decision_and_invocation(audit_space, monkeypatch, mode):
    import httpx

    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.routes import RouteTable

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    provider = _Provider(status=201)

    def called(request):
        if mode == "send-error":
            raise httpx.ConnectError(TOOL, request=request)
        return provider(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(called)) as client:
        forwarder = Forwarder(
            RouteTable([OPENAI]), Capture(None), client, None if mode == "no-mediator" else runtime.mediator
        )
        forwarder.request_audit = audit  # current consumer seam, no new-symbol scaffold
        if mode == "before-decision":

            async def interrupted(request, turn):
                raise RuntimeError(TOOL)

            monkeypatch.setattr(forwarder, "_handle", interrupted)
        elif mode == "full-owner":
            scope = audit._reserve_projection(1)
            assert scope is not None
        elif mode == "closed-owner":
            audit.close()
        try:
            if mode == "before-decision":
                with pytest.raises(RuntimeError, match=TOOL):
                    await forwarder.handle(_request(_chat("hello")))
            else:
                response = await forwarder.handle(_request(_chat("hello")))
                assert response.status_code == (400 if mode == "no-mediator" else 502 if mode == "send-error" else 201)
            rows = _producer_events(audit)
            if mode in ("before-decision", "full-owner", "closed-owner"):
                assert rows == []
            else:
                assert len(rows) == 1
                row = rows[0]
                assert row[2] == 1 and row[5] in (0, 2) and row[11:16] == [None] * 5
                if mode == "no-mediator":
                    assert row[3] == 3 and row[10] == 3 and row[17:20] == [0, 0, None]
                else:
                    assert row[3:5] == [1, 1] and row[8:10] == [2, 1]
                    assert row[17:20] == ([1, 0, None] if mode == "send-error" else [1, 1, 201])
            assert TOOL not in json.dumps(rows)
        finally:
            if mode == "full-owner":
                scope.close()


@pytest.mark.parametrize("refusal", ["no-route", "unread", "no-mediator"])
async def test_actual_ws_early_handshake_has_one_decision_before_local_close(audit_space, refusal):
    from latet.gateway import ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, Upstream

    from .test_request_outcomes import _Client

    _spaces, _space, runtime = audit_space
    client = _Client()
    routes = RouteTable(
        []
        if refusal == "no-route"
        else [Upstream("relay", "/relay", "https://invalid.example", refuses_unread=refusal == "unread")]
    )
    await ws.relay(
        client.socket,
        routes,
        Capture(None),
        None if refusal == "no-mediator" else runtime.mediator,
        request_audit=runtime.request_audit,
    )
    rows = _producer_events(runtime.request_audit)
    assert len(rows) == 1 and rows[0][2:5] == [2, 3, 0]
    assert rows[0][6] == 9 and rows[0][17:20] == [0, 0, None]
    assert rows[0][10] == (3 if refusal == "no-mediator" else 5)
    assert any(message["type"] == "websocket.close" for message in client.sent)


def test_actual_bridge_three_advisories_emit_with_recorder_absent_without_extra_observation(audit_space, monkeypatch):
    from starlette.testclient import TestClient

    from latet.bridge import api as module
    from latet.gateway.app import create_app

    from .test_request_observations import ENGINE, SECRET
    from .test_request_outcomes import EMAIL

    _spaces, _space, runtime = audit_space
    runtime.gateway.bridge_token = "p25-owned-token"
    runtime.gateway.recorder = None

    def forbidden(*args, **kwargs):
        raise AssertionError("audit must not construct sensitive Observation")

    monkeypatch.setattr(module, "observed", forbidden)
    monkeypatch.setattr(module.Bridge, "_record_block", forbidden)
    original_session, original_verify = module.Bridge._session, module.Bridge._verify
    session_calls, leak_walks = [], []

    def session(self, data):
        session_calls.append(data.get("session"))
        return original_session(self, data)

    class CountedLeaks(list):
        def __iter__(self):
            leak_walks.append(1)
            return super().__iter__()

    def verifying(self, *args, **kwargs):
        result, slots = original_verify(self, *args, **kwargs)
        result.leaks = CountedLeaks(result.leaks)
        return result, slots

    monkeypatch.setattr(module.Bridge, "_session", session)
    monkeypatch.setattr(module.Bridge, "_verify", verifying)
    with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
        headers = {"x-latet-token": runtime.gateway.bridge_token}
        masked = client.post(
            ENGINE + "/mask", headers=headers, json={"session": TOOL, "items": [{"text": f"contact {EMAIL}"}]}
        )
        fake = masked.json()["items"][0]["text"].removeprefix("contact ")
        assert fake and fake != EMAIL
        assert (
            client.post(
                ENGINE + "/route-check",
                headers=headers,
                json={"session": TOOL, "env": {}, "sources": {}, "entrypoint": "cli"},
            ).status_code
            == 200
        )
        outgoing = client.post(
            ENGINE + "/outbound", headers=headers, json={"session": TOOL, "tool": TOOL, "values": [fake]}
        )
        assert outgoing.status_code == 200 and outgoing.json()["found"]
        final = client.post(ENGINE + "/verify", headers=headers, json={"session": TOOL, "slots": [{"text": SECRET}]})
        assert final.status_code == 200 and final.json()["blocking"]
    rows = _producer_events(runtime.request_audit)
    assert len(rows) == 3 and [row.status for row in runtime.request_audit._events.values()] == [4, 5, 6]
    assert all(row[2:5] == [4, 4, 0] and row[6:11] == [0, 0, 0, 0, 31] for row in rows)
    assert all(row[11:16] == [None] * 5 and row[17:20] == [None, None, None] for row in rows)
    assert TOOL not in json.dumps(rows) and SECRET not in json.dumps(rows) and EMAIL not in json.dumps(rows)
    assert session_calls == [TOOL, TOOL, TOOL]  # mask/outbound/verify only; route check adds none
    assert leak_walks == [1, 1]  # existing blocks and response rendering only


@pytest.mark.parametrize("failure", ["refresh", "refused-retry", "retry-error", "second-reply"])
def test_actual_http_aws_first_reply_survives_refresh_and_retry(audit_space, monkeypatch, failure):
    import httpx

    from latet.gateway import request_transport

    from .test_request_transport import HELLO, _Aws, _bedrock, _invoke, _Provider, _route

    _spaces, _space, runtime = audit_space
    calls = []
    provider = _Provider()

    def answered(request):
        calls.append(request)
        if len(calls) == 1:
            # A genuine expired-token reply need not have status 403.
            return httpx.Response(409, headers={"x-amzn-errortype": "ExpiredTokenException"}, content=b"{}")
        if failure == "retry-error":
            raise httpx.ConnectError("owned retry failure", request=request)
        return provider(request)

    original = request_transport.validate_send

    def validating(decision, arena, envelope, policy, attempt, **kwargs):
        if failure == "refused-retry" and attempt == 2:
            return request_transport.SendRefusal("coverage-limit")
        return original(decision, arena, envelope, policy, attempt, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validating)
    aws = _Aws(fails=2 if failure == "refresh" else None)
    gateway = _bedrock(runtime.mediator, aws, answered, request_audit=runtime.request_audit)
    with gateway.client as client:
        response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
    assert (
        response.status_code == {"refresh": 400, "refused-retry": 422, "retry-error": 502, "second-reply": 200}[failure]
    )
    [row] = _producer_events(runtime.request_audit)
    assert row[2] == 1 and row[7] == 6 and row[11:16] == [None] * 5
    assert (
        row[17:20]
        == {
            "refresh": [1, 1, 409],
            "refused-retry": [1, 1, 409],
            "retry-error": [2, 1, 409],
            "second-reply": [2, 2, 200],
        }[failure]
    )
    assert len(calls) == (1 if failure in ("refresh", "refused-retry") else 2)
    assert aws.invalidated == ["work"] and aws.reads == 2
    assert row[3] == (3 if failure == "refused-retry" else 1)


class _AuditSocketProvider:
    """In-process library boundary; real relay finalization and transport guards run."""

    def __init__(self):
        import asyncio
        from types import SimpleNamespace

        from websockets.datastructures import Headers

        self.response = SimpleNamespace(headers=Headers())
        self.close_code = 1000
        self.got = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.mode = "normal"

    async def send(self, data):
        self.entered.set()
        if self.mode == "error":
            raise RuntimeError("owned send failure")
        if self.mode == "cancel":
            await self.release.wait()
        self.got.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.release.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["normal", "busy", "refused", "validate-error", "error", "cancel", "disconnect"])
async def test_actual_ws_frame_once_owner_and_unknown_execution(audit_space, monkeypatch, mode):
    import asyncio

    from latet.gateway import request_transport, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, Upstream

    from .test_request_outcomes import _Client, _until

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    client, provider = _Client(), _AuditSocketProvider()
    provider.mode = mode

    async def opened(*args, **kwargs):
        return provider

    monkeypatch.setattr(request_transport, "_NoRedirect", opened)
    real_validate = request_transport.validate_frame

    def validating(*args, **kwargs):
        if mode == "validate-error":
            raise RuntimeError("owned validation failure")
        if mode == "refused":
            return request_transport.SendRefusal("coverage-limit")
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(request_transport, "validate_frame", validating)

    class Turns:
        def __init__(self):
            self.begins = self.ends = 0

        def begin(self):
            self.begins += 1
            return "owned busy" if self.begins == 1 else None

        def end(self):
            self.ends += 1

    turns = Turns()
    if mode == "busy":
        client.socket.scope["latet.turns"] = turns
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                "https://invalid.example",
                envelope_expected=frozenset({("model", "m")}),
            )
        ]
    )
    relaying = asyncio.create_task(
        ws.relay(client.socket, routes, Capture(None), runtime.mediator, request_audit=audit)
    )
    try:
        await _until(lambda: len(_producer_events(audit)) == 1)
        [handshake] = _producer_events(audit)
        assert handshake[2:5] == [2, 1, 1] and handshake[17:20] == [1, 1, 101]
        if mode != "disconnect":
            payload = json.dumps(
                {
                    "type": "response.create",
                    "model": "m",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                }
            )
            client.incoming.put_nowait({"type": "websocket.receive", "text": payload})
            if mode == "cancel":
                await asyncio.wait_for(provider.entered.wait(), 5)
                relaying.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await relaying
            else:
                await _until(lambda: len(_producer_events(audit)) == 2)
                if mode in ("normal", "busy"):
                    client.incoming.put_nowait(
                        {"type": "websocket.receive", "text": json.dumps({"type": "response.cancel"})}
                    )
                    await _until(lambda: len(_producer_events(audit)) == 3)
        if not relaying.done():
            client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(relaying, 5)
        rows = _producer_events(audit)
        frames = rows[1:]
        assert len(frames) == (0 if mode == "disconnect" else 2 if mode in ("normal", "busy") else 1)
        assert all(row[2] == 3 and row[6] == 9 and row[11:16] == [None] * 5 and row[19] is None for row in frames)
        if mode == "busy":
            assert frames[0][3] == 3 and frames[0][10] == 9 and frames[0][17:20] == [0, 0, None]
            assert frames[1][17:20] == [1, None, None] and len(provider.got) == 1
            assert turns.begins == 2 and turns.ends == 1
        elif mode == "normal":
            assert all(row[3] in (1, 2) and row[17:20] == [1, None, None] for row in frames)
            assert frames[1][5] == 6 and len(provider.got) == 2
        elif frames:
            assert frames[0][17:20] == ([0, 0, None] if mode == "refused" else [None, None, None])
            assert provider.got == []
        assert provider.closed
        assert not any(
            task.get_name() in ("latet-ws-client", "latet-ws-upstream") and not task.done()
            for task in asyncio.all_tasks()
        )
    finally:
        provider.release.set()
        if not relaying.done():
            relaying.cancel()
            await asyncio.gather(relaying, return_exceptions=True)


@pytest.mark.parametrize("mode", ["invalid-status", "generic-error", "cancel", "accept-error"])
async def test_actual_ws_handshake_facts_precede_local_accept_and_errors(audit_space, monkeypatch, mode):
    import asyncio

    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from latet.gateway import request_transport, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, Upstream

    from .test_request_outcomes import _Client

    _spaces, _space, runtime = audit_space
    client, provider = _Client(), _AuditSocketProvider()
    if mode == "invalid-status":
        client.socket.scope["extensions"] = {"websocket.http.response": {}}

    async def opened(*args, **kwargs):
        if mode == "invalid-status":
            raise InvalidStatus(Response(429, "Too Many Requests", Headers(), b"owned refusal"))
        if mode == "generic-error":
            raise RuntimeError("owned connection failure")
        if mode == "cancel":
            raise asyncio.CancelledError
        return provider

    monkeypatch.setattr(request_transport, "_NoRedirect", opened)

    async def failed_accept(*args, **kwargs):
        assert _producer_events(runtime.request_audit)[0][17:20] == [1, 1, 101]
        raise RuntimeError("owned local accept failure")

    if mode == "accept-error":
        monkeypatch.setattr(client.socket, "accept", failed_accept)
    routes = RouteTable([Upstream("relay", "/relay", "https://invalid.example", envelope_expected=frozenset())])
    try:
        if mode in ("cancel", "accept-error"):
            with pytest.raises(asyncio.CancelledError if mode == "cancel" else RuntimeError):
                await ws.relay(
                    client.socket, routes, Capture(None), runtime.mediator, request_audit=runtime.request_audit
                )
        else:
            await ws.relay(client.socket, routes, Capture(None), runtime.mediator, request_audit=runtime.request_audit)
        [row] = _producer_events(runtime.request_audit)
        assert row[2] == 2 and row[3] in (1, 2)
        assert row[17:20] == (
            [1, 1, 429] if mode == "invalid-status" else [1, 1, 101] if mode == "accept-error" else [None, None, None]
        )
        if mode == "invalid-status":
            starts = [message for message in client.sent if message["type"] == "websocket.http.response.start"]
            bodies = [message for message in client.sent if message["type"] == "websocket.http.response.body"]
            assert len(starts) == 1 and starts[0]["status"] == 429
            assert bodies and b"".join(message.get("body", b"") for message in bodies) == b"owned refusal"
            assert not bodies[-1].get("more_body", False)
    finally:
        await provider.close()


def test_prepaid_projection_denial_full_population_and_same_owner(audit_space):
    from latet.gateway.request_audit import emit_advisory, new_exception_reference

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    assert runtime.gateway.request_audit is audit
    audit.bind_ceiling(lambda added: "owned finite denial")
    assert audit._reserve_projection(1) is None and audit.retained_bytes == 4096 and not audit.protects_idle
    audit.bind_ceiling(None)
    reference, leases = new_exception_reference(1, 1), []
    while (lease := audit.reserve_exception(reference)) is not None:
        leases.append(lease)
    before = audit.retained_bytes
    assert before <= 1024 * 1024 and audit._reserve_projection(1) is None
    emit_advisory(audit, 4, reason_present=False)
    assert audit.retained_bytes == before and not _producer_events(audit)
    assert audit.active == len(leases) and audit.partial
    audit.close()
    for lease in leases:
        audit.abandon(lease)
    assert audit.retained_bytes == 0


def test_finite_projection_catalogs_and_unknown_rule_loss():
    from latet.gateway import request_audit as module
    from latet.gateway import request_policy
    from latet.gateway.cloud import BedrockUpstream, VertexUpstream
    from latet.gateway.outcome import Block, Pass, PolicyEvidence, ResponsePlan
    from latet.gateway.routes import DEFAULT_UPSTREAMS, Upstream

    assert set(module._RULE_CODES) == request_policy.BUILT_IN
    assert [module.route_code(route) for route in DEFAULT_UPSTREAMS] == [1, 2, 3, 4, 5]
    assert module.route_code(BedrockUpstream()) == 6 and module.route_code(VertexUpstream()) == 7
    assert module.route_code(Upstream("openai", "/openai", "https://api.openai.com")) == 0
    assert [
        module.method_code(method)
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE")
    ] == [*range(1, 9), 0]
    audit = module.RequestAudit()
    for protocol, code in (
        ("anthropic", 1),
        ("chat", 2),
        ("responses", 3),
        ("gemini", 4),
        ("ws-cancel", 6),
        ("embeddings", 0),
    ):
        decision = Pass(
            None, b"", (), PolicyEvidence(tuple(module._RULE_CODES), protocol + ".body.1"), None, ResponsePlan(protocol)
        )
        module.emit_decision(audit, decision, source=3, method=9)
        row = _producer_events(audit)[-1]
        assert row[5] == code and row[16] == [[index, 1] for index in range(1, 7)]
        assert row[8:10] == ([code, 1] if code in (1, 2, 3, 4) else [0, 0])
    for reason, code in module._REASON_CODES.items():
        module.emit_decision(audit, Block("http", None, reason, TOOL, 400), source=1, attempts=0, answered=0)
        assert _producer_events(audit)[-1][10] == code
    before, overflow = audit.sequence, audit.overflow
    unknown = Pass(None, b"", (), PolicyEvidence(("unmapped used rule",), "chat.body.1"), None, ResponsePlan("chat"))
    module.emit_decision(audit, unknown, source=3)
    assert audit.sequence == before and audit.overflow == overflow + 1 and audit.partial
    assert not audit.protects_idle
    audit.close()


@pytest.mark.parametrize("advisory", [False, True])
@pytest.mark.parametrize("failure", ["projection", "encoding"])
def test_prepaid_projection_baseexception_releases_after_frames_end(monkeypatch, advisory, failure):
    from latet.gateway import request_audit as module
    from latet.gateway.outcome import Block

    class Interrupted(BaseException):
        pass

    audit = module.RequestAudit()
    observed = []
    error = Interrupted("owned interrupt")
    real_project = module.decision_record

    def failed_projection(*args, **kwargs):
        if failure == "projection" and not advisory:
            observed.append(audit.retained_bytes)
            raise error
        return real_project(*args, **kwargs)

    def failed_encoding(record, sequence):
        observed.append(audit.retained_bytes)
        raise error

    monkeypatch.setattr(module, "decision_record", failed_projection)
    if failure == "encoding" or advisory:
        monkeypatch.setattr(module, "_encode", failed_encoding)
    try:
        if advisory:
            with pytest.raises(Interrupted) as caught:
                module.emit_advisory(audit, 4, reason_present=False)
        else:
            with pytest.raises(Interrupted) as caught:
                module.emit_decision(audit, Block("http", None, "busy", TOOL, 429), source=1, attempts=0, answered=0)
        assert caught.value is error and observed
        assert all(held == 4096 + module.EVENT_RESERVE_BYTES + module.STATUS_BYTES for held in observed)
        # The propagated interrupt retains only the settled emitter; all frames
        # which held the projected record/rules/JSON have ended before refund.
        traceback = caught.value.__traceback__
        names = []
        while traceback is not None:
            names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert not {
            "_project_record",
            "decision_record",
            "commit",
            "_encode",
            "failed_encoding",
            "failed_projection",
        }.intersection(names)
        assert audit._projection is None and not audit.protects_idle
        assert audit.retained_bytes == 4096 and not audit._events and not audit._statuses
    finally:
        error.__traceback__ = None
        audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize("existing", [False, True])
def test_prepaid_projection_count_allocation_failure_is_atomic(monkeypatch, existing):
    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    if existing:
        assert audit.record(_audit_record(), bucket=1)
    before = (
        None if not existing else (audit._statuses[1].uses, audit._statuses[1].attempts, audit._statuses[1].answers)
    )
    scope = audit._reserve_projection(1)
    held, sequence = audit.retained_bytes, audit.sequence
    real = module.RequestAudit._count

    def failed_count(self, row, record):
        real(self, row, record)
        raise MemoryError("owned counter allocation failure")

    monkeypatch.setattr(module.RequestAudit, "_count", failed_count)
    assert not scope.commit(_audit_record())
    assert audit.retained_bytes == held and audit.sequence == sequence
    assert (1 in audit._statuses) is existing
    if existing:
        row = audit._statuses[1]
        assert (row.uses, row.attempts, row.answers) == before
    assert audit.partial and audit.overflow == 1
    scope.close()
    assert not audit.protects_idle and _owned_audit_shape(audit) <= audit.retained_bytes
    audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_prepaid_projection_full_owned_graph_totalpeak_and_chained_failure(monkeypatch, full, failed):
    import sys
    import tracemalloc

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    if full:
        for _ in range(128):
            lease = audit.reserve_exception(module.new_exception_reference(module.MAX_COUNTER, 7))
            assert lease is not None and audit.finalize(lease, _audit_record())
        for bucket in range(8):
            assert audit.record(_audit_record(), bucket=bucket)
        for _ in range(module.MAX_EVENTS):
            assert audit.record(_audit_record(), bucket=0)
        assert len(audit._events) == module.MAX_EVENTS and len(audit._statuses) == 136
    audit._sequence = module.MAX_COUNTER
    for row in audit._statuses.values():
        row.uses = int(str(module.MAX_COUNTER - 1))
        row.attempts = int(str(module.MAX_COUNTER - 2))
        row.answers = int(str(module.MAX_COUNTER - 3))
    scope = audit._reserve_projection(2)
    assert scope is not None
    # Independent integer instances exercise their real native sizes, rather
    # than sharing one MAX_COUNTER object across all five count fields.
    record = module.SafeDecisionRecord(
        source=1,
        outcome=2,
        coverage=1,
        protocol=2,
        method=9,
        route=8,
        profile=2,
        revision=int(str(module.MAX_COUNTER)),
        reason=31,
        **{name: int(str(module.MAX_COUNTER)) for name in ("inspected", "checked", "removed", "authorized", "opaque")},
        rules=tuple((code, 1) for code in (*range(1, 7), 8)),  # 7 retired (legacy-0.10), 8 exceptions
        attempts=2,
        answered=2,
        status=599,
    )
    held = audit.retained_bytes
    baseline = _owned_audit_shape(audit, record)
    original = module.json.dumps
    aliases = []
    failure = None

    def encoding(value, **kwargs):
        nonlocal failure
        text = original(value, **kwargs)
        aliases.extend((value, text, text.encode("ascii")))
        if failed:
            # Deliberately retain a real chained traceback while the escrow is
            # live. Only bounded exact internal frames/finite data are owned.
            try:
                raise ValueError("owned inner encoding failure")
            except ValueError as inner:
                try:
                    raise MemoryError("owned outer encoding failure") from inner
                except MemoryError as outer:
                    failure = outer
                    raise
        return text

    monkeypatch.setattr(module.json, "dumps", encoding)
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    try:
        accepted = scope.commit(record)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # No helper allocation contaminates TOTALpeak. Count every retained scalar,
    # rule pair, JSON input tuple, Unicode/ASCII result and scope/container; the
    # exception traceback/frame shells and their local dicts are explicit too.
    native = _owned_audit_shape(audit, record, aliases)
    if failure is not None:
        seen = set()
        pending = [failure]
        frame_bytes = 0
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            frame_bytes += sys.getsizeof(error) + sys.getsizeof(error.args)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
            if error.__context__ is not None:
                pending.append(error.__context__)
            traceback = error.__traceback__
            while traceback is not None:
                frame_bytes += sys.getsizeof(traceback) + sys.getsizeof(traceback.tb_frame)
                # f_locals values alias the already charged exact record/JSON
                # objects or borrowed test/global functions; count dict capacity.
                frame_bytes += sys.getsizeof(traceback.tb_frame.f_locals)
                traceback = traceback.tb_next
        native += frame_bytes
    assert accepted is (not failed) and audit.retained_bytes == held
    assert native + max(0, peak - current) <= held
    assert baseline + peak <= held
    assert held <= module.MAX_BYTES and len(audit._events) <= module.MAX_EVENTS
    # Test-owned exception/frame/JSON aliases end OUTSIDE A, before settlement.
    if failure is not None:
        failure.__traceback__ = failure.__context__ = failure.__cause__ = None
    error = traceback = None  # release test borrows of chained frame aliases too
    failure = record = None
    aliases.clear()
    scope.close()
    assert not audit.protects_idle and _owned_audit_shape(audit) <= audit.retained_bytes
    audit.close()
    assert audit.retained_bytes == 0


def test_prepaid_projection_chained_interrupt_detaches_only_its_owned_edges(monkeypatch):
    from latet.gateway import request_audit as module
    from latet.gateway.outcome import Block

    class Interrupted(BaseException):
        pass

    audit = module.RequestAudit()
    interrupt = Interrupted("owned interrupt")
    outer_caller = RuntimeError("caller-owned prior error")

    def encoding(record, sequence):
        private_record_alias = record
        try:
            raise ValueError("owned inner error")
        except ValueError as inner:
            assert audit.protects_idle and private_record_alias is record
            raise interrupt from inner

    monkeypatch.setattr(module, "_encode", encoding)
    try:
        try:
            raise outer_caller
        except RuntimeError:
            caller_traceback = outer_caller.__traceback__
            with pytest.raises(Interrupted) as caught:
                module.emit_decision(audit, Block("http", None, "busy", TOOL, 429), source=1)
            assert caught.value is interrupt
            assert caught.value.__cause__ is None and caught.value.__context__ is outer_caller
            assert outer_caller.__traceback__ is caller_traceback
            assert audit.retained_bytes == 4096 and audit._projection is None and not audit.protects_idle
    finally:
        interrupt.__traceback__ = None
        outer_caller.__traceback__ = None
        audit.close()


@pytest.mark.parametrize("absent", [False, True])
async def test_actual_http_audit_full_population_or_absence_never_changes_send(audit_space, absent):
    import httpx

    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.request_audit import new_exception_reference
    from latet.gateway.routes import RouteTable

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    leases, reference = [], new_exception_reference(1, 1)
    if not absent:
        while (lease := audit.reserve_exception(reference)) is not None:
            leases.append(lease)
    provider = _Provider()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        forwarder = Forwarder(
            RouteTable([OPENAI]), Capture(None), client, runtime.mediator, request_audit=None if absent else audit
        )
        response = await forwarder.handle(_request(_chat("hello")))
        assert response.status_code == 200 and len(provider.sent) == 1
        assert _producer_events(audit) == []
    for lease in leases:
        audit.abandon(lease)


@pytest.mark.parametrize("mode", ["disabled", "history-off", "failure", "contended", "closed"])
def test_actual_bridge_route_event_precedes_recorder_and_scope_failures(audit_space, monkeypatch, mode):
    from types import SimpleNamespace

    from starlette.testclient import TestClient

    from latet.bridge import api as module
    from latet.gateway.app import create_app
    from latet.gateway.recorder import Recorder

    from .test_request_observations import ENGINE

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    recorder = Recorder(
        SimpleNamespace() if mode in ("history-off", "failure") else None,
        runtime.mediator.engine,
        history="off" if mode in ("history-off", "failure") else "full",
    )
    runtime.gateway.recorder = recorder
    runtime.gateway.bridge_token = "p25-owned-bridge-token"
    session_calls, finish_calls = [], []
    original = module.Bridge._session

    def session(self, data, *, register=True):  # Bridge._session's signature: route-check reads without registering
        session_calls.append(1)
        return original(self, data, register=register)

    monkeypatch.setattr(module.Bridge, "_session", session)
    if mode == "failure":
        assert recorder.enabled and recorder.history == "off"

        def failed(outcome):
            finish_calls.append(1)
            assert len(_producer_events(audit)) == 1
            raise RuntimeError("owned recorder failure")

        monkeypatch.setattr(recorder, "finish", failed)
    scope = audit._reserve_projection(4) if mode == "contended" else None
    if mode == "closed":
        audit.close()
    try:
        with TestClient(
            create_app(runtime.gateway), base_url="http://127.0.0.1:5283", raise_server_exceptions=False
        ) as client:
            response = client.post(
                ENGINE + "/route-check",
                headers={"x-latet-token": runtime.gateway.bridge_token},
                json={"session": TOOL, "env": {}, "sources": {}, "entrypoint": "cli"},
            )
        assert response.status_code == (500 if mode == "failure" else 200)
        rows = _producer_events(audit)
        assert len(rows) == (0 if mode in ("contended", "closed") else 1)
        assert session_calls == (
            [1] if mode in ("history-off", "failure") else []
        )  # disabled Recorder is dropped at construction
        assert finish_calls == ([1] if mode == "failure" else [])
        assert all(row[2:5] == [4, 4, 0] and row[17:20] == [None, None, None] for row in rows)
        assert TOOL not in json.dumps(rows)
    finally:
        if scope is not None:
            scope.close()
        recorder.close()


async def test_actual_http_audit_interruption_keeps_original_receipt_and_answer_cleanup(audit_space, monkeypatch):
    import httpx

    from latet.gateway import forward
    from latet.gateway import request_audit as module
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    made, cleaned = [], []
    original, answered = forward._Turn, forward.Forwarder._answered

    def turn():
        made.append(original())
        return made[-1]

    def cleaned_answer(self, prepared):
        cleaned.append(prepared)
        return answered(self, prepared)

    class Interrupted(BaseException):
        pass

    def failed(record, sequence):
        raise Interrupted("owned audit interruption")

    monkeypatch.setattr(forward, "_Turn", turn)
    monkeypatch.setattr(forward.Forwarder, "_answered", cleaned_answer)
    monkeypatch.setattr(module, "_encode", failed)
    provider = _Provider()
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        forwarder = forward.Forwarder(
            RouteTable([OPENAI]), Capture(None), client, runtime.mediator, request_audit=audit
        )
        with pytest.raises(Interrupted):
            await forwarder.handle(_request(_chat("hello")))
    assert len(provider.sent) == 1 and len(made) == 1
    assert cleaned == [made[0].prepared] and made[0].renewer.cancelling()
    assert audit.retained_bytes == 4096 and audit._projection is None and not audit.protects_idle


def test_prepaid_projection_actual_concurrent_close_keeps_live_charge():
    import threading

    from latet.gateway import request_audit as module

    audit = module.RequestAudit()
    ready, release = threading.Event(), threading.Event()
    scopes, results, errors = [], [], []

    def projecting():
        scope = record = None
        try:
            scope = audit._reserve_projection(1)
            assert scope is not None
            scopes.append(scope)
            record = _audit_record()
            ready.set()
            assert release.wait(5)
            results.append(scope.commit(record))
        except BaseException as error:
            errors.append(error)
        finally:
            record = None
            if scope is not None:
                scope.close()

    worker = threading.Thread(target=projecting, name="p25-owned-projection")
    worker.start()
    try:
        assert ready.wait(5) and scopes
        assert audit.protects_idle and not audit.try_retire_if_idle() and audit.active == 0
        assert audit.record(_audit_record(), bucket=0)  # resets mutable _pending
        assert not audit._pending and audit.protects_idle
        assert audit._reserve_projection(2) is None
        audit.close()
        assert audit._events[scopes[0]._slot].reservation is scopes[0]
        assert _owned_audit_shape(audit) <= audit.retained_bytes
        assert audit.retained_bytes == module.BASE_BYTES + module.EVENT_RESERVE_BYTES + module.STATUS_BYTES
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors and results == [False]
    assert audit.retained_bytes == 0 and not audit.protects_idle and not audit._events


@pytest.mark.parametrize("held", [True, False], ids=("held-native-D2", "released-control"))
async def test_server_security_checks_finish_while_native_status_publication_is_held(audit_space, monkeypatch, held):
    """Security convergence/next scan must not await an already-owned D2 publication."""
    import threading
    import weakref
    from contextlib import AsyncExitStack, contextmanager
    from types import SimpleNamespace

    from latet.api import rest
    from latet.gateway.request_audit import BASE_BYTES, SNAPSHOT_WORK_BYTES
    from latet.logline import Paced
    from latet.server import app as server_module
    from latet.server import spaces as spaces_module

    spaces, first, runtime = audit_space
    audit = runtime.request_audit
    bus, publications, publication_threads, _events = _attach_d2_protocol_fixture(runtime)
    ids = (runtime.identity.id, "u_bbbbbbbbbb", "u_cccccccccc", "u_dddddddddd")
    first.identity = SimpleNamespace(id=ids[0], vault_limit=None)
    forgotten, cleaned, shared_forgotten, closed = [], [], [], []
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    work_references, worker_threads, scans = [], [], []
    if not held:
        release.set()

    def small_space(identity_id):
        def ended():
            closed.append(identity_id)

        def no_op():
            return None

        parts = SimpleNamespace(
            vault=SimpleNamespace(revoke_index=no_op, close=no_op),
            approvals=SimpleNamespace(close=no_op),
            approvals_feed=SimpleNamespace(close=no_op),
            feed=SimpleNamespace(close=no_op),
            conversations=SimpleNamespace(close=no_op),
        )
        local_runtime = SimpleNamespace(
            request_audit=None,
            _lock=threading.RLock(),
            close=ended,
            mediator=SimpleNamespace(approvals=SimpleNamespace(close=no_op)),
            built=SimpleNamespace(vault=None),  # a runtime's built parts: no stored vault, so no index to judge
        )
        return spaces_module.Space(
            SimpleNamespace(id=identity_id, vault_limit=None), local_runtime, None, parts, AsyncExitStack(), 0
        )

    revoked, disabled, survivor = (small_space(identity_id) for identity_id in ids[1:])
    revoked.tokens = frozenset({"p25-owned-revoked-token"})
    # The existing two-observation rule saw shared count1 once before this scan.
    assert not survivor.shared_counts.behind(1)
    spaces._spaces = dict(zip(ids, (first, revoked, disabled, survivor), strict=True))

    class Database:
        survivor_present = True

        @contextmanager
        def connection(self):
            yield self

        def execute(self, query, parameters):
            assert "FROM unnest(%s::text[])" in query
            owners = tuple(parameters[0])
            assert set(owners) <= set(ids) | {""} and owners[-1] == ""
            scans.append(owners)
            rows = []
            for owner in owners:
                rows.append(
                    (
                        owner,
                        self.survivor_present if owner == ids[3] else True,
                        owner == ids[2],
                        1 if owner == "" else 0,
                        0,
                        [],
                        None,
                    )
                )
            return SimpleNamespace(fetchall=lambda: rows)

    database = Database()
    # Bind actual Server methods without its backend-opening constructor.
    server = object.__new__(server_module.Server)
    server.db, server.bus, server.instance, server.spaces = database, bus, "000000000000", spaces
    server.shared = SimpleNamespace(count=0, forget=lambda: shared_forgotten.append(True))
    server.identities = SimpleNamespace(forget=forgotten.append)
    server._cleanups, server._cleaning = {}, None
    server._end = lambda identity_id, removed: (cleaned.append((identity_id, removed)) or True, [])
    spaces.server = server
    original = rest._read_request_status

    def status_body(work, mediator, on_server, aggregate=True):
        if work._owner is audit and not entered.is_set():
            work_references.append(weakref.ref(work))
            worker_threads.append(threading.current_thread().name)
            entered.set()
            try:
                assert release.wait(10), "owned publication gate was not released"
                return original(work, mediator, on_server, aggregate)
            finally:
                finished.set()
        return original(work, mediator, on_server, aggregate)

    monkeypatch.setattr(rest, "_read_request_status", status_body)
    jobs, failures = {}, {"check": Paced()}
    work = None
    try:
        server_module._start(jobs, failures, "check", server.check)
        assert await asyncio.to_thread(entered.wait, 5), "actual native D2 worker was not reached"
        assert worker_threads and all(name.startswith("latet-space") for name in worker_threads)
        if held:
            work = work_references[0]()
            assert work is not None and work._owner is audit and audit._snapshot is work
            assert audit.retained_bytes >= BASE_BYTES + SNAPSHOT_WORK_BYTES and audit.protects_idle
            assert not finished.is_set() and not release.is_set()
        # A real second pool job can run: this is D2 ordering, not pool starvation.
        spare = await asyncio.wait_for(spaces._run(lambda: threading.current_thread().name), 1)
        assert spare.startswith("latet-space")
        # Existing awaited publication leaves this real security task pending;
        # the bounded wait ends at a behavioral assertion, not a missing API.
        completed, _pending = await asyncio.wait((jobs["check"],), timeout=1)
        assert jobs["check"] in completed, "security checks awaited held native status publication"
        await jobs["check"]
        assert revoked.closed and disabled.closed and ids[1] not in spaces._spaces and ids[2] not in spaces._spaces
        assert forgotten == [ids[1], ids[2]]
        assert spaces_module._SHARED_BEHIND in survivor.pending
        channel, message, _sequence = survivor.queue.get_nowait()
        survivor.queue.task_done()
        assert channel == spaces_module._SHARED_BEHIND and message == {"counter": 1}
        assert shared_forgotten
        if server._cleaning is not None:
            await asyncio.wait_for(asyncio.shield(server._cleaning), 5)
        assert cleaned == [(ids[1], False), (ids[2], False)] and not server._cleanups
        if held:
            assert not release.is_set() and not finished.is_set()
        database.survivor_present = False
        previous = jobs["check"]
        server_module._start(jobs, failures, "check", server.check)
        assert jobs["check"] is not previous, "completed first check must permit the next beat scan"
        completed, _pending = await asyncio.wait((jobs["check"],), timeout=1)
        assert jobs["check"] in completed, "next security scan awaited prior native status publication"
        await jobs["check"]
        assert len(scans) == 2 and scans[1] == (ids[0], ids[3], "")
        assert survivor.closed and ids[3] not in spaces._spaces and forgotten == list(ids[1:])
        if server._cleaning is not None:
            await asyncio.wait_for(asyncio.shield(server._cleaning), 5)
        assert cleaned == [(ids[1], False), (ids[2], False), (ids[3], True)] and not server._cleanups
        if held:
            assert not release.is_set() and not finished.is_set(), "security scans waited for held D2"
    finally:
        release.set()
        work = None  # end this test borrow before native disposal assertions
        for task in jobs.values():
            await asyncio.wait_for(asyncio.shield(task), 5)
        if server._cleaning is not None:
            await asyncio.wait_for(asyncio.shield(server._cleaning), 5)
        if spaces._closing:
            await asyncio.wait_for(asyncio.gather(*tuple(spaces._closing)), 5)
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        await asyncio.to_thread(spaces._index_executor.shutdown, wait=True)
        assert finished.is_set() and audit._snapshot is None
        assert publications and all(mode == "p" for mode in publications)
        assert all(name.startswith("latet-space") for name in publication_threads)


def test_sweep_lease_prepays_same_owner_and_protects_closed_base():
    from latet.gateway.request_audit import BASE_BYTES, SWEEP_BYTES, RequestAudit

    audit = RequestAudit()
    lease = audit._reserve_sweep()
    assert lease is not None and audit.retained_bytes == BASE_BYTES + SWEEP_BYTES
    assert audit.active == 0 and audit.protects_idle and not audit.try_retire_if_idle()
    assert audit._reserve_sweep() is None
    audit.close()
    assert audit.retained_bytes == BASE_BYTES + SWEEP_BYTES
    lease.close()
    lease.close()
    assert audit.retained_bytes == 0 and not audit.protects_idle
    denied = RequestAudit()
    denied.bind_ceiling(lambda added: "owned sweep denial")
    assert denied._reserve_sweep() is None and denied.retained_bytes == BASE_BYTES and not denied.protects_idle
    denied.close()


def _sweep_population(audit_space, count=4):
    import threading
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    from latet.gateway.request_audit import RequestAudit
    from latet.server.spaces import Space

    spaces, first, runtime = audit_space
    calls, owners = [], []
    for number in range(count):
        identity = "u_" + chr(ord("a") + number) * 10
        audit = runtime.request_audit if number == 0 else RequestAudit()

        class Script:
            sha = "0" * 40

            def __init__(self, identity):
                self.identity = identity

            def __call__(self, *, keys, args):
                assert args[0] == "p"
                calls.append((self.identity, threading.current_thread().name))
                return [1]

        class Client:
            def __init__(self, identity):
                self.identity = identity

            def register_script(self, source):
                return Script(self.identity)

        bus = SimpleNamespace(prefix="latet_t_0123456789:", client=Client(identity))
        assert audit.bind_status(bus, identity, "000000000000")
        local = (
            runtime
            if number == 0
            else SimpleNamespace(request_audit=audit, mediator=runtime.mediator, built=SimpleNamespace(vault=None))
        )
        space = (
            first
            if number == 0
            else Space(
                SimpleNamespace(id=identity, vault_limit=None), local, None, SimpleNamespace(), AsyncExitStack(), 0
            )
        )
        space.identity = SimpleNamespace(id=identity, vault_limit=None)
        owners.append((identity, audit, space))
    spaces._spaces = {identity: space for identity, audit, space in owners}
    return spaces, owners, calls


async def _wait_sweep_disposed(spaces):
    for _ in range(500):
        if spaces._status_sweep is None or spaces._status_sweep() is None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("owned sweep did not dispose")


async def test_one_real_check_sweeps_four_fast_identities_without_another_beat(audit_space, monkeypatch):
    from types import SimpleNamespace

    from latet.server import app
    from latet.server import spaces as module

    spaces, owners, calls = _sweep_population(audit_space)
    server = SimpleNamespace(spaces=spaces, db=object(), shared=SimpleNamespace(count=0), _cleanups={}, _cleaning=None)
    spaces.server = server
    monkeypatch.setattr(
        module, "_states", lambda db, ids: {key: module._State(True, False, 0, 0, frozenset()) for key in (*ids, "")}
    )
    try:
        await app.Server.check(server)
        await _wait_sweep_disposed(spaces)
        assert [identity for identity, _thread in calls] == [identity for identity, _audit, _space in owners]
        assert all(thread.startswith("latet-space") for _identity, thread in calls)
        assert all(audit._snapshot is None and not audit._sweep for _identity, audit, _space in owners)
    finally:
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        for _identity, audit, _space in owners:
            audit.close()


async def test_a_space_whose_index_cannot_be_judged_never_ends_the_beats_check(audit_space, monkeypatch, caplog):
    """The index pass at the end of ``Spaces.check`` (Task 13's fix round) judges each space alone: one that raises is
    said by its error's type, never its words, and the check goes on to the status sweep and the other spaces."""
    import logging
    from types import SimpleNamespace

    from latet.server import app
    from latet.server import spaces as module

    spaces, owners, calls = _sweep_population(audit_space)
    server = SimpleNamespace(spaces=spaces, db=object(), shared=SimpleNamespace(count=0), _cleanups={}, _cleaning=None)
    spaces.server = server
    monkeypatch.setattr(
        module, "_states", lambda db, ids: {key: module._State(True, False, 0, 0, frozenset()) for key in (*ids, "")}
    )
    judged = []

    def indexed(space):
        judged.append(space.identity.id)
        raise RuntimeError("words of a secret")

    monkeypatch.setattr(module, "_indexed", indexed)
    try:
        with caplog.at_level(logging.WARNING, logger="latet.server.spaces"):
            await app.Server.check(server)
        await _wait_sweep_disposed(spaces)
        assert sorted(judged) == sorted(identity for identity, _audit, _space in owners)
        assert [identity for identity, _thread in calls] == [identity for identity, _audit, _space in owners]
        said = [record.getMessage() for record in caplog.records if "RuntimeError" in record.getMessage()]
        assert said and not any("words of a secret" in line for line in caplog.messages)
    finally:
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        for _identity, audit, _space in owners:
            audit.close()


async def test_repeated_kicks_busy_closed_and_churn_do_not_build_a_queue(audit_space, monkeypatch):
    import threading

    from latet.api import rest

    spaces, owners, calls = _sweep_population(audit_space)
    entered, release = threading.Event(), threading.Event()
    original = rest._read_request_status

    def gated(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(rest, "_read_request_status", gated)
    busy = owners[1][1].reserve_snapshot()
    owners[2][1].close()
    try:
        spaces.kick_status()
        assert await asyncio.to_thread(entered.wait, 5)
        reference = spaces._status_sweep
        remaining = reference()._remaining
        for _ in range(32):
            spaces.kick_status()
            assert spaces._status_sweep is reference and reference()._remaining == remaining
        assert (
            sum(audit._snapshot is not None for _identity, audit, _space in owners) == 2
        )  # pre-existing busy + ONE input
        del spaces._spaces[owners[3][0]]
        release.set()
        await _wait_sweep_disposed(spaces)
        assert 1 <= len(calls) <= len(owners) and all(
            identity == owners[0][0] and thread.startswith("latet-space") for identity, thread in calls
        )
        assert owners[1][1]._snapshot is busy
    finally:
        release.set()
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        busy.close()
        for _identity, audit, _space in owners:
            audit.close()


async def test_terminal_handle_retains_sweep_charge_context_and_closed_base(audit_space, monkeypatch):
    import contextvars

    from latet.gateway.request_audit import SWEEP_BYTES
    from latet.server.spaces import _StatusSweep

    spaces, owners, calls = _sweep_population(audit_space, 1)
    loop = asyncio.get_running_loop()
    marker = contextvars.ContextVar("p25-owned-traffic-marker", default=None)
    token = marker.set(TOOL)
    original, retained = loop.call_soon_threadsafe, []

    def scheduled(callback, *args, context=None):
        handle = original(callback, *args, context=context)
        if type(callback) is _StatusSweep and callback._remaining == 0:
            retained.append(handle)
        return handle

    monkeypatch.setattr(loop, "call_soon_threadsafe", scheduled)
    try:
        spaces.kick_status()
        marker.reset(token)
        for _ in range(500):
            if retained and retained[0]._callback._stopped:
                break
            await asyncio.sleep(0.005)
        assert len(retained) == 1 and retained[0]._callback._stopped and calls
        assert len(retained[0]._context) == 0 and retained[0]._context.run(marker.get) is None
        audit = owners[0][1]
        assert audit._snapshot is None and audit._sweep and audit.protects_idle
        audit.close()
        assert audit.retained_bytes == 4096 + SWEEP_BYTES
        retained.clear()
        await _wait_sweep_disposed(spaces)
        assert audit.retained_bytes == 0 and not audit.protects_idle
    finally:
        retained.clear()
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)


@pytest.mark.parametrize("mode", ["normal", "queued-cancel", "submit-after-enqueue", "body-BaseException"])
async def test_publication_input_disposal_is_the_only_terminal_owner(audit_space, monkeypatch, mode):
    import threading
    import weakref

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    entered, release = threading.Event(), threading.Event()
    blocker = pool.submit(lambda: (entered.set(), release.wait(5)))
    assert await asyncio.to_thread(entered.wait, 5)
    futures, inputs, ended = [], [], []
    original = pool.submit

    def submitting(carrier):
        inputs.append(weakref.ref(carrier))
        future = original(carrier)
        futures.append(future)
        if mode == "submit-after-enqueue":
            raise RuntimeError("owned post-enqueue failure")
        return future

    monkeypatch.setattr(pool, "submit", submitting)

    class Disposed:
        def _input_ended(self):
            assert not audit._lock._is_owned()

        def _disposed(self):
            assert not audit._lock._is_owned() and audit._snapshot is None
            ended.append(True)

    if mode == "body-BaseException":

        def failed(*args):
            raise KeyboardInterrupt("owned status body interruption")

        monkeypatch.setattr(rest, "_read_request_status", failed)
    work = audit.reserve_snapshot()
    assert work is not None
    try:
        assert rest._submit_publication(work, runtime.mediator, pool, Disposed())
        if mode == "queued-cancel":
            assert futures[0].cancel()
        assert work._worker and work._queued and audit._snapshot is work and inputs[0]() is not None
        audit.close()
        assert audit.retained_bytes > 0 and ended == []
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        assert ended == [True] and inputs[0]() is None and work._owner is None and audit.retained_bytes == 0
        if mode != "queued-cancel":
            assert futures[0].result() is None and futures[0].exception() is None
    finally:
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        blocker.result(5)


@pytest.mark.parametrize("after_enqueue", [False, True])
async def test_ambiguous_sweep_wake_failure_stops_without_early_refund(audit_space, monkeypatch, after_enqueue):
    from latet.gateway.request_audit import SWEEP_BYTES
    from latet.server.spaces import _StatusSweep

    spaces, owners, _calls = _sweep_population(audit_space, 1)
    audit, loop = owners[0][1], asyncio.get_running_loop()
    retained, original = [], loop.call_soon_threadsafe

    def failed(callback, *args, context=None):
        if type(callback) is _StatusSweep:
            if after_enqueue:
                retained.append(original(callback, *args, context=context))
            raise RuntimeError("owned ambiguous wake failure")
        return original(callback, *args, context=context)

    monkeypatch.setattr(loop, "call_soon_threadsafe", failed)
    spaces.kick_status()
    await asyncio.sleep(0)
    if after_enqueue:
        assert audit._sweep and retained and retained[0]._callback._stopped
        audit.close()
        assert audit.retained_bytes == 4096 + SWEEP_BYTES
        retained.clear()
    await _wait_sweep_disposed(spaces)
    assert not audit._sweep
    spaces.shutdown()


async def test_sweep_two_handles_empty_work_full_native_graph_fits_same_lease(audit_space):
    import contextvars
    import sys
    import tracemalloc

    from latet.api.rest import _PublicationInput
    from latet.gateway.request_audit import SWEEP_BYTES, SnapshotWork
    from latet.server.spaces import _StatusSweep

    spaces, _space, runtime = audit_space
    audit, loop = runtime.request_audit, asyncio.get_running_loop()
    baseline = _owned_audit_shape(audit)
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    lease = audit._reserve_sweep()
    sweep = _StatusSweep(lease, spaces, loop, sys.maxsize, sys.maxsize - 1)
    import weakref

    spaces._status_sweep = weakref.ref(sweep)
    handle1 = asyncio.Handle(sweep, (), loop, context=contextvars.Context())
    handle2 = asyncio.Handle(sweep, (), loop, context=contextvars.Context())
    empty = SnapshotWork(audit)
    empty._owner = None  # actual terminal empty-shell shape, no buffer or refund claim
    empty_input = _PublicationInput.__new__(_PublicationInput)
    empty_input._work = empty_input._context = empty_input._mediator = empty_input._sweep = None
    empty_input._armed = False
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # A concurrent close can still borrow the weak marker after the carrier
    # clears it; include that empty-shell overlap rather than discounting it.
    marker = weakref.ref(empty)
    native = _owned_audit_shape(audit, sweep, handle1, handle2, empty, empty_input, marker, spaces._status_sweep)
    dimensions = dict(baseline=baseline, native=native, current=current, peak=peak, retained=audit.retained_bytes)
    assert native - baseline <= SWEEP_BYTES, dimensions
    assert baseline + peak <= audit.retained_bytes, dimensions
    assert native + max(0, peak - current) <= audit.retained_bytes, dimensions
    handle1 = handle2 = None
    sweep = empty = empty_input = marker = None
    lease = None
    assert not audit._sweep and audit.retained_bytes == 4096


async def test_publication_future_terminal_is_not_actual_input_disposal(audit_space, monkeypatch):
    import threading
    from concurrent.futures import Future

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    blocked, release_blocker, terminal, release_terminal = (threading.Event() for _ in range(4))
    blocker = pool.submit(lambda: (blocked.set(), release_blocker.wait(5)))
    assert await asyncio.to_thread(blocked.wait, 5)
    futures, ended = [], []
    original_submit, original_result = pool.submit, Future.set_result

    def submitting(carrier):
        future = original_submit(carrier)
        futures.append(future)
        return future

    def returning(future, result):
        original_result(future, result)
        if futures and future is futures[0]:
            terminal.set()
            assert release_terminal.wait(5)

    monkeypatch.setattr(pool, "submit", submitting)
    monkeypatch.setattr(Future, "set_result", returning)

    class Disposed:
        def _input_ended(self):
            assert not audit._lock._is_owned()

        def _disposed(self):
            assert not audit._lock._is_owned()
            ended.append(True)

    work = audit.reserve_snapshot()
    try:
        assert rest._submit_publication(work, runtime.mediator, pool, Disposed())
        release_blocker.set()
        assert await asyncio.to_thread(terminal.wait, 5)
        assert futures[0].done() and work._queued and work._native and audit._snapshot is work and ended == []
        audit.close()
        assert audit.retained_bytes > 0
        release_terminal.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        assert ended == [True] and work._owner is None and audit.retained_bytes == 0
    finally:
        release_blocker.set()
        release_terminal.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        blocker.result(5)


async def test_publication_initializer_failure_refunds_only_actual_drained_input(audit_space, monkeypatch):
    import threading
    from concurrent.futures.thread import BrokenThreadPool

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit, pool = runtime.request_audit, runtime.mediator.executor
    entered, release = threading.Event(), threading.Event()
    futures, ended = [], []
    assert not pool._threads

    def initializing():
        entered.set()
        assert release.wait(5)
        raise RuntimeError("owned initializer failure")

    thread_pools.initialize_with(pool, initializing)
    original = pool.submit

    def submitting(carrier):
        future = original(carrier)
        futures.append(future)
        return future

    monkeypatch.setattr(pool, "submit", submitting)

    class Disposed:
        def _input_ended(self):
            assert not audit._lock._is_owned()

        def _disposed(self):
            assert not audit._lock._is_owned()
            ended.append(True)

    work = audit.reserve_snapshot()
    try:
        assert rest._submit_publication(work, runtime.mediator, pool, Disposed())
        assert await asyncio.to_thread(entered.wait, 5)
        audit.close()
        assert work._queued and audit.retained_bytes > 0 and not ended
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        assert isinstance(futures[0].exception(), BrokenThreadPool)
        assert ended == [True] and audit.retained_bytes == 0 and work._owner is None
    finally:
        release.set()
        await asyncio.to_thread(pool.shutdown, wait=True)


def test_publication_partial_construction_clears_inputs_before_refund(audit_space, monkeypatch):
    import weakref

    from latet.api import rest

    _spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    observed = []
    original = rest._PublicationInput.__init__

    def failed(self, *args):
        original(self, *args)
        observed.append(weakref.ref(self))
        raise MemoryError("owned partial publication setup")

    monkeypatch.setattr(rest._PublicationInput, "__init__", failed)
    work = audit.reserve_snapshot()
    assert not rest._submit_publication(work, runtime.mediator, runtime.mediator.executor, object())
    assert observed and observed[0]() is None
    assert work._owner is None and audit._snapshot is None and audit.retained_bytes == 4096


async def test_drop_closed_owner_and_shutdown_do_not_republish_or_end_live_sweep_early(audit_space, monkeypatch):
    import threading

    from latet.api import rest
    from latet.gateway.request_audit import SWEEP_BYTES

    spaces, owners, calls = _sweep_population(audit_space, 2)
    entered, release = threading.Event(), threading.Event()
    original = rest._read_request_status

    def gated(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(rest, "_read_request_status", gated)
    audit = owners[0][1]
    try:
        spaces.kick_status()
        assert await asyncio.to_thread(entered.wait, 5)
        del spaces._spaces[owners[0][0]]
        audit.close()
        spaces.shutdown()
        assert audit._sweep and audit._snapshot is not None and audit.retained_bytes > 4096 + SWEEP_BYTES
        spaces.kick_status()
        release.set()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        await _wait_sweep_disposed(spaces)
        assert calls == [] and audit.retained_bytes == 0 and owners[1][1]._snapshot is None
    finally:
        release.set()
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        for _identity, owner, _space in owners:
            owner.close()


async def test_closed_loop_retains_exact_sweep_owner_until_shell_disposes(audit_space):
    import weakref

    from latet.gateway.request_audit import SWEEP_BYTES
    from latet.server.spaces import _StatusSweep

    spaces, _space, runtime = audit_space
    audit = runtime.request_audit
    loop = asyncio.new_event_loop()
    loop.close()
    lease = audit._reserve_sweep()
    sweep = _StatusSweep(lease, spaces, loop, 1, 0)
    spaces._status_sweep = weakref.ref(sweep)
    sweep._schedule()
    assert sweep._stopped and audit._sweep and audit.protects_idle
    audit.close()
    assert audit.retained_bytes == 4096 + SWEEP_BYTES
    sweep = lease = None
    assert audit.retained_bytes == 0


async def test_actual_drop_ends_queued_publication_consumer_before_close_wait(audit_space, monkeypatch):
    import threading
    from types import SimpleNamespace

    from latet.api import rest
    from latet.gateway.request_audit import SWEEP_BYTES

    spaces, owners, calls = _sweep_population(audit_space, 1)
    audit = owners[0][1]
    owners[0][2].parts.vault = SimpleNamespace(revoke_index=lambda: None)
    entered, release = threading.Event(), threading.Event()
    close_entered, close_release = asyncio.Event(), asyncio.Event()
    original = rest._read_request_status

    def gated(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    async def held_close(selected):
        assert selected == [owners[0][2]]
        close_entered.set()
        await close_release.wait()

    monkeypatch.setattr(rest, "_read_request_status", gated)
    monkeypatch.setattr(spaces, "_close_all", held_close)
    try:
        spaces.kick_status()
        assert await asyncio.to_thread(entered.wait, 5)
        await spaces.drop(owners[0][0], wait=False)
        await asyncio.wait_for(close_entered.wait(), 1)
        assert not audit._closed and audit._snapshot._ending and audit._snapshot._queued
        assert audit.retained_bytes > 4096 + SWEEP_BYTES and audit.protects_idle and calls == []
        release.set()
        await _wait_sweep_disposed(spaces)
        assert calls == [] and audit._snapshot is None and audit.retained_bytes == 4096
    finally:
        release.set()
        close_release.set()
        if spaces._closing:
            await asyncio.wait_for(asyncio.gather(*tuple(spaces._closing)), 5)
        spaces.shutdown()
        await asyncio.to_thread(spaces._executor.shutdown, wait=True)
        audit.close()


def _hc_views(store, exchange_id):
    listed, _cursor = store.list()
    return [
        next(row for row in listed if row["id"] == exchange_id),
        store.detail(exchange_id)["summary"],
        next(row for row in store.summaries_since(0) if row["id"] == exchange_id),
        next(row for part in store.summaries_parts(0, 1000) for row in part if row["id"] == exchange_id),
    ]


def _hc_assert_public(rows, coverage):
    for row in rows:
        # Coverage is the authenticated capsule's alone; verified is the recorder's own fact, a boolean either way.
        assert row["coverage"] == coverage and type(row["verified"]) is bool
        assert "_history_coverage" not in row and "history_code" not in row
        assert "hc:l1:" not in json.dumps(row) and "hc:p1:" not in json.dumps(row)


@pytest.mark.parametrize("history", ["metadata", "full"])
@pytest.mark.parametrize(
    "case", ["forward", "policy", "off", "block", "prepared-refusal", "late-block", "transport-refusal"]
)
async def test_history_coverage_actual_http_recorder_pipeline(tmp_path, audit_space, monkeypatch, history, case):
    """Kills missing producer capture, replacement capture, feed projection and any reader bypass."""
    from types import SimpleNamespace

    import httpx
    from starlette.responses import StreamingResponse

    from latet.engine.vault import MaskingError
    from latet.gateway import forward, request_transport
    from latet.gateway.capture import Capture
    from latet.gateway.recorder import Recorder
    from latet.gateway.routes import RouteTable
    from latet.storage.exchanges import ExchangeStore
    from latet.vision.masking import ImageMasking

    from ..fake_ocr import FakeOcr
    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    provider, feed = _Provider(), []
    store = ExchangeStore(tmp_path / "hc-real.db", ROOT_KEY)
    recorder = Recorder(store, runtime.mediator.engine, SimpleNamespace(publish=feed.append), history=history)
    captured, real_turn = [], forward._Turn

    def turn():
        made = real_turn()
        captured.append(made)
        return made

    monkeypatch.setattr(forward, "_Turn", turn)
    body = _chat("hello")
    if case == "policy":
        ocr = FakeOcr()
        runtime.mediator.vision = ImageMasking(ocr)
        body = _chat("hello", image=ocr.screenshot(["ordinary image"]))
    if case == "off":  # D5: a project where Latet is off refuses its requests, never sends them unread
        from latet.config import Config, IdentityConfig, ProjectEntry
        from latet.projects import Projects

        root = tmp_path / "hc-off-project"
        root.mkdir()
        projects = Projects(
            Config(
                ner="off",
                identity=IdentityConfig(enabled=False),
                projects=[ProjectEntry(path=str(root), enabled=False)],
            ),
            runtime.mediator.engine.vault,
            files=False,
        )
        monkeypatch.setattr(runtime.mediator.engine, "scopes", projects)
    if case == "prepared-refusal":

        def refused(*args, **kwargs):
            raise MaskingError("owned preparation failure")

        monkeypatch.setattr(runtime.mediator, "prepare_http", refused)
    original = request_transport.validate_send

    def validate(*args, **kwargs):
        if case in ("late-block", "transport-refusal"):
            return request_transport.SendRefusal("coverage-limit" if case == "late-block" else "stale")
        return original(*args, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validate)
    if case == "block":
        body = _chat("hello").replace(b'"hello"', b"4111111111111111")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = forward.Forwarder(
                RouteTable([OPENAI]),
                Capture(None),
                client,
                runtime.mediator,
                recorder=recorder,
                request_audit=runtime.request_audit,
            )
            request = _request(body)
            if case == "off":
                request.scope["headers"].append((b"x-latet-project", str(root).encode()))
            response = await gateway.handle(request)
            if isinstance(response, StreamingResponse):
                async for _chunk in response.body_iterator:
                    pass
        assert await asyncio.to_thread(recorder.drain, 5)
        await asyncio.sleep(0)  # worker publication was posted on this loop
        from latet.gateway.outcome import Block, Forward, Pass, PolicyEvidence

        [decided] = captured
        if case == "policy":
            assert type(decided.decision) is Pass and type(decided.decision.evidence) is PolicyEvidence
        elif case in ("off", "block", "prepared-refusal", "late-block"):
            assert type(decided.decision) is Block
        else:
            assert type(decided.decision) is Forward
        rows, cursor = store.list()
        assert len(rows) == 1 and cursor is None, "coverage must preserve the existing row denominator"
        [row] = rows
        expected = "unknown" if case in ("off", "block", "prepared-refusal", "late-block") else "complete-accounting"
        _hc_assert_public(_hc_views(store, row["id"]) + feed, expected)
        assert len(feed) == 1 and row["status"] == response.status_code
        assert len(provider.sent) == (1 if case in ("forward", "policy") else 0)
        with store._lock:
            raw = json.loads(
                store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (row["id"],)).fetchone()[0]
            )
        assert len(raw["_history_coverage"]) == 70 and raw["_history_coverage"].startswith("hc:l1:")
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize("history", ["metadata", "full"])
@pytest.mark.parametrize("mode", ["absent", "full", "closed"])
async def test_history_coverage_is_independent_of_audit_availability(tmp_path, audit_space, history, mode):
    """Kills accidental gating of genuine history facts on successful Audit admission."""
    from types import SimpleNamespace

    import httpx
    from starlette.responses import StreamingResponse

    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.recorder import Recorder
    from latet.gateway.request_audit import new_exception_reference
    from latet.gateway.routes import RouteTable
    from latet.storage.exchanges import ExchangeStore

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    audit, held = runtime.request_audit, []
    if mode == "full":
        reference = new_exception_reference(1, 1)
        while (lease := audit.reserve_exception(reference)) is not None:
            held.append(lease)
        assert audit._reserve_projection(1) is None
    elif mode == "closed":
        audit.close()
    store, feed = ExchangeStore(tmp_path / "hc-audit.db", ROOT_KEY), []
    recorder = Recorder(store, runtime.mediator.engine, SimpleNamespace(publish=feed.append), history=history)
    provider = _Provider()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = Forwarder(
                RouteTable([OPENAI]),
                Capture(None),
                client,
                runtime.mediator,
                recorder=recorder,
                request_audit=None if mode == "absent" else audit,
            )
            response = await gateway.handle(_request(_chat("hello")))
            if isinstance(response, StreamingResponse):
                async for _chunk in response.body_iterator:
                    pass
        assert response.status_code == 200 and len(provider.sent) == 1
        assert await asyncio.to_thread(recorder.drain, 5)
        await asyncio.sleep(0)
        [row], _ = store.list()
        _hc_assert_public(_hc_views(store, row["id"]) + feed, "complete-accounting")
        assert len(feed) == 1 and not _producer_events(audit)
    finally:
        for lease in held:
            audit.abandon(lease)
        recorder.close()
        store.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "unknown"),
        (21, "complete-accounting"),
        (25, "complete-accounting"),
        (26, "legacy-uninspected"),
        (28, "unknown"),
        (True, "unknown"),
        (999, "unknown"),
    ],
)
def test_history_coverage_immutable_snapshot_and_explicit_writer_input(
    tmp_path, outbound_owner, monkeypatch, code, expected
):
    """Kills aliasing to Outcome/Prepared and summary-driven coverage upgrades."""
    from dataclasses import FrozenInstanceError
    from types import SimpleNamespace

    from latet.gateway.recorder import Outcome, Recorder
    from latet.storage.exchanges import ExchangeStore

    store, feed = ExchangeStore(tmp_path / "hc-freeze.db", ROOT_KEY), []
    recorder = Recorder(store, outbound_owner.engine, SimpleNamespace(publish=feed.append), history="metadata")
    outcome = Outcome({}, "openai", "/openai/v1/chat/completions", "http", time.time(), history_code=code)
    try:
        snap = recorder.snapshot(outcome)
        assert snap is not None
        with pytest.raises(FrozenInstanceError):
            snap.history_code = 28
        outcome.history_code = 28
        outcome.prepared = _prepared(outbound_owner, "anthropic-server")
        public = recorder._record(snap)
        [row], _ = store.list()
        _hc_assert_public([*_hc_views(store, row["id"]), public], expected)
        assert row["transport"] == "http"
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "prefix",
        "long",
        "uppercase",
        "nonhex",
        "tag",
        "version",
        "triple",
        "rebound",
        "summary-id",
        "foreign-root",
    ],
)
def test_history_coverage_local_authentication_rejects_without_repair(tmp_path, change):
    """Kills predecode omissions, unauthenticated triples and JSON-ID binding instead of selected physical ID."""
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "hc-auth.db", ROOT_KEY)
    foreign = ExchangeStore(tmp_path / "hc-foreign.db", b"Z" * 32)
    try:
        first = store.record(
            ExchangeRecord({"id": "ex_owned", "ts": time.time(), "session": "owned", "flags": []}, history_code=21)
        )
        second = store.record(
            ExchangeRecord({"id": "ex_other", "ts": time.time(), "session": "owned", "flags": []}, history_code=25)
        )
        with store._lock:
            raw = json.loads(store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (first,)).fetchone()[0])
            other = json.loads(store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (second,)).fetchone()[0])
        capsule = raw["_history_coverage"]
        if change == "missing":
            raw.pop("_history_coverage")
        elif change in ("version", "triple"):
            import os

            nonce = os.urandom(12)
            payload = b"\x02\x01\x01\x01" if change == "version" else b"\x01\x01\x03\x01"
            aad = b"latet:history-coverage:v1\0local\0" + first.encode("ascii")
            raw["_history_coverage"] = "hc:l1:" + (nonce + store._aead.encrypt(nonce, payload, aad)).hex()
        elif change == "rebound":
            raw["_history_coverage"] = other["_history_coverage"]
        elif change == "summary-id":
            raw["id"] = second
        elif change == "foreign-root":
            foreign.record(
                ExchangeRecord({"id": first, "ts": time.time(), "session": "owned", "flags": []}, history_code=21)
            )
            raw["_history_coverage"] = json.loads(
                foreign._db.execute("SELECT summary FROM exchanges WHERE id = ?", (first,)).fetchone()[0]
            )["_history_coverage"]
        else:
            raw["_history_coverage"] = {
                "prefix": "hc:p1:" + capsule[6:],
                "long": capsule + "0",
                "uppercase": "hc:l1:" + "A" * 64,
                "nonhex": "hc:l1:" + "g" * 64,
                "tag": capsule[:-1] + ("0" if capsule[-1] != "0" else "1"),
            }[change]
        raw.update(coverage="complete-accounting", verified=True, history_code=21)
        installed = json.dumps(raw)
        with store._lock:
            store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (installed, first))
            before = store._db.execute("SELECT * FROM exchanges ORDER BY id").fetchall()
        # summary-id corruption can change public id; select the physically addressed detail as the direct assertion.
        _hc_assert_public([store.detail(first)["summary"]], "unknown")
        for summary in (
            store.list()[0] + store.summaries_since(0) + [r for part in store.summaries_parts(0, 1000) for r in part]
        ):
            if summary.get("coverage") != "unknown":
                assert summary["id"] == second
        with store._lock:
            assert store._db.execute("SELECT * FROM exchanges ORDER BY id").fetchall() == before
    finally:
        store.close()
        foreign.close()


def test_history_coverage_summary_only_and_old_rows_stay_unknown_byte_exact(tmp_path):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore, safe_summary

    store = ExchangeStore(tmp_path / "hc-old.db", ROOT_KEY)
    try:
        summary = {
            "id": "ex_old",
            "ts": time.time(),
            "session": "owned",
            "flags": [],
            "coverage": "complete-accounting",
            "verified": True,
            "history_code": 21,
            "_history_coverage": "hc:l1:" + "0" * 64,
        }
        _hc_assert_public([safe_summary(summary)], "unknown")
        exchange_id = store.record(ExchangeRecord(summary))
        raw = json.dumps(summary)
        with store._lock:
            stored = json.loads(
                store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (exchange_id,)).fetchone()[0]
            )
            assert "_history_coverage" not in stored
            store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (raw, exchange_id))
            before = store._db.execute("SELECT * FROM exchanges").fetchall()
        _hc_assert_public(_hc_views(store, exchange_id), "unknown")
        assert store._db.execute("SELECT * FROM exchanges").fetchall() == before
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["refresh", "refused-retry", "credentials"])
def test_history_coverage_actual_aws_final_decision(tmp_path, audit_space, monkeypatch, failure):
    import httpx

    from latet.gateway import request_transport
    from latet.gateway.recorder import Recorder
    from latet.storage.exchanges import ExchangeStore

    from .test_request_transport import HELLO, _Aws, _bedrock, _invoke, _route

    _spaces, _space, runtime = audit_space
    store = ExchangeStore(tmp_path / "hc-aws.db", ROOT_KEY)
    recorder = Recorder(store, runtime.mediator.engine, history="metadata")
    calls = []

    def answered(request):
        calls.append(request)
        return httpx.Response(409, headers={"x-amzn-errortype": "ExpiredTokenException"}, content=b"{}")

    original = request_transport.validate_send

    def validate(decision, arena, envelope, policy, attempt, **kwargs):
        if failure == "refused-retry" and attempt == 2:
            return request_transport.SendRefusal("coverage-limit")
        return original(decision, arena, envelope, policy, attempt, **kwargs)

    monkeypatch.setattr(request_transport, "validate_send", validate)
    aws = _Aws(fails=1 if failure == "credentials" else 2 if failure == "refresh" else None)
    try:
        gateway = _bedrock(runtime.mediator, aws, answered, recorder=recorder, request_audit=runtime.request_audit)
        with gateway.client as client:
            response = client.post(_route(), content=_invoke(HELLO), headers={"content-type": "application/json"})
        assert recorder.drain(5)
        [row], _ = store.list()
        _hc_assert_public(_hc_views(store, row["id"]), "complete-accounting" if failure == "refresh" else "unknown")
        assert row["status"] == response.status_code and len(calls) == (0 if failure == "credentials" else 1)
    finally:
        recorder.close()
        store.close()


async def test_history_coverage_delayed_stream_reads_frozen_scalar_without_proof_rewalk(
    tmp_path, audit_space, monkeypatch
):
    import httpx

    from latet.gateway import forward
    from latet.gateway.body_coverage import BodyProof
    from latet.gateway.capture import Capture
    from latet.gateway.recorder import Recorder
    from latet.gateway.routes import RouteTable
    from latet.storage.exchanges import ExchangeStore

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI

    _spaces, _space, runtime = audit_space
    store = ExchangeStore(tmp_path / "hc-delayed.db", ROOT_KEY)
    recorder = Recorder(store, runtime.mediator.engine, history="metadata")
    provider, captured = _Provider(), []
    real = forward._Turn

    def turn():
        made = real()
        captured.append(made)
        return made

    monkeypatch.setattr(forward, "_Turn", turn)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = forward.Forwarder(
                RouteTable([OPENAI]), Capture(None), client, runtime.mediator, recorder=recorder
            )
            body = json.dumps(
                {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hello"}]}
            ).encode()
            response = await gateway.handle(_request(body))
            assert response.status_code == 200 and not store.list()[0]
            [held] = captured
            # handle() has emitted Audit/closed its receipt. The stream's finish runs later.
            held.decision = None

            def forbidden(*args, **kwargs):
                raise AssertionError("history coverage revisited a content proof after handle finalization")

            monkeypatch.setattr(BodyProof, "proves", forbidden)
            chunks = [chunk async for chunk in response.body_iterator]
            assert chunks and len(provider.sent) == 1
        assert await asyncio.to_thread(recorder.drain, 5)
        [row], _ = store.list()
        _hc_assert_public(_hc_views(store, row["id"]), "complete-accounting")
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize("mode", ["off", "full-queue", "seal-failure"])
async def test_history_coverage_history_failures_preserve_traffic_and_audit(tmp_path, audit_space, monkeypatch, mode):
    from types import SimpleNamespace

    import httpx
    from starlette.responses import StreamingResponse

    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.recorder import Recorder
    from latet.gateway.routes import RouteTable
    from latet.storage.exchanges import ExchangeStore

    from .test_request_leaks import _Provider, _request
    from .test_request_outcomes import OPENAI, _chat

    _spaces, _space, runtime = audit_space
    store, feed = ExchangeStore(tmp_path / "hc-failure.db", ROOT_KEY), []
    recorder = Recorder(
        store,
        runtime.mediator.engine,
        SimpleNamespace(publish=feed.append),
        history="off" if mode == "off" else "metadata",
    )
    provider = _Provider()
    if mode == "off":

        def forbidden(*args, **kwargs):
            raise AssertionError("history-off allocated a snapshot")

        monkeypatch.setattr(recorder, "snapshot", forbidden)
    elif mode == "full-queue":
        recorder._pending = 500  # inject admission state, no fake worker or fake producer result
    else:

        class BrokenAEAD:
            def encrypt(self, *args):
                raise ValueError("P25_PRIVATE_ENCRYPT_FAILURE")

        monkeypatch.setattr(store, "_aead", BrokenAEAD())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = Forwarder(
                RouteTable([OPENAI]),
                Capture(None),
                client,
                runtime.mediator,
                recorder=recorder,
                request_audit=runtime.request_audit,
            )
            response = await gateway.handle(_request(_chat("hello")))
            if isinstance(response, StreamingResponse):
                async for _chunk in response.body_iterator:
                    pass
        if mode == "full-queue":
            recorder._pending = 0
        assert await asyncio.to_thread(recorder.drain, 5)
        await asyncio.sleep(0)
        assert response.status_code == 200 and len(provider.sent) == 1
        assert len(_producer_events(runtime.request_audit)) == 1
        assert not store.list()[0] and feed == []
    finally:
        if mode == "full-queue":
            recorder._pending = 0
        recorder.close()
        store.close()


async def test_history_coverage_real_finish_snapshot_outlives_mutable_outcome(tmp_path, outbound_owner, monkeypatch):
    import threading

    from latet.gateway.recorder import Outcome, Recorder
    from latet.storage.exchanges import ExchangeStore

    store = ExchangeStore(tmp_path / "hc-held.db", ROOT_KEY)
    recorder = Recorder(store, outbound_owner.engine, history="metadata")
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    original = recorder._record

    def paused(snapshot):
        snapshots.append(snapshot)
        entered.set()
        assert release.wait(5)
        return original(snapshot)

    monkeypatch.setattr(recorder, "_record", paused)
    outcome = Outcome({}, "openai", "/openai/v1/chat/completions", "http", time.time(), history_code=21)
    try:
        recorder.finish(outcome)
        assert await asyncio.to_thread(entered.wait, 5)
        outcome.history_code = 28
        outcome.prepared = _prepared(outbound_owner, "anthropic-server")
        assert snapshots[0].history_code == 21
        release.set()
        assert await asyncio.to_thread(recorder.drain, 5)
        [row], _ = store.list()
        _hc_assert_public(_hc_views(store, row["id"]), "complete-accounting")
    finally:
        release.set()
        recorder.close()
        store.close()


@pytest.mark.parametrize("transport", ["ws", "plugin"])
def test_history_coverage_no_handoff_never_infers_from_prepared_or_observation(tmp_path, outbound_owner, transport):
    from latet.gateway.recorder import Outcome, Recorder
    from latet.storage.exchanges import ExchangeStore

    store = ExchangeStore(tmp_path / "hc-unavailable.db", ROOT_KEY)
    recorder = Recorder(store, outbound_owner.engine, history="metadata")
    try:
        outcome = Outcome(
            {},
            "openai",
            "/openai/v1/responses",
            transport,
            time.time(),
            prepared=_prepared(outbound_owner, "anthropic-server"),
            status=200,
        )
        recorder.finish(outcome)
        assert recorder.drain(5)
        [row], _ = store.list()
        _hc_assert_public(_hc_views(store, row["id"]), "unknown")
        raw = json.loads(store._db.execute("SELECT summary FROM exchanges").fetchone()[0])
        assert "_history_coverage" not in raw
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize("mode", ["normal", "refused", "cancel", "supersede", "response-during-send"])
async def test_history_coverage_actual_ws_turn_is_recorded_as_over_http(tmp_path, audit_space, monkeypatch, mode):
    """Root's ruling on P2-7's concern 3: a WebSocket turn records its coverage, what it was let through by and its
    profile in the history as a request over HTTP does, from the turn's own decision (one code path); only an older
    row stays unknown. The capsule is sealed in the row and never shown."""
    from latet.gateway import request_transport, ws
    from latet.gateway.capture import Capture
    from latet.gateway.recorder import Recorder
    from latet.gateway.routes import RouteTable, Upstream
    from latet.storage.exchanges import ExchangeStore

    from .test_request_outcomes import _Client, _until

    _spaces, _space, runtime = audit_space
    store = ExchangeStore(tmp_path / "hc-ws.db", ROOT_KEY)
    recorder = Recorder(store, runtime.mediator.engine, history="metadata")
    client = _Client()
    # A tracing header its profile removes at the handshake: each turn's history counts it (review Minor 10).
    client.socket.scope["headers"].append((b"traceparent", b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"))

    class Provider(_AuditSocketProvider):
        def __init__(self):
            super().__init__()
            self.answers = asyncio.Queue()

        async def send(self, data):
            if mode == "response-during-send":
                self.answers.put_nowait(
                    json.dumps({"type": "response.completed", "response": {"id": "r-owned", "output": []}})
                )
                await asyncio.sleep(0)
            await super().send(data)

        async def __anext__(self):
            return await self.answers.get()

    provider = Provider()
    provider.mode = "cancel" if mode == "cancel" else "normal"

    async def opened(*args, **kwargs):
        return provider

    monkeypatch.setattr(request_transport, "_NoRedirect", opened)
    if mode == "refused":
        monkeypatch.setattr(
            request_transport, "validate_frame", lambda *args, **kwargs: request_transport.SendRefusal("coverage-limit")
        )
    routes = RouteTable(
        [
            Upstream(
                "relay",
                "/relay",
                "https://invalid.example",
                envelope_expected=frozenset({("model", "m")}),
            )
        ]
    )
    task = asyncio.create_task(
        ws.relay(
            client.socket,
            routes,
            Capture(None),
            runtime.mediator,
            recorder=recorder,
            request_audit=runtime.request_audit,
        )
    )
    try:
        await _until(lambda: len(_producer_events(runtime.request_audit)) == 1)
        assert not store.list()[0], "a handshake must not invent a history row"
        payload = json.dumps(
            {
                "type": "response.create",
                "model": "m",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            }
        )
        client.incoming.put_nowait({"type": "websocket.receive", "text": payload})
        if mode != "refused":
            await asyncio.wait_for(provider.entered.wait(), 5)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await _until(lambda: len(_producer_events(runtime.request_audit)) >= 2)
            if mode == "supersede":
                client.incoming.put_nowait({"type": "websocket.receive", "text": payload})
                await _until(lambda: len(_producer_events(runtime.request_audit)) >= 3)
            client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({"type": "response.cancel"})})
            client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 5)
        assert await asyncio.to_thread(recorder.drain, 5)
        rows, _ = store.list()
        assert bool(rows) is (mode != "refused"), "preserve the actual recorder row denominator"
        _hc_assert_public(rows, "complete-accounting")
        for row in rows:
            assert row["passes"] == [] and str(row["profile"]).endswith(".ws-envelope-1"), row
            assert row["removed"] == {"headers": 1, "members": 0}, row
        assert all(
            "_history_coverage" in json.loads(row[0]) for row in store._db.execute("SELECT summary FROM exchanges")
        )
    finally:
        provider.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        recorder.close()
        store.close()


@pytest.mark.parametrize("bad_id", ["", "bad/identity", "x" * 65, "é", True, 12])
def test_history_coverage_invalid_writer_id_does_not_mint_capsule(tmp_path, bad_id):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "hc-bad-id.db", ROOT_KEY)
    try:
        eid = store.record(
            ExchangeRecord({"id": bad_id, "ts": time.time(), "session": "owned", "flags": []}, history_code=21)
        )
        raw = json.loads(store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (eid,)).fetchone()[0])
        assert "_history_coverage" not in raw
        _hc_assert_public([store.detail(eid)["summary"]], "unknown")
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad_capsule",
    [None, 12, True, [], "hc:l1:" + "0" * 63, "hc:l1:" + "0" * 65, "hc:p1:" + "0" * 64, "hc:l1:" + "G" * 64],
)
def test_history_coverage_local_predecode_checks_precede_crypto(tmp_path, monkeypatch, bad_capsule):
    from latet.storage.exchanges import ExchangeRecord, ExchangeStore

    store = ExchangeStore(tmp_path / "hc-predecode.db", ROOT_KEY)
    try:
        eid = store.record(ExchangeRecord({"ts": time.time(), "session": "owned", "flags": []}))
        raw = json.loads(store._db.execute("SELECT summary FROM exchanges WHERE id = ?", (eid,)).fetchone()[0])
        raw["_history_coverage"] = bad_capsule
        store._db.execute("UPDATE exchanges SET summary = ? WHERE id = ?", (json.dumps(raw), eid))

        class ForbiddenAEAD:
            def decrypt(self, *args):
                raise AssertionError("unbounded or malformed coverage capsule reached crypto")

        monkeypatch.setattr(store, "_aead", ForbiddenAEAD())
        _hc_assert_public(_hc_views(store, eid), "unknown")
    finally:
        store.close()


def test_history_coverage_actual_bridge_keeps_only_existing_observations(tmp_path, audit_space, monkeypatch):
    from starlette.testclient import TestClient

    from latet.bridge import api as module
    from latet.gateway.app import create_app
    from latet.gateway.recorder import Recorder
    from latet.storage.exchanges import ExchangeStore

    from .test_request_observations import ENGINE, SECRET
    from .test_request_outcomes import EMAIL

    _spaces, _space, runtime = audit_space
    store = ExchangeStore(tmp_path / "hc-bridge.db", ROOT_KEY)
    recorder = Recorder(store, runtime.mediator.engine, history="metadata")
    runtime.gateway.bridge_token = "p25-owned-token"
    runtime.gateway.recorder = recorder
    original_observed, original_session = module.observed, module.Bridge._session
    observations, sessions = [], []

    def observed(kind, *args, **kwargs):
        observations.append(kind)
        return original_observed(kind, *args, **kwargs)

    def session(self, data, *, register=True):  # Bridge._session's signature: route-check reads without registering
        sessions.append(data.get("session"))
        return original_session(self, data, register=register)

    monkeypatch.setattr(module, "observed", observed)
    monkeypatch.setattr(module.Bridge, "_session", session)
    try:
        with TestClient(create_app(runtime.gateway), base_url="http://127.0.0.1:5283") as client:
            headers = {"x-latet-token": runtime.gateway.bridge_token}
            masked = client.post(
                ENGINE + "/mask", headers=headers, json={"session": TOOL, "items": [{"text": f"contact {EMAIL}"}]}
            )
            fake = masked.json()["items"][0]["text"].removeprefix("contact ")
            route = client.post(
                ENGINE + "/route-check",
                headers=headers,
                json={"session": TOOL, "env": {}, "sources": {}, "entrypoint": "cli"},
            )
            assert route.status_code == 200 and not route.json()["ok"]
            outgoing = client.post(
                ENGINE + "/outbound", headers=headers, json={"session": TOOL, "tool": TOOL, "values": [fake]}
            )
            assert outgoing.status_code == 200 and outgoing.json()["found"]
            final = client.post(
                ENGINE + "/verify", headers=headers, json={"session": TOOL, "slots": [{"text": SECRET}]}
            )
            assert final.status_code == 200 and final.json()["blocking"]
        assert recorder.drain(5)
        assert observations == ["route-check", "outbound", "final-check"]
        assert sessions == [TOOL] * 4  # existing mask/route-check/outbound/verify, no coverage session
        rows, _ = store.list()
        assert len(rows) == 3 and len(_producer_events(runtime.request_audit)) == 3
        _hc_assert_public(rows, "unknown")
        assert all(
            "_history_coverage" not in json.loads(row[0]) for row in store._db.execute("SELECT summary FROM exchanges")
        )
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize("format_name", ["text", "json"])
async def test_admitted_custom_websocket_warning_omits_destination_in_server_logs(
    outbound_owner, tmp_path, monkeypatch, caplog, format_name
):
    """A store failure on an admitted custom WS must not expose its private prefix in ordinary logs."""
    import threading

    from latet.config import Destination
    from latet.gateway import request_profiles, ws
    from latet.gateway.capture import Capture
    from latet.gateway.routes import RouteTable, destination_upstream
    from latet.server.logs import JsonFormatter, TextFormatter

    from .test_request_outcomes import _Client

    marker = "customer-merger-project"
    path = "/customer-merger-project/v1/responses"
    destination = Destination(name=marker, url="https://upstream.invalid", request_profile="standard")
    route = destination_upstream(destination)
    client = _Client()
    client.socket.scope["path"] = path
    client.socket.scope["raw_path"] = path.encode("ascii")
    # This failure needs no resource-bearing session header; the real profile admits the Host-only handshake.
    client.socket.scope["headers"] = [(b"host", b"127.0.0.1:5283")]
    admissions, reads = [], []
    admit = request_profiles.admit
    caller = threading.get_ident()

    def admitted(raw, upstream):
        result = admit(raw, upstream)
        admissions.append(result.profile)
        return result

    def unavailable_session(_key):
        reads.append(threading.get_ident())
        raise OSError("owned conversation store unavailable")

    monkeypatch.setattr(request_profiles, "admit", admitted)
    monkeypatch.setattr(outbound_owner.engine, "session", unavailable_session)
    directory = tmp_path / "authorized-capture"
    try:
        with caplog.at_level(logging.WARNING, logger="latet.gateway.ws"):
            await asyncio.wait_for(ws.relay(client.socket, RouteTable([route]), Capture(directory), outbound_owner), 3)
        assert len(admissions) == 1
        profile = admissions[0]
        assert (profile.family, profile.method, profile.template, profile.bodyless) == (
            "openai",
            "GET",
            "/{destination}/v1/responses",
            True,
        )
        assert len(reads) == 1 and reads[0] != caller, "actual mediator worker must reach the store-failure seam"
        assert client.sent == [{"type": "websocket.close", "code": 1011, "reason": ws._UNREAD_CONVERSATION}]
        assert client.socket.scope["path"] == path and route.prefix == "/customer-merger-project"
        [captured] = list(directory.glob("*.jsonl"))
        events = [json.loads(line) for line in captured.read_text("utf-8").splitlines()]
        assert [event["t"] for event in events] == ["open", "request", "end"]
        assert events[0]["upstream"] == marker and events[0]["kind"] == "ws"
        assert events[0]["method"] == "GET" and events[0]["url"] == "wss://upstream.invalid/v1/responses"
        # A raw capture keeps a failure by a fixed category alone, never a transport's string (capture.response_end).
        assert events[-1]["error"] == "response-error", "authorized capture must say the answer failed"
        warnings = [record for record in caplog.records if record.name == "latet.gateway.ws"]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        formatter = TextFormatter() if format_name == "text" else JsonFormatter()
        rendered = formatter.format(warnings[0])
        assert "not connected" in rendered and "OSError" in rendered, "real warning/category must remain visible"
        assert marker not in rendered and path not in rendered, "custom destination escaped into ordinary safe logs"
    finally:
        outbound_owner.executor.shutdown(wait=True)
        outbound_owner.bridge_executor.shutdown(wait=True)


@pytest.mark.parametrize("format_name", ["text", "json"])
async def test_task11_application_request_log_keeps_positive_categorical_witness(caplog, format_name):
    """The old Task11 scenario's positive access witness remains useful after path categorization."""
    from latet.server.logs import JsonFormatter, TextFormatter

    raw_path = "/_latet/apps/v1/mask"
    scope = {
        "type": "http",
        "method": "POST",
        "path": raw_path,
        "raw_path": raw_path.encode("ascii"),
        "query_string": b"",
        "headers": [],
    }
    messages = []

    async def application(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    with caplog.at_level(logging.INFO, logger="latet.server.requests"):
        await RequestLog(application)(scope, receive, send)
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    records = [record for record in caplog.records if record.name == "latet.server.requests"]
    assert len(records) == 1
    record = records[0]
    assert (record.method, record.path, record.status) == ("POST", "/applications", 200)
    formatter = TextFormatter() if format_name == "text" else JsonFormatter()
    rendered = formatter.format(record)
    assert "POST /applications 200" in rendered
    assert raw_path not in rendered
    if format_name == "json":
        line = json.loads(rendered)
        assert (line["method"], line["path"], line["status"]) == ("POST", "/applications", 200)


@pytest.fixture
def native_qualification_inputs(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from ..d2_qualification import PROFILE

    lua = "-- inert qualification fixture\nreturn 1"
    output = tmp_path / "qualification-evidence"
    output.mkdir()
    node = "tests/server/test_request_audit_privacy.py::test_d2_native_prefix128_large_small_large"
    endpoint = {"host": "127.0.0.1", "port": 56379, "db": 0}
    expected = {
        "run_id": "a" * 40,
        "process_id": 7,
        "valkey_version": "9.1.2",
        "redis_version": "7.2.4",
        "redis_build_id": "inert-build",
        "arch_bits": 64,
        "endpoint_sha256": hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode("ascii")).hexdigest(),
        "io_threads": 1,
    }
    manifest = {
        "schema": 1,
        "profile": PROFILE,
        "lua_sha256": hashlib.sha256(lua.encode("utf-8")).hexdigest(),
        "helper_sha256": "b" * 64,
        "output_root": str(output.resolve()),
        "config_reads": ["io-threads"],
        "nodes": [node],
        "expected": expected,
    }

    class InertClient:
        def __init__(self):
            self.connection_pool = SimpleNamespace(connection_kwargs=endpoint)
            self.server = {
                key: value for key, value in expected.items() if key not in ("endpoint_sha256", "io_threads")
            }
            self.server["uptime_in_seconds"] = 20
            self.calls = []
            self.allow_config = False

        def info(self, *, section):
            self.calls.append(("info", section))
            return dict(self.server)

        def config_get(self, key):
            self.calls.append(("config_get", key))
            assert self.allow_config, "unauthorized input reached even the inert CONFIG boundary"
            return {"io-threads": "1"}

    client = InertClient()
    bus = SimpleNamespace(client=client, prefix="latet_t_0123456789:")
    return bus, lua, manifest, output, node


@pytest.mark.parametrize("failure", ["not-admitted", "manifest", "lua", "profile", "root", "config", "case"])
def test_native_qualification_rejects_unauthorized_inputs_before_client_or_evidence(
    native_qualification_inputs, failure
):
    from ..d2_qualification import NativeObserver

    bus, lua, manifest, output, node = native_qualification_inputs
    admitted = failure != "not-admitted"
    if failure == "manifest":
        manifest = None
    elif failure == "lua":
        manifest["lua_sha256"] = "0" * 64
    elif failure == "profile":
        manifest["profile"] = "unqualified-custom-profile"
    elif failure == "root":
        manifest["output_root"] = str(output.parent)
    elif failure == "config":
        manifest["config_reads"] = []
    elif failure == "case":
        node = "tests/server/test_request_audit_privacy.py::test_d2_native_prune_batch_limit[64]"
    expected = {
        "not-admitted": "native qualification was not admitted",
        "manifest": "invalid qualification manifest fields",
        "lua": "Lua binding mismatch",
        "profile": "unadmitted native profile",
        "root": "output root mismatch",
        "config": "unadmitted CONFIG reads",
        "case": "case was not selected",
    }[failure]
    with pytest.raises(ValueError, match=f"^{expected}$"):
        NativeObserver(bus, lua=lua, manifest=manifest, output_root=output, case_id=node, admitted=admitted)
    assert bus.client.calls == []
    assert list(output.iterdir()) == [], "rejected qualification must not publish an artifact"


@pytest.mark.parametrize("failure", ["endpoint", "run", "build"])
def test_native_qualification_rejects_changed_endpoint_or_process_before_config(native_qualification_inputs, failure):
    from ..d2_qualification import NativeObserver

    bus, lua, manifest, output, node = native_qualification_inputs
    if failure == "endpoint":
        bus.client.connection_pool.connection_kwargs = {"host": "127.0.0.1", "port": 56380, "db": 0}
    else:
        bus.client.server["run_id" if failure == "run" else "redis_build_id"] = (
            "c" * 40 if failure == "run" else "different-build"
        )
    expected = {
        "endpoint": "endpoint binding mismatch",
        "run": "run or build binding mismatch",
        "build": "run or build binding mismatch",
    }[failure]
    with pytest.raises(ValueError, match=f"^{expected}$"):
        NativeObserver(bus, lua=lua, manifest=manifest, output_root=output, case_id=node, admitted=True)
    assert bus.client.calls == ([] if failure == "endpoint" else [("info", "server")])
    assert list(output.iterdir()) == []


def test_native_qualification_admitted_control_uses_exact_reads_and_finalizes_once(native_qualification_inputs):
    from ..d2_qualification import NativeObserver

    bus, lua, manifest, output, node = native_qualification_inputs
    bus.client.allow_config = True
    observer = NativeObserver(bus, lua=lua, manifest=manifest, output_root=output, case_id=node, admitted=True)
    assert list(output.iterdir()) == []
    observer.sample(
        "control",
        {
            "label": "control",
            "reservations": 0,
            "reported_string_bytes": 0,
            "max_pair_with_R80_slot_charge": 0,
            "keys": {},
        },
    )
    observer.close()
    observer.close()
    assert bus.client.calls == [("info", "server"), ("config_get", "io-threads"), ("info", "server")] * 2
    files = list(output.glob("p25-native-*.json"))
    assert len(files) == 2, "one sample plus one final context, even after repeated close"
    reports = [json.loads(path.read_text("utf-8")) for path in files]
    assert all(report["case_id"] == node and report["lua_sha256"] == manifest["lua_sha256"] for report in reports)
    [sample] = [report for report in reports if "keys" in report]
    assert sample["keys"] == {} and sample["context"]["source_to_endpoint_qualified"] is False
