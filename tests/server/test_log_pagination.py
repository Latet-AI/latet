"""Task 23C HTTP contract witnesses; runtime evidence waits for the coordinator's isolated slot.

Use only the existing administration route and database fixtures. No future paging helper is imported.
The insertion ceiling is not a retained snapshot and these pages do not verify the hash chain.
"""

import base64
import hashlib
import json
import math
import threading
from contextlib import closing, contextmanager

import pytest
from psycopg import Connection, Cursor, OperationalError, ServerCursor
from psycopg.types.json import JsonbLoader
from psycopg.types.string import TextLoader
from starlette.testclient import TestClient

from latet.server.app import create_server_app

from .support import URL

pytestmark = pytest.mark.server

PATH = "/_latet/server/v1/admin/log"
PAGE_BYTES = 16 * 1024 * 1024
ACTION = "pagination.witness"
ACTOR = "server"


@pytest.fixture
def reader(server):
    token = server.admin.issue("pagination witness")
    with closing(TestClient(create_server_app(server))) as client:
        # No lifespan/background listeners; only this HTTPS administration request is under test.
        yield client, {"x-latet-admin": token}


def _get(reader, **params):
    client, headers = reader
    return client.get(URL + PATH, params={"action": ACTION, "actor": ACTOR, **params}, headers=headers)


def _record(server, monkeypatch, times, *, action=ACTION, actor=ACTOR):
    ids = []
    for stamp in times:
        monkeypatch.setattr(server.audit, "_clock", lambda stamp=stamp: stamp)
        server.audit.record(actor, action)
        ids.append(server.audit.query(action=action, actor=actor, limit=1)[0].id)
    return ids


def _page(response):
    assert response.status_code == 200, response.status_code
    assert len(response.content) <= PAGE_BYTES
    found = response.json()
    assert isinstance(found["entries"], list)
    # This assertion is a capability gate, never evidence that a security mutation was caught.
    assert "next_cursor" in found, "pagination metadata is not implemented"
    assert found["next_cursor"] is None or isinstance(found["next_cursor"], str)
    return found


def _token(raw):
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _payload(token):
    assert token.isascii() and len(token) <= 1024
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(decoded) <= 768
    return json.loads(decoded)


def test_time_order_is_independent_of_insertion_order(reader, server, monkeypatch):
    ids = _record(server, monkeypatch, [100.0, 300.0, 200.0, 300.0])
    response = _get(reader, limit=10)
    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["entries"]] == [ids[3], ids[1], ids[2], ids[0]]


def test_equal_time_seek_keeps_every_id_and_allows_a_smaller_next_limit(reader, server, monkeypatch):
    ids = _record(server, monkeypatch, [100.0] * 5)
    first = _page(_get(reader, limit=2))
    second = _page(_get(reader, cursor=first["next_cursor"], limit=1))
    third = _page(_get(reader, cursor=second["next_cursor"], limit=2))
    assert [[e["id"] for e in page["entries"]] for page in (first, second, third)] == [
        ids[::-1][:2],
        ids[::-1][2:3],
        ids[::-1][3:],
    ]
    assert first["next_cursor"] and second["next_cursor"] and third["next_cursor"] is None


def test_ceiling_excludes_new_backdated_entries_and_survives_another_instance(instances, monkeypatch):
    here, there = instances
    ids = _record(here, monkeypatch, [100.0, 200.0, 300.0])
    headers = {"x-latet-admin": here.admin.issue("pagination witness")}
    with (
        closing(TestClient(create_server_app(here))) as first_client,
        closing(TestClient(create_server_app(there))) as other,
    ):
        params = {"action": ACTION, "actor": ACTOR, "limit": 1}
        first = _page(first_client.get(URL + PATH, params=params, headers=headers))
        cursor = first["next_cursor"]
        ceiling = _payload(cursor)["ceiling_id"]
        _record(there, monkeypatch, [50.0, 250.0, 400.0])
        rest = _page(other.get(URL + PATH, params={**params, "limit": 10, "cursor": cursor}, headers=headers))
        assert [e["id"] for e in first["entries"] + rest["entries"]] == ids[::-1]
        assert all(e["id"] <= ceiling for e in rest["entries"])
        assert rest["next_cursor"] is None


@pytest.mark.parametrize("stamp", [math.nextafter(100.0, math.inf), 1e300, -1e300])
def test_cursor_keeps_exact_finite_database_time_without_datetime_conversion(reader, server, monkeypatch, stamp):
    _record(server, monkeypatch, [stamp] * 2)
    first = _page(_get(reader, limit=1))
    assert _payload(first["next_cursor"])["ts"] == stamp.hex()
    rest = _page(_get(reader, cursor=first["next_cursor"], limit=1))
    assert rest["entries"][0]["ts"] == stamp and rest["next_cursor"] is None


@pytest.mark.parametrize("action", ["key_a.*", "key%a.*", "key" + chr(0x5C) + "a.*"])
def test_original_literal_filters_and_half_open_window_survive_continuation(reader, server, monkeypatch, action):
    wanted = action[:-1] + "changed"
    ids = _record(server, monkeypatch, [100.0, 200.0, 300.0], action=wanted)
    _record(server, monkeypatch, [150.0], action="keyXa.changed")
    _record(server, monkeypatch, [150.0], action=wanted, actor="another")
    filters = {"action": action, "actor": ACTOR, "since": "100", "until": "300"}
    first = _page(_get(reader, **filters, limit=1))
    rest = _page(_get(reader, **filters, limit=10, cursor=first["next_cursor"]))
    assert [e["id"] for e in first["entries"] + rest["entries"]] == ids[1::-1]
    assert rest["next_cursor"] is None


@pytest.mark.parametrize(
    ("field", "value"), [("since", "99"), ("until", "400"), ("actor", "another"), ("action", "pagination.*")]
)
def test_cursor_is_bound_to_original_filters(reader, server, monkeypatch, field, value):
    _record(server, monkeypatch, [100.0] * 2)
    first = _page(_get(reader, limit=1, since="100", until="300"))
    response = _get(reader, **{"since": "100", "until": "300", field: value}, cursor=first["next_cursor"])
    assert response.status_code == 400


@pytest.mark.parametrize("field", ["since", "until", "action", "actor", "limit", "cursor"])
def test_duplicate_query_fields_are_refused(reader, field):
    client, headers = reader
    response = client.get(URL + PATH, params=[(field, "1"), (field, "2")], headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("field", "value"),
    [("since", "0" * 65), ("until", "0" * 65), ("since", "NaN"), ("until", "1e999"), ("limit", "0" * 1000 + "1")],
)
def test_numeric_query_input_is_bounded_before_conversion(reader, field, value):
    assert _get(reader, **{field: value}).status_code == 400


@pytest.mark.parametrize(
    "token",
    [
        "!private_cursor_97!",
        "A" * 1025,
        "e30=",
        "W10",
        "bnVsbA",
        _token("[" * 200 + "0" + "]" * 200),
        _token('{"v":1,"v":1}'),
        _token(" " * 769),
    ],
)
def test_malformed_cursor_is_a_fixed_private_refusal(reader, caplog, token):
    response = _get(reader, cursor=token)
    assert response.status_code == 400
    assert token not in response.text and token not in caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("v", True),
        ("v", 2),
        ("ts", "nan"),
        ("ts", "inf"),
        ("ts", "1e999"),
        ("ts", True),
        ("id", True),
        ("id", 0),
        ("id", 2**63),
        ("ceiling_id", 0),
        ("ceiling_id", 2**63),
        ("extra", "planted_cursor_value_97"),
    ],
)
def test_cursor_field_mutation_is_refused_after_capability_gate(reader, server, monkeypatch, field, value):
    _record(server, monkeypatch, [100.0] * 2)
    payload = _payload(_page(_get(reader, limit=1))["next_cursor"])
    payload[field] = value
    token = _token(json.dumps(payload, separators=(",", ":")))
    response = _get(reader, cursor=token)
    assert response.status_code == 400 and "planted_cursor_value_97" not in response.text


@pytest.mark.parametrize("mutation", ["duplicate", "padding", "boundary_above_ceiling", "numeric_id"])
def test_otherwise_valid_cursor_has_strict_encoding_and_integer_boundary(reader, server, monkeypatch, mutation):
    _record(server, monkeypatch, [100.0] * 2)
    token = _page(_get(reader, limit=1))["next_cursor"]
    payload = _payload(token)
    raw = json.dumps(payload, separators=(",", ":"))
    if mutation == "duplicate":
        token = _token(raw[:-1] + ',"id":' + str(payload["id"]) + "}")
    elif mutation == "padding":
        token += "="
    else:
        payload["id"] = payload["ceiling_id"] + 1 if mutation == "boundary_above_ceiling" else float(payload["id"])
        token = _token(json.dumps(payload, separators=(",", ":")))
    assert _get(reader, cursor=token).status_code == 400


def _large_rows(server, sizes):
    # Build stored legacy data in PostgreSQL, without shipping its large text through the writer or Python first.
    with server.db.connection() as conn:
        ids = []
        for n, size in enumerate(sizes):
            ids.append(
                conn.execute(
                    "INSERT INTO audit (ts, actor, action, details, prev, hash)"
                    " VALUES (%s, %s, %s, jsonb_build_object('padding', repeat('x', %s)), '', '') RETURNING id",
                    (100.0 + n, ACTOR, ACTION, size),
                ).fetchone()[0]
            )
    return ids


def test_byte_limited_short_page_resumes_at_the_unemitted_row(reader, server):
    ids = _large_rows(server, [9 * 1024 * 1024] * 2)
    first = _page(_get(reader, limit=10))
    assert [e["id"] for e in first["entries"]] == [ids[1]]
    assert _payload(first["next_cursor"])["id"] == ids[1]
    rest = _page(_get(reader, limit=10, cursor=first["next_cursor"]))
    assert [e["id"] for e in rest["entries"]] == [ids[0]] and rest["next_cursor"] is None


@pytest.mark.parametrize("after_prefix", [False, True])
def test_oversized_row_is_never_skipped_or_returned_as_success(reader, server, after_prefix):
    ids = _large_rows(server, [PAGE_BYTES + 1, *([1] if after_prefix else [])])
    params = {}
    if after_prefix:
        prefix = _page(_get(reader, limit=10))
        assert [e["id"] for e in prefix["entries"]] == [ids[1]]
        assert _payload(prefix["next_cursor"])["id"] == ids[1]
        params["cursor"] = prefix["next_cursor"]
    for _ in range(2):
        response = _get(reader, limit=10, **params)
        assert response.status_code == 422
        assert response.json()["reason"] == "log_entry_too_large"
        assert "next_cursor" not in response.json() and len(response.content) < 1024
        assert "padding" not in response.text
    with server.db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM audit WHERE action = %s", (ACTION,)).fetchone()[0] == len(ids)


def test_oversized_source_is_refused_before_python_decodes_its_fields(reader, server, monkeypatch):
    _large_rows(server, [PAGE_BYTES + 1])
    decoded_sizes = []

    class GuardedText(TextLoader):
        def load(self, data):
            decoded_sizes.append(len(data))
            assert len(data) <= PAGE_BYTES, "unbounded text reached Python decoding"
            return super().load(data)

    class GuardedJsonb(JsonbLoader):
        def load(self, data):
            decoded_sizes.append(len(data))
            assert len(data) <= PAGE_BYTES, "unbounded JSONB reached Python decoding"
            return super().load(data)

    def guarded(open_connection):
        @contextmanager
        def opened(*args, **kwargs):
            with open_connection(*args, **kwargs) as conn:
                conn.adapters.register_loader("text", GuardedText)
                conn.adapters.register_loader("jsonb", GuardedJsonb)
                yield conn

        return opened

    monkeypatch.setattr(server.db, "connection", guarded(server.db.connection))
    monkeypatch.setattr(server.db, "transaction", guarded(server.db.transaction))
    response = _get(reader)
    assert response.status_code == 422 and response.json()["reason"] == "log_entry_too_large"
    assert all(size <= PAGE_BYTES for size in decoded_sizes)


def test_existing_query_and_chain_contract_stays_in_id_order(reader, server, monkeypatch):
    ids = _record(server, monkeypatch, [300.0, 100.0, 200.0])
    assert [e.id for e in server.audit.query(action=ACTION)] == ids[::-1]
    assert server.audit.verify().ok
    assert [e["id"] for e in _page(_get(reader))["entries"]] == [ids[0], ids[2], ids[1]]
    assert server.audit.verify().ok


def test_cursor_has_the_exact_shared_schema_and_canonical_filter_digest(reader, server, monkeypatch):
    ids = _record(server, monkeypatch, [100.0, 200.0])
    first = _page(_get(reader, limit=1, since="0", until="300"))
    payload = _payload(first["next_cursor"])
    normalized = {"action": ACTION, "actor": ACTOR, "since": (0.0).hex(), "until": (300.0).hex()}
    expected = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload == {"v": 1, "ts": (200.0).hex(), "id": ids[1], "ceiling_id": ids[1], "filters": expected}
    assert _token(json.dumps(payload, sort_keys=True, separators=(",", ":"))) == first["next_cursor"]


@pytest.mark.parametrize("field", ["action", "actor"])
def test_empty_and_absent_text_filters_have_the_existing_same_selection(reader, server, monkeypatch, field):
    _record(server, monkeypatch, [100.0, 200.0])
    first = _page(_get(reader, **{field: ""}, limit=1))
    payload = _payload(first["next_cursor"])
    filters = {"action": ACTION, "actor": ACTOR, "since": None, "until": None, field: None}
    assert (
        payload["filters"]
        == hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    assert _get(reader, **{field: None}, cursor=first["next_cursor"]).status_code == 200


@pytest.mark.parametrize("mutation", ["missing", "digest", "negative_infinity", "integer_digits", "lower_bound"])
def test_otherwise_valid_cursor_rejects_missing_nonfinite_and_noncanonical_fields(
    reader, server, monkeypatch, mutation
):
    _record(server, monkeypatch, [100.0, 200.0])
    payload = _payload(_page(_get(reader, limit=1))["next_cursor"])
    if mutation == "missing":
        del payload["filters"]
    elif mutation == "digest":
        payload["filters"] = "0" * 64
    elif mutation == "negative_infinity":
        payload["ts"] = "-inf"
    elif mutation == "lower_bound":
        payload["id"] = -1
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if mutation == "integer_digits":
        raw = raw.replace('"id":' + str(payload["id"]), '"id":' + "9" * 20)
    assert _get(reader, cursor=_token(raw)).status_code == 400


@pytest.mark.parametrize("column", ["target", "address", "prev", "hash"])
def test_source_bound_covers_combined_utf8_text_columns_before_decoding(reader, server, monkeypatch, column):
    _large_rows(server, [9 * 1024 * 1024])
    with server.db.connection() as conn:
        # The column comes from this fixed parameter list, never an input to production SQL.
        conn.execute(f"UPDATE audit SET {column} = repeat(chr(233), %s) WHERE action = %s", (4 * 1024 * 1024, ACTION))
    sizes = []

    class BoundedText(TextLoader):
        def load(self, data):
            sizes.append(len(data))
            return super().load(data)

    original = ServerCursor.execute

    def intercepted(cursor, *args, **kwargs):
        cursor.adapters.register_loader("text", BoundedText)
        return original(cursor, *args, **kwargs)

    monkeypatch.setattr(ServerCursor, "execute", intercepted)
    response = _get(reader)
    assert response.status_code == 422 and response.json()["reason"] == "log_entry_too_large"
    assert not any(size > 1024 for size in sizes), (
        "a refused row transferred its fields instead of only bounded metadata"
    )


def test_http_page_uses_a_real_server_cursor_and_existence_only_lookahead(reader, server, monkeypatch):
    _large_rows(server, [PAGE_BYTES + 1, 1])
    named_fetches = []
    original = ServerCursor.fetchone

    def observed(cursor):
        named_fetches.append(cursor.name)
        return original(cursor)

    def buffered(*args, **kwargs):
        pytest.fail("a log page buffered a result instead of fetching one bounded candidate")

    monkeypatch.setattr(ServerCursor, "fetchone", observed)
    monkeypatch.setattr(Cursor, "fetchall", buffered)
    monkeypatch.setattr(ServerCursor, "fetchall", buffered)
    first = _page(_get(reader, limit=1))
    assert len(first["entries"]) == 1 and first["next_cursor"]
    assert named_fetches and len(named_fetches) == 1
    # A row-limit lookahead must observe existence without transferring the giant next row.
    assert _get(reader, cursor=first["next_cursor"]).status_code == 422


@pytest.mark.parametrize("over", [0, 1])
@pytest.mark.parametrize("escaped", [False, True])
def test_complete_body_including_cursor_has_an_exact_byte_boundary(reader, server, over, escaped):
    ids = _large_rows(server, [1, 0])
    if escaped:
        with server.db.connection() as conn:
            conn.execute("UPDATE audit SET target = repeat(chr(1), %s) WHERE id = %s", (2 * 1024 * 1024, ids[1]))
    initial = _page(_get(reader, limit=1))
    assert initial["next_cursor"]
    overhead = len(json.dumps(initial, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
    with server.db.connection() as conn:
        conn.execute(
            "UPDATE audit SET details = jsonb_build_object('padding', repeat('x', %s)) WHERE id = %s",
            (PAGE_BYTES - overhead + over, ids[1]),
        )
    response = _get(reader, limit=1)
    if over:
        assert response.status_code == 422 and response.json()["reason"] == "log_entry_too_large"
    else:
        page = _page(response)
        assert len(response.content) == PAGE_BYTES and page["next_cursor"]
        assert _payload(page["next_cursor"])["id"] == ids[1]


@pytest.mark.parametrize("stamp", [float("nan"), float("inf"), -float("inf")])
def test_stored_nonfinite_boundary_is_a_controlled_failure(reader, server, stamp):
    _large_rows(server, [1])
    with server.db.connection() as conn:
        conn.execute("UPDATE audit SET ts = %s WHERE action = %s", (stamp, ACTION))
    response = _get(reader)
    assert response.status_code == 422 and response.json()["reason"] == "log_entry_invalid"
    assert len(response.content) < 1024


@pytest.mark.parametrize(
    "details",
    ["[]", '{"deep":' + "[" * 1500 + "0" + "]" * 1500 + "}", '{"number":' + "9" * 5000 + "}"],
    ids=["array", "deep", "integer"],
)
def test_unreadable_stored_details_never_become_an_empty_record(reader, server, details):
    _large_rows(server, [1])
    with server.db.connection() as conn:
        conn.execute("UPDATE audit SET details = %s::jsonb WHERE action = %s", (details, ACTION))
    if '"deep"' in details:
        # The real decoder accepted this value in the initial run. Exercise the fixed serializer depth refusal.
        decoded = json.loads(details)
        nested = decoded["deep"]
        for _ in range(65):
            assert isinstance(nested, list)
            nested = nested[0]
    response = _get(reader)
    assert response.status_code == 422 and response.json()["reason"] == "log_entry_invalid"
    assert "entries" not in response.json() and len(response.content) < 1024


def test_page_deadline_is_cumulative_and_work_stays_in_the_administration_pool(reader, server, monkeypatch):
    from latet.server import audit as audit_module

    _large_rows(server, [1, 1, 1])
    elapsed = [0.0]
    threads = []
    original = ServerCursor.fetchone

    def slow_steps(cursor):
        threads.append(threading.current_thread().name)
        result = original(cursor)
        elapsed[0] += 31.0
        return result

    monkeypatch.setattr(audit_module, "_page_clock", lambda: elapsed[0], raising=False)
    monkeypatch.setattr(ServerCursor, "fetchone", slow_steps)
    response = _get(reader)
    assert response.status_code == 503 and response.headers["retry-after"]
    assert threads and all(name.startswith("latet-admin") for name in threads)
    assert len(threads) == 2, "the deadline was reset for another row instead of stopping cumulative page work"


def test_page_connection_deadline_reaches_waits_and_is_restored_after_failure(server, monkeypatch):
    from latet.server import db as db_module

    def clock():
        return 10.0

    seen = []
    original = db_module._Connection.wait

    def observed(conn, gen, interval=0.1, timeout=None):
        seen.append((phase[0], conn._deadline))
        return original(conn, gen, interval, timeout)

    monkeypatch.setattr(db_module._Connection, "wait", observed)
    phase = ["body"]

    def failed():
        with server.db.connection(deadline=20.0, clock=clock) as conn:
            conn.execute("SELECT 1")
            assert conn._deadline == 20.0
            assert seen and all(value == 20.0 for _, value in seen)
            phase[0] = "rollback"
            raise RuntimeError("owned failure")

    with pytest.raises(RuntimeError, match="owned failure"):
        failed()
    assert ("rollback", 20.0) in seen
    assert all(value == 20.0 for _, value in seen)
    with server.db.connection() as conn:
        assert conn._deadline is None
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_pool_wait_and_commit_share_the_absolute_page_deadline(server, monkeypatch):
    from latet.server import db as db_module

    bounds, commits = [], []
    original_pool = server.db.pool.getconn
    original_wait = db_module._Connection.wait
    phase = ["body"]

    def observed_pool(*args, **kwargs):
        bounds.append(kwargs.get("timeout"))
        return original_pool(*args, **kwargs)

    def observed_wait(conn, gen, interval=0.1, timeout=None):
        if phase[0] == "commit":
            commits.append(conn._deadline)
        return original_wait(conn, gen, interval, timeout)

    monkeypatch.setattr(server.db.pool, "getconn", observed_pool)
    monkeypatch.setattr(db_module._Connection, "wait", observed_wait)
    with server.db.connection(deadline=13.0, clock=lambda: 10.0) as conn:
        conn.execute("SELECT 1")
        phase[0] = "commit"
    assert bounds == [3.0]
    assert commits and all(value == 13.0 for value in commits)
    assert conn._deadline is None


def test_expired_page_never_starts_a_pool_wait(server, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("an expired page asked the pool for a connection")

    monkeypatch.setattr(server.db.pool, "getconn", forbidden)

    def expired():
        with server.db.connection(deadline=9.0, clock=lambda: 10.0):
            pytest.fail("an expired page acquired a connection")

    with pytest.raises(TimeoutError):
        expired()


def test_a_page_wait_timeout_discards_the_uncertain_connection(server, monkeypatch):
    from latet.server import db as db_module

    original = Connection.wait
    with server.db.connection(deadline=20.0, clock=lambda: 10.0) as conn:

        def interrupted(connection, gen, interval=0.1, timeout=None):
            if connection is conn:
                gen.close()
                raise db_module._WAITED_TOO_LONG
            return original(connection, gen, interval, timeout)

        monkeypatch.setattr(Connection, "wait", interrupted)

        def pending():
            yield 0

        with pytest.raises(OperationalError):
            conn.wait(pending(), timeout=0.5)
        assert conn.closed, "a timeout must not return an uncertain transaction to the pool"
    with server.db.connection() as fresh:
        assert not fresh.closed and fresh.execute("SELECT 1").fetchone()[0] == 1


def test_page_wait_uses_the_remaining_absolute_budget(server, monkeypatch):
    original = Connection.wait
    bounds = []
    with server.db.connection(deadline=10.25, clock=lambda: 10.0) as conn:

        def observed(connection, gen, interval=0.1, timeout=None):
            if connection is conn:
                bounds.append(timeout)
                gen.close()
                return None
            return original(connection, gen, interval, timeout)

        def pending():
            yield 0

        monkeypatch.setattr(Connection, "wait", observed)
        conn.wait(pending(), timeout=5.0)
        assert bounds == [0.25]


@pytest.mark.parametrize("rollback", [False, True])
def test_page_budget_is_reset_after_transaction_exit_and_before_pool_return(server, monkeypatch, rollback):
    from latet.server import db as db_module

    returned, finishing = [], []
    original_put = server.db.pool.putconn
    original_wait = db_module._Connection.wait
    default_clock = db_module._Connection._deadline_clock
    phase = ["body"]

    def put(conn):
        returned.append((conn._deadline, conn._deadline_clock))
        # Observe real pool ownership without allowing a failing assertion to strand its checkout.
        return original_put(conn)

    def wait(conn, gen, interval=0.1, timeout=None):
        if phase[0] == "exit":
            finishing.append(conn._deadline)
        return original_wait(conn, gen, interval, timeout)

    monkeypatch.setattr(server.db.pool, "putconn", put)
    monkeypatch.setattr(db_module._Connection, "wait", wait)

    def transaction():
        with server.db.connection(deadline=20.0, clock=lambda: 10.0) as conn:
            conn.execute("SELECT 1")
            phase[0] = "exit"
            if rollback:
                raise RuntimeError("owned rollback")

    if rollback:
        with pytest.raises(RuntimeError, match="owned rollback"):
            transaction()
    else:
        transaction()
    assert finishing and all(value == 20.0 for value in finishing)
    assert returned == [(None, default_clock)], "a later borrower could receive the previous page's deadline"


def test_composite_seek_has_an_available_index_path(server):
    with server.db.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) SELECT id, ts FROM audit WHERE (ts, id) < (%s, %s) ORDER BY ts DESC, id DESC LIMIT 10",
            (100.0, 99),
        ).fetchone()[0][0]["Plan"]
    # This demonstrates an ordered index path is available; it does not claim every production filter selects it.
    assert plan["Node Type"] == "Limit"
    assert "Index" in plan["Plans"][0]["Node Type"]
    assert plan["Plans"][0]["Index Name"] == "audit_ts_id"
