"""P2-5 backend sink witness, held for separate namespace/runtime admission.

This module is NOT part of the initial backend-free job.
"""

import hashlib
import json
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from latet.server.history import PgHistoryStore
from latet.storage.exchanges import ExchangeRecord

pytestmark = pytest.mark.server

ROOT_KEY = b"\x07" * 32


def test_server_history_indexes_never_keep_a_client_tool_name(server):
    """Catches sensitive tool/upstream strings escaping into clear indexed columns."""
    identity, _secret = server.identities.add_key("P25 owned fixture")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    marker = "P25_PRIVATE_INDEXED_TOOL_21763"
    upstream = "P25_PRIVATE_INDEXED_UPSTREAM_73126"
    try:
        exchange_id = store.record(
            ExchangeRecord(
                {
                    "ts": time.time(),
                    "session": "p25-owned",
                    "agent": "other",
                    "flags": ["outbound:" + marker],
                    "upstream": upstream,
                    "new_masked": 0,
                }
            )
        )
        with server.db.connection() as connection:
            row = connection.execute(
                "SELECT flags, upstream, summary FROM exchanges WHERE identity_id = %s AND id = %s",
                (identity.id, exchange_id),
            ).fetchone()
        assert row is not None, "privacy cannot pass by skipping the real server store"
        opened = store._open(bytes(row[2]), f"{exchange_id}:summary")
        assert opened["id"] == exchange_id, "the identity-sealed summary must remain readable"
        clear = json.dumps([row[0], row[1]])
        assert marker not in clear and upstream not in clear, "sensitive names reached indexed clear metadata"
    finally:
        store.close()


def test_server_sensitive_history_keeps_custom_destination_last_used(server):
    """Controls against collapsing destination identity when clear indexed fields become categorical."""
    identity, _secret = server.identities.add_key("P25 owned last-used fixture")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    from latet.config import Config, Destination

    configured = Config(
        upstreams=[
            Destination(name="acme-east", url="https://east.example"),
            Destination(name="acme-west", url="https://west.example"),
        ]
    )
    first, second = (d.name for d in configured.upstreams)
    try:
        for name, timestamp in ((first, 1_800_000_001.0), (second, 1_800_000_002.0)):
            store.record(
                ExchangeRecord(
                    {
                        "ts": timestamp,
                        "session": "p25-owned",
                        "agent": "other",
                        "flags": [],
                        "upstream": name,
                        "new_masked": 0,
                    },
                    destination_name=name,
                )
            )
        assert store.last_used((first, second)) == {first: 1_800_000_001.0, second: 1_800_000_002.0}
        assert store.last_used((second,)) == {second: 1_800_000_002.0}
        assert store.last_used([]) == {} and store.last_used() == {}
    finally:
        store.close()


@pytest.mark.parametrize("historical_refs", [False, True], ids=("full-envelope", "old-list"))
def test_server_refs_keep_identity_forgetting_and_exact_byte_accounting(server, historical_refs):
    """Full detail follows existing row sealing, entity index, forget and history-byte accounting."""
    from cryptography.exceptions import InvalidTag

    from latet.server.history import _KEPT
    from latet.storage.exchanges import BlockRecord, block_ref

    identity, _secret = server.identities.add_key("P25 owned refs fixture")
    other_identity, _other_secret = server.identities.add_key("P25 other refs fixture")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    other = PgHistoryStore(server.db, other_identity.id, ROOT_KEY)
    marker = "P25_PRIVATE_DETAIL_21739"
    entity = "e" + "7" * 19
    block = BlockRecord(
        store.block_id("masked body"),
        "$.input[0]",
        "user_text",
        False,
        True,
        "masked body",
        [],
        tool="private-tool",
        source="masked source",
        source_found=[{"entity": entity, "masked_start": 0, "masked_end": 6}],
    )
    try:
        rec = ExchangeRecord(
            {"ts": time.time(), "session": "owned", "flags": []},
            [block],
            sensitive=None if historical_refs else {"model": marker},
        )
        exchange_id = store.record(rec)
        with server.db.transaction() as connection:
            original, original_summary = connection.execute(
                "SELECT refs, summary FROM exchanges WHERE identity_id = %s AND id = %s",
                (identity.id, exchange_id),
            ).fetchone()
            if historical_refs:
                sealed = store._seal([block_ref(block)], f"{exchange_id}:refs")
                historical = store._open(bytes(original_summary), f"{exchange_id}:summary")
                historical.pop("history_group_version", None)
                historical.pop("_history_coverage", None)
                historical.update(
                    {
                        "session": "owned",
                        "model": marker,
                        "path": "/legacy/private-path",
                        "agent_version": "legacy-version",
                    }
                )
                sealed_summary = store._seal(historical, f"{exchange_id}:summary")
                connection.execute(
                    "UPDATE exchanges SET refs = %s, summary = %s WHERE identity_id = %s AND id = %s",
                    (sealed, sealed_summary, identity.id, exchange_id),
                )
                store._counted(connection, len(sealed) + len(sealed_summary) - len(original) - len(original_summary))
            else:
                sealed = bytes(original)
            connection.execute(
                "UPDATE exchanges SET entity_keys = NULL WHERE identity_id = %s AND id = %s",
                (identity.id, exchange_id),
            )
        assert other.detail(exchange_id) is None
        with pytest.raises(InvalidTag):
            other._open(bytes(sealed), f"{exchange_id}:refs")
        with pytest.raises(InvalidTag):
            store._open(bytes(sealed), f"{exchange_id}:response")
        detail = store.detail(exchange_id)
        [slot] = detail["request"]["slots"]
        assert slot["source"] == [block.source, block.source_found] and slot["masked"] == "masked body"
        if historical_refs:
            assert detail["sensitive"] == {
                "agent_version": "legacy-version",
                "upstream": "unknown",
                "host": None,
                "path": "/legacy/private-path",
                "model": marker,
                "block_reason": None,
                "session": "owned",
                "project": None,
                "outbound": [],
            }
            assert marker not in json.dumps(store.list()[0])
            with server.db.connection() as connection:
                kept = connection.execute(
                    "SELECT refs, summary FROM exchanges WHERE identity_id = %s AND id = %s",
                    (identity.id, exchange_id),
                ).fetchone()
            assert tuple(map(bytes, kept)) == (bytes(sealed), bytes(sealed_summary))
        else:
            assert detail["sensitive"]["model"] == marker
            assert marker not in json.dumps(store.list()[0])
        assert store.index_entities() == 1
        with server.db.connection() as connection:
            keys, kept_sealed = connection.execute(
                "SELECT entity_keys, refs FROM exchanges WHERE identity_id = %s AND id = %s",
                (identity.id, exchange_id),
            ).fetchone()
            counted = connection.execute(
                "SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)
            ).fetchone()[0]
            actual = connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0]
        assert list(map(bytes, keys)) == store._entity_keys([entity])
        assert bytes(kept_sealed) == bytes(sealed) and counted == actual > 0
        with server.db.transaction() as connection:
            assert store.forget_entities(connection, [entity]) == 1
        assert store.detail(exchange_id) is None
        # A source-only reference removes its exchange, while its independently kept body remains.
        with server.db.connection() as connection:
            actual = connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0]
            counted = connection.execute(
                "SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)
            ).fetchone()[0]
        assert actual == counted > 0
        store.clear()
        with server.db.connection() as connection:
            assert connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0] == 0
            assert (
                connection.execute("SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)).fetchone()[
                    0
                ]
                == 0
            )
    finally:
        store.close()
        other.close()


def test_server_private_detail_obeys_existing_retention_and_clear(server):
    """The new envelope is included in the same exchange row's bytes, prune and clear."""
    from latet.server.history import _KEPT

    identity, _secret = server.identities.add_key("P25 private retention fixture")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    now = time.time()
    try:
        ids = [
            store.record(
                ExchangeRecord(
                    {"ts": at, "session": "owned", "flags": []},
                    sensitive={"model": "P25_PRIVATE_RETAINED_19732"},
                )
            )
            for at in (now - 15 * 86400, now)
        ]
        assert store.detail(ids[0])["sensitive"]["model"] == "P25_PRIVATE_RETAINED_19732"
        assert store.detail(ids[1])["sensitive"]["model"] == "P25_PRIVATE_RETAINED_19732"
        with server.db.connection() as connection:
            actual = connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0]
            counted = connection.execute(
                "SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)
            ).fetchone()[0]
        assert actual == counted > 0
        assert store.prune(now=now) == 1
        assert store.detail(ids[0]) is None and store.detail(ids[1]) is not None
        with server.db.connection() as connection:
            actual = connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0]
            counted = connection.execute(
                "SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)
            ).fetchone()[0]
        assert actual == counted > 0
        store.clear()
        assert store.detail(ids[1]) is None
        with server.db.connection() as connection:
            assert connection.execute(_KEPT, {"identity": identity.id}).fetchone()[0] == 0
            assert (
                connection.execute("SELECT bytes FROM history_bytes WHERE identity_id = %s", (identity.id,)).fetchone()[
                    0
                ]
                == 0
            )
    finally:
        store.close()


def test_server_destination_indexes_bind_identity_and_preserve_raw_legacy_rows(server):
    """The same ROOT_KEY and current name yield distinct private indexes and only own legacy/new maxima."""
    from latet.config import Destination

    name = Destination(name="acme-east", url="https://east.example").name
    identities = [server.identities.add_key("P25 destination identity " + str(n))[0] for n in range(2)]
    stores = [PgHistoryStore(server.db, identity.id, ROOT_KEY) for identity in identities]
    try:
        tokens = []
        for n, store in enumerate(stores):
            exchange_id = store.record(
                ExchangeRecord(
                    {"ts": float(n + 1), "session": "owned", "flags": [], "upstream": name},
                    destination_name=name,
                )
            )
            legacy_id = store.record(
                ExchangeRecord(
                    {"ts": float(n + 10), "session": "owned", "flags": [], "upstream": name},
                )
            )
            with server.db.transaction() as connection:
                tokens.append(
                    connection.execute(
                        "SELECT upstream FROM exchanges WHERE identity_id = %s AND id = %s",
                        (store.identity, exchange_id),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE exchanges SET upstream = %s WHERE identity_id = %s AND id = %s",
                    (name, store.identity, legacy_id),
                )
            with server.db.connection() as connection:
                before = connection.execute(
                    "SELECT id, upstream, summary, refs FROM exchanges WHERE identity_id = %s ORDER BY id",
                    (store.identity,),
                ).fetchall()
            assert any(row[0] == legacy_id and row[1] == name for row in before)
            assert store.last_used([name]) == {name: float(n + 10)}
            assert store.last_used() == {} and store.last_used([]) == {}
            with server.db.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT id, upstream, summary, refs FROM exchanges WHERE identity_id = %s ORDER BY id",
                        (store.identity,),
                    ).fetchall()
                    == before
                )
        assert tokens[0].startswith("dest:v1:") and tokens[1].startswith("dest:v1:")
        assert tokens[0] != tokens[1] and all(name not in token for token in tokens)
    finally:
        for store in stores:
            store.close()


def test_server_history_groups_keep_legacy_bytes_and_reject_foreign_handles(server):
    """PG canonical handles use associated existing C and identity predicates, including shared-root stress."""
    from latet.config import Config, IdentityConfig
    from latet.engine.vault import Vault
    from latet.projects import Projects

    identities = [server.identities.add_key("P25 history group " + str(n))[0] for n in range(2)]
    stores = [PgHistoryStore(server.db, identity.id, ROOT_KEY) for identity in identities]
    source = "x-session-id:P25_PRIVATE_SERVER_SESSION"
    projects = Projects(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY), files=False)
    project = projects.resolve("C:/P25_PRIVATE_PROJECT_FOLDER")
    groups = [store.history_group(source) for store in stores]
    try:
        assert groups[0] != groups[1] and groups[0][6:70] == groups[1][6:70]  # accepted same-root C correlation
        for n, store in enumerate(stores):
            old_id = store.record(ExchangeRecord({"ts": float(n + 1), "session": source, "flags": []}))
            historical = {
                "id": old_id,
                "ts": float(n + 1),
                "session": source,
                "flags": ["blocked"],
                "new_masked": 1,
                "mode": "verify",
                "verified": True,
                "model": "P25_PRIVATE_OLD_SERVER_MODEL",
                "project": {"id": project.id, "name": "P25_PRIVATE_PROJECT_FOLDER"},
            }
            sealed = store._seal(historical, f"{old_id}:summary")
            with server.db.transaction() as connection:
                original = bytes(
                    connection.execute(
                        "SELECT summary FROM exchanges WHERE identity_id = %s AND id = %s",
                        (store.identity, old_id),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE exchanges SET summary = %s, flags = %s, project = %s, new_masked = 1"
                    " WHERE identity_id = %s AND id = %s",
                    (sealed, " blocked ", store._keyed("project", project.id), store.identity, old_id),
                )
                store._counted(connection, len(sealed) - len(original))
            with server.db.connection() as connection:
                before = connection.execute(
                    "SELECT session, project, summary, refs FROM exchanges WHERE identity_id = %s AND id = %s",
                    (store.identity, old_id),
                ).fetchone()
            assert store._open(bytes(before[2]), f"{old_id}:summary")["model"] == "P25_PRIVATE_OLD_SERVER_MODEL"
            assert before[0] == store._keyed("session", source) and before[1] == store._keyed("project", project.id)
            new_id = store.record(
                ExchangeRecord(
                    {"ts": float(n + 10), "session": groups[n], "flags": [], "new_masked": 2},
                    session_source=source,
                    project_id=project.id,
                )
            )
            [group] = store.sessions()
            assert (
                group["key"] == groups[n] and group["exchanges"] == 2 and group["masked"] == 3 and group["blocked"] == 1
            )
            assert store.in_order(source) == store.in_order(groups[n]) == [old_id, new_id]
            assert {row["id"] for row in store.list(session=groups[n], project=project.id)[0]} == {old_id, new_id}
            assert store.list(session=groups[1 - n])[0] == [] and store.in_order(groups[1 - n]) == []
            for rows in (store.list()[0], store.summaries_since(0), *store.summaries_parts(0, 10)):
                assert all(
                    row["session"] == groups[n] and row["project"] == {"id": project.id, "name": project.id}
                    for row in rows
                )
                assert "P25_PRIVATE_" not in json.dumps(rows)
            assert store.detail(old_id)["summary"]["coverage"] == "unknown"
            with server.db.connection() as connection:
                after = connection.execute(
                    "SELECT session, project, summary, refs FROM exchanges WHERE identity_id = %s AND id = %s",
                    (store.identity, old_id),
                ).fetchone()
            assert tuple(bytes(value) if isinstance(value, memoryview) else value for value in after) == tuple(
                bytes(value) if isinstance(value, memoryview) else value for value in before
            )
    finally:
        for store in stores:
            store.close()


@pytest.fixture
def d2_observer(request):
    """Ordinary regression runs never import or instantiate controller qualification support."""
    if request.config.pluginmanager.get_plugin("p25_native_qualification") is None:
        return None
    return request.getfixturevalue("p25_native_observer")


def test_d2_actual_atomic_numeric_row_and_owned_four_families(bus, d2_observer):
    """Owned synthetic protocol IDs only; actual Lua/Valkey, no PostgreSQL account or server process."""
    from latet.gateway.request_audit import RequestAudit
    from latet.server.live import ValkeyRequestStatus

    from ..unit.test_request_audit import _audit_record

    audit = RequestAudit()
    assert audit.record(_audit_record(), bucket=1)
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker()
    try:
        assert work.build(removed_headers=19, server=True)
        transport = ValkeyRequestStatus(bus, "u_aaaaaaaaaa", "0123456789ab")
        assert transport.publish(work)
        keys = transport._keys
        assert [bus.client.type(key) for key in keys] == [b"string"] * 4
        assert [bus.client.strlen(key) for key in keys[:3]] == [32, 32, 22]
        assert all(bus.client.pttl(key) == -1 for key in keys[:3])
        raw = bus.client.get(keys[3])
        assert raw.endswith(work._d2_payload)
        assert len(raw) <= 32768 and raw[13:14] == b"\n"
        seconds, micros = bus.client.time()
        now = seconds * 1000 + micros // 1000
        assert 0 < int(raw[:13]) - now <= 30000
        assert 0 < bus.client.pttl(keys[3]) <= 30000
        assert transport.aggregate(work)
        projection = work.decode()
        server = projection["server"]
        assert server["available"] and server["complete"] and server["published"]
        assert server["contributors"] == 1 and server["counts"] == projection["local"]["counts"]
        assert server["removed_headers"] == projection["local"]["removed_headers"] == 19
        assert sum(bus.client.memory_usage(key, samples=0) for key in keys) <= 65536
        if d2_observer is not None:
            d2_observer.sample("atomic-published", {"reservations": 1})
        # This is persistent native key usage for this actual legal row only;
        # NOT a Valkey transient peak/all-pairs/maximum-row qualification.
        before = [bus.client.get(key) for key in keys]
        bus.client.set(keys[0], b"malformed-reservation")
        corrupt = [bus.client.get(key) for key in keys]
        assert not transport.publish(work)
        assert [bus.client.get(key) for key in keys] == corrupt
        assert before[1:] == corrupt[1:]
    finally:
        work.worker_finished()
        work.close()
        audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize("distinct", [False, True], ids=("all-max-wire", "distinct-near-max-native"))
def test_d2_actual_maximum_legal_schema_persistent_native_and_owned_work(bus, distinct):
    """Synthetic closed protocol schema; actual Lua/native keys, never a producer/authority claim."""
    import tracemalloc

    from latet.gateway.request_audit import RequestAudit
    from latet.server.live import ValkeyRequestStatus

    from ..unit.test_request_audit import _d2_maximum_local, _owned_audit_shape

    audit = RequestAudit()
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker() and work.build(server=True)
    work._projection["local"] = _d2_maximum_local(distinct)
    baseline = _owned_audit_shape(audit)  # warm exact helper imports BEFORE tracing
    assert not tracemalloc.is_tracing()
    tracemalloc.start()
    transport = decoded = None
    try:
        transport = ValkeyRequestStatus(bus, "u_aaaaaaaaaa", "0123456789ab")
        assert transport.publish(work)
        work._d2_raw = bus.client.get(transport._keys[3])
        assert len(work._d2_raw) == 7618 and work._d2_raw.endswith(work._d2_payload)
        assert transport.aggregate(work)
        assert len(work.encoded) <= 32768
        decoded = work.decode()
        assert decoded is not None and work.encode_output(decoded)
        assert decoded["server"]["contributors"] == 1 and not decoded["server"]["complete"]
        assert decoded["server"]["removed_headers"] == decoded["local"]["removed_headers"]
        persistent = sum(bus.client.memory_usage(key, samples=0) for key in transport._keys)
        assert persistent <= 65536  # exact key allocations ONLY, not Lua/Valkey transient peak
        native = _owned_audit_shape(audit)
        current, peak = tracemalloc.get_traced_memory()
        assert native <= audit.retained_bytes
        assert native + max(0, peak - current) <= audit.retained_bytes
        assert baseline + peak <= audit.retained_bytes
    finally:
        tracemalloc.stop()
        decoded = transport = None
        work.worker_finished()
        work.close()
        audit.close()
    assert audit.retained_bytes == 0


@pytest.mark.parametrize("acknowledged", [True, False], ids=("ack-then-read-fails", "real-write-lost-reply"))
def test_d2_actual_ack_is_distinct_from_row_existence_and_unavailable_read(bus, monkeypatch, acknowledged):
    from latet.gateway.request_audit import RequestAudit
    from latet.server.live import ValkeyRequestStatus

    audit = RequestAudit()
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker() and work.build(removed_headers=7, server=True)
    try:
        transport = ValkeyRequestStatus(bus, "u_aaaaaaaaaa", "0123456789ab")
        original = ValkeyRequestStatus._call

        def called(self, mode, argument):
            result = original(self, mode, argument)  # actual Lua/row-last completes
            if self is transport and mode == "p" and not acknowledged:
                raise ConnectionError("owned lost-reply fixture")
            return result

        monkeypatch.setattr(ValkeyRequestStatus, "_call", called)
        assert transport.publish(work) is acknowledged
        assert bus.client.exists(transport._keys[3]) == 1  # lost ACK NEVER means no row
        bus.client.set(transport._keys[0], b"malformed-reservation")
        assert not transport.aggregate(work)
        assert work.decode()["server"] == {
            "available": False,
            "complete": False,
            "contributors": 0,
            "counts": None,
            "overflow": None,
            "removed_headers": None,
            "saturated": False,
            "published": acknowledged,
        }
        assert work.decode()["local"]["removed_headers"] == 7
    finally:
        work.worker_finished()
        work.close()
        audit.close()


def test_d2_actual_binary_v2_rejects_old_and_structurally_oversized_rows_before_read(bus):
    from latet.gateway.request_audit import RequestAudit, _d2_encode
    from latet.server.live import ValkeyRequestStatus

    from ..unit.test_request_audit import _d2_maximum_local

    audit = RequestAudit()
    work = audit.reserve_snapshot()
    assert work is not None and work.claim_worker() and work.build(server=True)
    try:
        transport = ValkeyRequestStatus(bus, "u_aaaaaaaaaa", "0123456789ab")
        assert transport.publish(work)
        before = [bus.client.get(key) for key in transport._keys]
        valid = _d2_encode(_d2_maximum_local())
        for payload in (
            bytes((1,)) + valid[1:],
            valid + b"\x00",
            b'[1,"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",0,0,0,0,0,[]]',
        ):
            assert transport._call("p", payload) == [0]
            assert [bus.client.get(key) for key in transport._keys] == before
        for payload in (valid + b"\x00", b'[1,"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",0,0,0,0,0,[]]'):
            row = before[3][:14] + payload
            if payload.startswith(b"\x02"):
                assert len(row) == 7619
            bus.client.set(transport._keys[3], row, px=30000)
            assert transport._call("d", b"") == [0]  # STRLEN/version structural rejection
            assert transport._call("p", valid) == [0]  # no old-wire repair/overwrite
            assert bus.client.strlen(transport._keys[3]) == len(row)
            assert [bus.client.get(key) for key in transport._keys[:3]] == before[:3]
    finally:
        work.worker_finished()
        work.close()
        audit.close()
    assert audit.retained_bytes == 0


def _hc_pg_views(store, exchange_id):
    return [
        next(row for row in store.list()[0] if row["id"] == exchange_id),
        store.detail(exchange_id)["summary"],
        next(row for row in store.summaries_since(0) if row["id"] == exchange_id),
        next(row for part in store.summaries_parts(0, 1000) for row in part if row["id"] == exchange_id),
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "unknown"),
        (21, "complete-accounting"),
        (25, "complete-accounting"),
        (26, "legacy-uninspected"),
        (28, "unknown"),
    ],
)
def test_server_history_coverage_capsule_identity_and_byte_count(server, code, expected):
    from latet.server.history import _KEPT

    identities = [server.identities.add_key("P25 coverage " + str(n))[0] for n in range(2)]
    stores = [PgHistoryStore(server.db, identity.id, ROOT_KEY) for identity in identities]
    first, other = stores
    try:
        eid = first.record(
            ExchangeRecord({"id": "ex_same", "ts": time.time(), "session": "owned", "flags": []}, history_code=code)
        )
        other.record(ExchangeRecord({"id": eid, "ts": time.time(), "session": "other", "flags": []}))
        for row in _hc_pg_views(first, eid):
            assert row["coverage"] == expected and row["verified"] is False
            assert "_history_coverage" not in row and "history_code" not in row
        for row in _hc_pg_views(other, eid):
            assert row["coverage"] == "unknown" and row["verified"] is False
        with server.db.connection() as connection:
            raw = first._open(
                bytes(
                    connection.execute(
                        "SELECT summary FROM exchanges WHERE identity_id = %s AND id = %s",
                        (first.identity, eid),
                    ).fetchone()[0]
                ),
                f"{eid}:summary",
            )
            actual = connection.execute(_KEPT, {"identity": first.identity}).fetchone()[0]
            counted = connection.execute(
                "SELECT bytes FROM history_bytes WHERE identity_id = %s", (first.identity,)
            ).fetchone()[0]
        assert counted == actual > 0
        if code:
            assert len(raw["_history_coverage"]) == 74
            assert raw["_history_coverage"].startswith("hc:p1:")
        else:
            assert "_history_coverage" not in raw
        # Same ROOT_KEY and physical id still cannot replay a capsule across identity.
        if code:
            rebound = dict(raw)
            sealed = other._seal(rebound, f"{eid}:summary")  # a valid outer seal is not coverage provenance
            with server.db.transaction() as connection:
                old = connection.execute(
                    "SELECT summary FROM exchanges WHERE identity_id = %s AND id = %s", (other.identity, eid)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE exchanges SET summary = %s WHERE identity_id = %s AND id = %s",
                    (sealed, other.identity, eid),
                )
                other._counted(connection, len(sealed) - len(old))
            for row in _hc_pg_views(other, eid):
                assert row["coverage"] == "unknown" and row["verified"] is False
        first.clear()
        assert first.detail(eid) is None and other.detail(eid) is not None
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize(
    "corruption", ["legacy", "tag", "long", "uppercase", "format", "version", "triple", "rebound", "summary-id"]
)
def test_server_history_coverage_invalid_capsule_is_unknown_without_repair(server, corruption):
    identity, _secret = server.identities.add_key("P25 bad coverage")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    try:
        eid = store.record(
            ExchangeRecord({"id": "ex_owned", "ts": time.time(), "session": "owned", "flags": []}, history_code=21)
        )
        other_id = store.record(
            ExchangeRecord({"id": "ex_other", "ts": time.time(), "session": "owned", "flags": []}, history_code=25)
        )
        with server.db.connection() as connection:
            blobs = connection.execute(
                "SELECT id, summary FROM exchanges WHERE identity_id = %s ORDER BY id", (identity.id,)
            ).fetchall()
        opened = {key: store._open(bytes(blob), f"{key}:summary") for key, blob in blobs}
        raw = opened[eid]
        capsule = raw["_history_coverage"]
        if corruption == "legacy":
            raw.pop("_history_coverage")
            raw.update(coverage="complete-accounting", verified=True, history_code=21)
        elif corruption in ("version", "triple"):
            payload = b"\x02\x01\x01\x01" if corruption == "version" else b"\x01\x01\x03\x01"
            raw["_history_coverage"] = (
                "hc:p1:" + store._sealer.seal(payload, store.identity + ":history-coverage:v1:" + eid).hex()
            )
        elif corruption == "summary-id":
            raw["id"] = other_id
        else:
            raw["_history_coverage"] = {
                "tag": capsule[:-1] + ("0" if capsule[-1] != "0" else "1"),
                "long": capsule + "0",
                "uppercase": "hc:p1:" + "A" * 68,
                "format": "hc:p1:" + "ff" + capsule[8:],
                "rebound": opened[other_id]["_history_coverage"],
            }[corruption]
        sealed = store._seal(raw, f"{eid}:summary")
        with server.db.transaction() as connection:
            old = connection.execute(
                "SELECT summary FROM exchanges WHERE identity_id = %s AND id = %s", (identity.id, eid)
            ).fetchone()[0]
            connection.execute(
                "UPDATE exchanges SET summary = %s WHERE identity_id = %s AND id = %s", (sealed, identity.id, eid)
            )
            store._counted(connection, len(sealed) - len(old))
        with server.db.connection() as connection:
            before = connection.execute(
                "SELECT * FROM exchanges WHERE identity_id = %s ORDER BY id", (identity.id,)
            ).fetchall()
        assert store.detail(eid)["summary"]["coverage"] == "unknown"
        # Verified is the sealed row's own recorded fact (the "legacy" case seals a claim of it), never coverage.
        assert store.detail(eid)["summary"]["verified"] is (corruption == "legacy")
        for row in store.list()[0] + store.summaries_since(0) + [r for p in store.summaries_parts(0, 1000) for r in p]:
            assert "_history_coverage" not in row and "history_code" not in row
            if row["coverage"] != "unknown":
                assert row["id"] == other_id
        with server.db.connection() as connection:
            assert (
                connection.execute(
                    "SELECT * FROM exchanges WHERE identity_id = %s ORDER BY id", (identity.id,)
                ).fetchall()
                == before
            )
    finally:
        store.close()


@pytest.mark.parametrize("history", ["metadata", "full"])
async def test_server_history_coverage_actual_forwarder_recorder_pipeline(server, tmp_path, history):
    import asyncio
    from types import SimpleNamespace

    import httpx
    from starlette.responses import StreamingResponse

    from latet.config import Config, IdentityConfig
    from latet.engine.build import build
    from latet.engine.vault import Vault
    from latet.gateway.capture import Capture
    from latet.gateway.forward import Forwarder
    from latet.gateway.mediate import Mediator
    from latet.gateway.recorder import Recorder
    from latet.gateway.routes import RouteTable

    from ..unit.test_request_leaks import _Provider, _request
    from ..unit.test_request_outcomes import OPENAI, _chat

    identity, _secret = server.identities.add_key("P25 actual PG producer")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    built = build(Config(ner="off", identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    feed, provider = [], _Provider()
    recorder = Recorder(store, mediator.engine, SimpleNamespace(publish=feed.append), history=history)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            gateway = Forwarder(
                RouteTable([OPENAI]), Capture(None), client, mediator, recorder=recorder, request_audit=None
            )
            response = await gateway.handle(_request(_chat("hello")))
            if isinstance(response, StreamingResponse):
                async for _chunk in response.body_iterator:
                    pass
        assert response.status_code == 200 and len(provider.sent) == 1
        assert await asyncio.to_thread(recorder.drain, 5)
        await asyncio.sleep(0)
        [row], cursor = store.list()
        assert cursor is None and len(feed) == 1
        for public in _hc_pg_views(store, row["id"]) + feed:
            assert public["coverage"] == "complete-accounting" and public["verified"] is True  # nothing left in clear
            assert "_history_coverage" not in public and "history_code" not in public
    finally:
        recorder.close()
        mediator.close()
        store.close()


@pytest.mark.parametrize(
    "bad_capsule",
    [None, 12, True, [], "hc:p1:" + "0" * 67, "hc:p1:" + "0" * 69, "hc:l1:" + "0" * 68, "hc:p1:" + "G" * 68],
)
def test_server_history_coverage_predecode_checks_precede_capsule_crypto(server, monkeypatch, bad_capsule):
    identity, _secret = server.identities.add_key("P25 bounded capsule decode")
    store = PgHistoryStore(server.db, identity.id, ROOT_KEY)
    try:
        eid = store.record(ExchangeRecord({"ts": time.time(), "session": "owned", "flags": []}))
        with server.db.connection() as connection:
            old = connection.execute(
                "SELECT summary FROM exchanges WHERE identity_id = %s AND id = %s", (identity.id, eid)
            ).fetchone()[0]
        raw = store._open(bytes(old), f"{eid}:summary")
        raw["_history_coverage"] = bad_capsule
        sealed = store._seal(raw, f"{eid}:summary")
        with server.db.transaction() as connection:
            connection.execute(
                "UPDATE exchanges SET summary = %s WHERE identity_id = %s AND id = %s", (sealed, identity.id, eid)
            )
            store._counted(connection, len(sealed) - len(old))
        original = store._sealer.open

        def open_only_existing_columns(blob, aad):
            assert ":history-coverage:v1:" not in aad, "malformed capsule reached the crypto decoder"
            return original(blob, aad)

        monkeypatch.setattr(store._sealer, "open", open_only_existing_columns)
        for row in _hc_pg_views(store, eid):
            assert row["coverage"] == "unknown" and row["verified"] is False
    finally:
        store.close()


_D2_ADMIT_CUTS = {
    "reserve": "redis.call('SET', KEYS[1], published_size)",
    "expiry": "redis.call('SET', KEYS[2], published_expiry)",
    "directory": "redis.call('SET', KEYS[3], published_directory)",
    "row": "redis.call('SET', KEYS[4], row, 'PXAT', string.format('%.0f', deadline))",
}
_D2_PRUNE_CUTS = {
    "row": "redis.call('DEL', rowkey(pair))",
    "directory": "replace(dirkey(id), raw)",
    "expiry": "replace(KEYS[2], prune_expiry)",
    "refund": "replace(KEYS[1], prune_size)",
}


def _d2_b36(value):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(10):
        value, digit = divmod(value, 36)
        out = alphabet[digit] + out
    assert value == 0
    return out.encode("ascii")


def _d2_pair(instance, identity):
    assert 0 <= instance <= 64 and 0 <= identity <= 16
    return f"{instance:012x}" + "a" * 9 + chr(ord("a") + identity)


def _d2_payload(maximum=True):
    from latet.gateway.request_audit import _d2_encode

    from ..unit.test_request_audit import _d2_maximum_local

    local = _d2_maximum_local(True)
    if not maximum:
        local["counts"] = []
    raw = _d2_encode(local)
    assert type(raw) is bytes and len(raw) == (7604 if maximum else 44)
    return raw


class _D2Native:
    def __init__(self, bus, tmp_path, observer=None):
        from latet.server import live as live_module

        _REQUEST_STATUS = live_module._REQUEST_STATUS
        self.bus = bus
        self.client = bus.client
        self.prefix = bus.prefix + "n" * (128 - len(bus.prefix))
        assert len(self.prefix) == 128 and self.prefix.startswith(bus.prefix)
        self.base = self.prefix + "request-status:"
        self.area = tmp_path
        self.known = set()
        self.observer = observer
        self.script = _REQUEST_STATUS
        self.samples = 0

    def keys(self, pair):
        keys = (
            self.base + "size",
            self.base + "expiry",
            self.base + "identity:u_" + pair[12:],
            self.base + "row:" + pair[:12] + ":u_" + pair[12:],
        )
        self.known.update(keys)
        assert len(self.known) <= 1044  # includes two absent 1025th-candidate keys
        return keys

    def now(self):
        seconds, micros = self.client.time()
        return seconds * 1000 + micros // 1000

    def call(self, pair, payload, cut=None, prune=False):
        script = self.script
        if cut is not None:
            needle = (_D2_PRUNE_CUTS if prune else _D2_ADMIT_CUTS)[cut]
            assert script.count(needle) == 1
            # Exact production prefix/control flow through a completed REAL command.
            # Semicolon is required for injection inside one-line for loops.
            script = script.replace(needle, needle + "; error('P25_NATIVE_PREFIX')", 1)
        return self.client.eval(script, 4, *self.keys(pair), "p", pair[:12], pair[12:], payload)

    def seed(self, pairs, *, expired=False, maximum=True):
        """Explicit canonical fixture, never bulk MSET and never repeated script preflights."""
        pairs = sorted(pairs)
        assert 1 <= len(pairs) <= 1024
        assert max(Counter(pair[12:] for pair in pairs).values()) <= 64
        assert len({pair[:12] for pair in pairs}) <= 65
        assert len({pair[12:] for pair in pairs}) <= 17
        stamp = self.now() + (-1000 if expired else 25000)
        encoded = _d2_b36(stamp)
        groups = {}
        for pair in pairs:
            self.keys(pair)
            groups.setdefault(pair[12:], []).append(pair[:12])
        # Reserve first, then install indexes and one reusable row buffer.
        self.client.set(self.base + "size", b"".join(pair.encode() + encoded for pair in pairs))
        self.client.set(self.base + "expiry", b"".join(encoded + pair.encode() for pair in pairs))
        for ident, instances in groups.items():
            self.client.set(self.base + "identity:u_" + ident, b"".join(inst.encode() + encoded for inst in instances))
        if not expired:
            payload = _d2_payload(maximum)
            row = f"{stamp:013d}\n".encode() + payload
            for pair in pairs:
                self.client.set(self.keys(pair)[3], row, pxat=stamp)
            del row, payload
            assert self.now() < stamp, "fixture expired during prepopulation; no retry"
        return stamp

    def snapshot(self):
        """One raw row at a time; retain only fixed metadata, never a row list."""
        values = {}
        for key in sorted(self.known):
            kind = self.client.type(key)
            assert kind in (b"none", b"string")
            length = self.client.strlen(key)
            assert length <= 32768
            raw = self.client.get(key)
            if raw is None:
                values[key] = None
                continue
            assert type(raw) is bytes and len(raw) == length
            native = self.client.memory_usage(key, samples=0)
            assert type(native) is int and native > 0  # nil/expiry is not zero-cost evidence
            ttl = self.client.pttl(key)
            assert (0 < ttl <= 30000) if key.startswith(self.base + "row:") else ttl == -1
            values[key] = {
                "type": "string",
                "length": length,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "native": native,
                "ttl": ttl,
            }
            del raw
        return values

    @staticmethod
    def fingerprint(snapshot):
        return {key: None if value is None else (value["length"], value["sha256"]) for key, value in snapshot.items()}

    def measure(self, label, expected_n, expected_present=None):
        raw = self.client.get(self.base + "size")
        if expected_n == 0:
            assert raw is None
            pairs = []
        else:
            assert type(raw) is bytes and len(raw) == expected_n * 32
            pairs = [raw[i : i + 22].decode("ascii") for i in range(0, len(raw), 32)]
            assert pairs == sorted(set(pairs))
        del raw
        counts = Counter(pair[12:] for pair in pairs)
        observed = self.snapshot()
        present = {key for key, value in observed.items() if value is not None}
        if expected_present is not None:
            assert present == set(expected_present)
        if not pairs:
            assert not present, "zero reservations with still-owned D2 keys"
            total = largest = 0
        else:
            allowed = {key for pair in pairs for key in self.keys(pair)}
            assert present <= allowed

            def amount(key):
                return 0 if observed.get(key) is None else observed[key]["native"]

            global_cost = amount(self.base + "size") + amount(self.base + "expiry")
            largest = 0
            for pair in pairs:
                _size, _expiry, directory, row = self.keys(pair)
                # R80 over-reservation: two main globals, directory, row+expiry ref.
                owned = amount(row) + (amount(directory) + counts[pair[12:]] - 1) // counts[pair[12:]]
                owned += (global_cost + len(pairs) - 1) // len(pairs)
                occupied = 512 + (256 + counts[pair[12:]] - 1) // counts[pair[12:]]
                occupied += (512 + len(pairs) - 1) // len(pairs)
                largest = max(largest, owned + occupied)
            total = sum(value["native"] for value in observed.values() if value is not None)
            assert largest <= 65536
            assert total + 256 * (3 * len(pairs) + 2) <= 65536 * len(pairs) <= 67108864
        self.samples += 1
        report = {
            "label": label,
            "reservations": len(pairs),
            "reported_string_bytes": total,
            "max_pair_with_R80_slot_charge": largest,
            "keys": observed,
        }
        if self.observer is not None:
            self.observer.sample(label, report)
        else:
            path = self.area / f"d2-regression-{self.samples:02d}-{label}.json"
            with path.open("x", encoding="utf-8") as output:
                json.dump(report, output, sort_keys=True)
        return observed

    def close(self):
        # Exact known keys only; root's unchanged prefix fixture/guard verifies absence.
        for key in sorted(self.known):
            self.client.delete(key)


def test_d2_native_prefix128_large_small_large(bus, tmp_path, d2_observer):
    h = _D2Native(bus, tmp_path, d2_observer)
    pair = _d2_pair(0, 0)
    try:
        assert h.call(pair, _d2_payload()) == [1]
        maximum = h.measure("large1", 1, h.keys(pair))
        for maximum_payload, label in ((False, "small"), (True, "large2")):
            time.sleep(0.01)  # fixed separation; no clock override or retry
            assert h.call(pair, _d2_payload(maximum_payload)) == [1]
            seen = h.measure(label, 1, h.keys(pair))
            assert seen[h.keys(pair)[3]]["length"] == (7618 if maximum_payload else 58)
        assert maximum[h.keys(pair)[3]]["length"] == 7618
    finally:
        h.close()


@pytest.mark.parametrize("global_limit", [False, True], ids=("contributors64", "reservations1024"))
def test_d2_native_capacity_boundaries(bus, tmp_path, global_limit, d2_observer):
    h = _D2Native(bus, tmp_path, d2_observer)
    all_pairs = [_d2_pair(inst, ident) for ident in range(16 if global_limit else 1) for inst in range(64)]
    admitted = all_pairs[-1]
    candidate = _d2_pair(0, 16) if global_limit else _d2_pair(64, 0)
    try:
        h.keys(candidate)  # absent candidate in both before/after manifests
        stamp = h.seed(all_pairs[:-1])
        expected_before = {key for pair in all_pairs[:-1] for key in h.keys(pair)}
        h.measure("before-cap", len(all_pairs) - 1, expected_before)
        assert h.now() < stamp
        assert h.call(admitted, _d2_payload()) == [1]
        expected_full = {key for pair in all_pairs for key in h.keys(pair)}
        before = h.measure("at-cap", len(all_pairs), expected_full)
        assert h.now() < stamp, "expiry must not masquerade as capacity rejection"
        assert h.call(candidate, _d2_payload()) == [0]
        after = h.measure("rejected-over-cap", len(all_pairs), expected_full)
        assert h.now() < stamp, "fixture expiry during sampling is incomplete evidence"
        assert h.fingerprint(before) == h.fingerprint(after)
    finally:
        h.close()


@pytest.mark.parametrize(
    ("replacement", "cut"),
    [
        (False, "reserve"),
        (False, "expiry"),
        (False, "directory"),
        (False, "row"),
        (True, "reserve"),
        (True, "expiry"),
        (True, "directory"),
        (True, "row"),
    ],
    ids=(
        "new-reserve",
        "new-expiry",
        "new-directory",
        "new-row",
        "replace-reserve",
        "replace-expiry",
        "replace-directory",
        "replace-row",
    ),
)
def test_d2_native_publication_prefix_failure(bus, tmp_path, replacement, cut, d2_observer):
    from redis.exceptions import ResponseError

    h = _D2Native(bus, tmp_path, d2_observer)
    pair = _d2_pair(0, 0)
    keys = h.keys(pair)
    try:
        if replacement:
            h.seed([pair], maximum=False)
        with pytest.raises(ResponseError, match="P25_NATIVE_PREFIX"):
            h.call(pair, _d2_payload(), cut=cut)
        # New orphan states; replacements preserve the existing row/indices.
        present = keys if replacement else keys[: tuple(_D2_ADMIT_CUTS).index(cut) + 1]
        failed = h.measure("fault-" + cut, 1, present)
        if cut != "row":
            from latet.server.live import ValkeyRequestStatus

            transport = ValkeyRequestStatus(
                SimpleNamespace(client=bus.client, prefix=h.prefix), "u_" + pair[12:], pair[:12]
            )
            assert transport._call("d", b"")[1] == 0
        if replacement and cut != "row":
            assert failed[keys[3]]["length"] == 58
        # Root admitted prefix error is not OOM/crash/real network loss.
        time.sleep(0.01)
        assert h.call(pair, _d2_payload()) == [1]
        h.measure("retry-" + cut, 1, keys)
    finally:
        h.close()


@pytest.mark.parametrize("cut", tuple(_D2_PRUNE_CUTS))
def test_d2_native_prune_prefix_conserves_until_zero(bus, tmp_path, cut, d2_observer):
    from redis.exceptions import ResponseError

    h = _D2Native(bus, tmp_path, d2_observer)
    pair = _d2_pair(0, 0)
    keys = h.keys(pair)
    try:
        h.seed([pair], expired=True)  # canonical expired reservation; row already absent
        with pytest.raises(ResponseError, match="P25_NATIVE_PREFIX"):
            h.call(pair, _d2_payload(), cut=cut, prune=True)
        present = {"row": keys[:3], "directory": keys[:2], "expiry": keys[:1], "refund": ()}[cut]
        h.measure("prune-" + cut, 0 if cut == "refund" else 1, present)
        assert h.call(pair, _d2_payload()) == [1]
        h.measure("prune-retry-" + cut, 1, keys)
    finally:
        h.close()


@pytest.mark.parametrize("expired_count", [64, 65])
def test_d2_native_prune_batch_limit(bus, tmp_path, expired_count, d2_observer):
    h = _D2Native(bus, tmp_path, d2_observer)
    pairs = [_d2_pair(inst, 0) for inst in range(64)]
    if expired_count == 65:
        pairs.append(_d2_pair(0, 1))
    target = _d2_pair(64, 1)  # no more than65 synthetic instance IDs
    try:
        h.seed(pairs, expired=True)
        assert h.call(target, _d2_payload()) == ([1] if expired_count == 64 else [0])
        h.measure("prune-batch", 1)
        raw = h.client.get(h.base + "size")
        if expired_count == 65:
            assert raw[:22].decode() in pairs and raw[:22].decode() != target
            assert h.client.exists(h.keys(target)[3]) == 0
        else:
            assert raw[:22].decode() == target
    finally:
        h.close()


def test_d2_native_actual_31s_expiry_then_zero_refund(bus, tmp_path, d2_observer):
    """ONE real31s wait, no synthetic clock/poll/retry; qualification remains separately selected."""
    from redis.exceptions import ResponseError

    h = _D2Native(bus, tmp_path, d2_observer)
    pair = _d2_pair(0, 0)
    keys = h.keys(pair)
    try:
        assert h.call(pair, _d2_payload()) == [1]
        h.measure("before-real-expiry", 1, keys)
        before = h.now()
        time.sleep(31)
        assert h.now() - before >= 30000
        h.measure("expired-but-charged", 1, keys[:3])
        with pytest.raises(ResponseError, match="P25_NATIVE_PREFIX"):
            h.call(pair, _d2_payload(), cut="refund", prune=True)
        h.measure("real-expiry-zero-refund", 0, ())
        assert h.call(pair, _d2_payload()) == [1]
        h.measure("after-real-expiry-retry", 1, keys)
    finally:
        h.close()
