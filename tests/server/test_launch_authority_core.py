"""Protected launch bootstrap and bounded PostgreSQL/Valkey operation witnesses."""

import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from itertools import islice

import psycopg
import pytest
import redis

from latet.server import db as db_module
from latet.server.bus import Bus, redis_url
from latet.server.db import Database, migrate
from latet.server.instance import migrate_schema
from latet.server.launch_binding import Binding, BindingCodec, RemoteResult, binding_key
from latet.server.launch_records import (
    BOOTSTRAP_MIGRATION,
    LaunchUnavailable,
    Operation,
    check_launch_registry,
    open_registry,
    rewrap_launch_registry,
)
from latet.server.launches import LaunchAuthority
from latet.server.master import MasterKey, MasterKeyError, check_master_key
from latet.server.settings import ServerSettings

from . import test_launch_bootstrap
from .conftest import dump, identity_in, valkey_dump
from .support import DATABASE, URL, VALKEY

launch_bootstrap_db = test_launch_bootstrap.launch_bootstrap_db
owned_launch_namespaces = test_launch_bootstrap.owned_launch_namespaces

pytestmark = pytest.mark.server
_FIRST = b"f" * 32
_OTHER = b"o" * 32
_LATER = b"l" * 32
_BUS_KEY = b"b" * 32
_ISSUER = "u_aaaaaaaaaa"
_FOREIGN = "u_bbbbbbbbbb"
_KEY = b"latet_lk_" + base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=")
_ID = b"i" * 16
_BINDING = Binding("/workspace/p26a-private-folder-canary", "/workspace", "a" * 12, "client", "posix")


def _master() -> MasterKey:
    return MasterKey([_FIRST])


def _registry(db, master):
    with db.connection() as conn:
        row, keys, _secret = open_registry(conn, db.schema, master)
        return row, keys


def test_protected_bootstrap_repeats_and_rewrap_keeps_epoch_and_keys(launch_bootstrap_db):
    db = launch_bootstrap_db
    master = _master()
    assert migrate(db, master=master).count(BOOTSTRAP_MIGRATION) == 1
    before, keys = _registry(db, master)
    assert (before.entries, before.bindings) == (0, 0)
    assert migrate(db, master=master) == []
    assert _registry(db, master) == (before, keys)
    assert not rewrap_launch_registry(db, master)
    later = MasterKey([_FIRST, _LATER])
    assert rewrap_launch_registry(db, later)
    after, later_keys = _registry(db, later)
    assert later_keys == keys and after.epoch == before.epoch
    assert after.wrap_version == 2 and after.revision == before.revision + 1
    with pytest.raises(LaunchUnavailable):
        rewrap_launch_registry(db, master)
    assert _registry(db, later) == (after, later_keys)


def test_concurrent_protected_bootstrap_contenders_keep_one_root(launch_bootstrap_db):
    first = launch_bootstrap_db
    master = _master()
    barrier = threading.Barrier(2)

    def apply(database):
        barrier.wait(timeout=3)
        return migrate(database, master=master)

    with closing(Database(DATABASE, schema=first.schema, min_size=0, max_size=1)) as second:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(apply, database) for database in (first, second)]
            results = [future.result(timeout=15) for future in futures]
        assert sorted(len(result) for result in results)[0] == 0
        assert sum(result.count(BOOTSTRAP_MIGRATION) for result in results) == 1
        assert _registry(first, master) == _registry(second, master)


def test_wrong_master_cannot_initialize_or_replace_protected_root(launch_bootstrap_db, tmp_path, monkeypatch):
    db = launch_bootstrap_db
    master, wrong = _master(), MasterKey([_OTHER])
    migrations = db_module._migrations
    with monkeypatch.context() as historical:
        historical.setattr(
            db_module,
            "_migrations",
            lambda packages: [(name, text) for name, text in migrations(packages) if name != BOOTSTRAP_MIGRATION],
        )
        assert migrate(db)
    check_master_key(db, master)
    with pytest.raises(MasterKeyError, match="not the one this database was first started with"):
        migrate(db, master=wrong)
    with db.connection() as conn:
        assert conn.execute("SELECT to_regclass('launch_registry')").fetchone() == (None,)
    assert migrate(db, master=master) == [BOOTSTRAP_MIGRATION]
    before = _registry(db, master)
    with db.connection() as conn:
        conn.execute("DELETE FROM master_check WHERE id = 1")
        assert conn.execute("SELECT 1 FROM root_keys").fetchone() is None
    file = tmp_path / "wrong-master.key"
    file.write_bytes(base64.b64encode(_OTHER) + b"\n")
    settings = ServerSettings(url=URL, database_url=DATABASE, valkey_url=VALKEY, master_key_file=file)
    with pytest.raises(LaunchUnavailable):
        migrate_schema(settings, database=db)
    with db.connection() as conn:
        assert conn.execute("SELECT 1 FROM master_check").fetchone() is None
    assert _registry(db, master) == before


@contextmanager
def _bus(prefix):
    with closing(Bus(VALKEY, prefix=prefix, key=_BUS_KEY, connections=1)) as bus:
        yield bus


@pytest.fixture
def protected_core(launch_bootstrap_db, owned_launch_namespaces):
    db = launch_bootstrap_db
    _, prefix = owned_launch_namespaces
    master = _master()
    assert BOOTSTRAP_MIGRATION in migrate(db, master=master)
    identity_in(db, _ISSUER)
    try:
        with _bus(prefix) as bus:
            yield db, bus, LaunchAuthority(db, bus, master), BindingCodec(b"r" * 32), master
    finally:
        with redis.Redis.from_url(redis_url(VALKEY), socket_timeout=5, socket_connect_timeout=5) as client:
            keys = list(islice(client.scan_iter(match=prefix + "*", count=100), 3))
            if len(keys) > 2:
                raise RuntimeError("the owned launch prefix exceeded its admitted key count")
            if keys:
                client.delete(*keys)


def _open(authority, codec):
    locator = authority.locator(_KEY)
    return authority.reserve_open(_ISSUER, locator, _ID, _BINDING, codec.verifier(_KEY), codec)


def _sent(authority, operation):
    done = authority.submit(operation)
    assert done.wire.remote == RemoteResult.SET and done.wire.cleanup_pending is None
    assert done.settled and done.receipt is not None
    return done


def _live(authority, codec):
    opened = _open(authority, codec)
    _sent(authority, opened)
    return _sent(authority, authority.reserve_hold(_ISSUER, _ID, 0, codec))


def _decision(authority, codec, issuer=_ISSUER):
    return authority.decide(issuer, authority.locator(_KEY), codec.verifier(_KEY), codec)


def test_unsigned_protected_counter_and_entry_edits_refuse_without_repair(protected_core):
    db, _bus_owner, authority, codec, master = protected_core
    _live(authority, codec)
    assert _decision(authority, codec).state == "live"
    edits = (
        ("UPDATE launch_registry SET bindings = 0", "UPDATE launch_registry SET bindings = 1"),
        ("UPDATE launch_identity SET bindings = 0", "UPDATE launch_identity SET bindings = 1"),
        (
            "UPDATE launch_entry SET deadline_us = deadline_us + 1",
            "UPDATE launch_entry SET deadline_us = deadline_us - 1",
        ),
    )
    for mutate, restore in edits:
        with db.connection() as conn:
            conn.execute(mutate)
        try:
            assert _decision(authority, codec).state == "unavailable"
            with pytest.raises(LaunchUnavailable):
                check_launch_registry(db, master)
        finally:
            with db.connection() as conn:
                conn.execute(restore)
        assert _decision(authority, codec).state == "live"


def _mutating_wire(monkeypatch, bus, key, command, *, lose=False):
    pool = bus.client.connection_pool
    connection = pool.get_connection()
    pool.release(connection)
    original_send, original_read = connection.send_packed_command, connection.read_response
    counts = {"sent": 0, "lost": 0}
    awaiting = False
    slots = b"5" if command == b"SET" else b"2"
    marker = b"*" + slots + b"\r\n$3\r\n" + command + b"\r\n$" + str(len(key)).encode() + b"\r\n" + key + b"\r\n"

    def send(chunks, *args, **kwargs):
        nonlocal awaiting
        wire = chunks if type(chunks) is bytes else b"".join(chunks)
        if wire.startswith(marker):
            counts["sent"] += 1
            wire_canaries_absent = _KEY not in wire and _BINDING.folder.encode() not in wire
            assert wire_canaries_absent
            awaiting = lose
        return original_send(chunks, *args, **kwargs)

    def read(*args, **kwargs):
        nonlocal awaiting
        reply = original_read(*args, **kwargs)
        if awaiting:
            awaiting = False
            expected_reply = (
                (type(reply) is bytes and reply == b"OK") if command == b"SET" else (type(reply) is int and reply == 1)
            )
            assert expected_reply
            counts["lost"] += 1
            raise redis.ConnectionError("synthetic lost launch mutation reply")
        return reply

    monkeypatch.setattr(connection, "send_packed_command", send)
    monkeypatch.setattr(connection, "read_response", read)
    return counts


def test_two_authority_owners_read_current_generation_end_and_foreign_refusal(protected_core, monkeypatch):
    db, bus, first, codec, master = protected_core
    identity_in(db, _FOREIGN)
    publication = _open(first, codec)
    locator = publication.ticket.locator
    key = binding_key(bus, publication.ticket.epoch, locator)
    with monkeypatch.context() as wire:
        counts = _mutating_wire(wire, bus, key, b"SET")
        _sent(first, publication)
    assert counts == {"sent": 1, "lost": 0}
    assert _decision(first, codec).state == "refused"  # published PENDING is not authority
    initial = _sent(first, first.reserve_hold(_ISSUER, _ID, 0, codec))
    assert initial.receipt is not None
    with (
        closing(Database(DATABASE, schema=db.schema, min_size=0, max_size=1)) as other_db,
        _bus(bus.prefix) as other_bus,
    ):
        other = LaunchAuthority(other_db, other_bus, master)
        assert _decision(other, codec).state == "live"
        assert _decision(other, codec, _FOREIGN).state == "refused"
        replacement = _sent(other, other.reserve_hold(_ISSUER, _ID, 1, codec))
        assert not first.revalidate(initial.receipt)
        assert not first.terminal(_ISSUER, locator, 1)
        refreshed = _sent(other, other.reserve_refresh(replacement, codec))
        assert refreshed.receipt == replacement.receipt
        recovered = _sent(other, other.reserve_recovery(_ISSUER, locator, 2, _BINDING, codec.verifier(_KEY), codec))
        assert recovered.receipt is not None and recovered.receipt.address.generation == 3
        assert _decision(first, codec).receipt == recovered.receipt
        sql_bytes, redis_bytes = dump(db), valkey_dump(bus, bus.prefix)
        expected_records_present = key in redis_bytes and locator in sql_bytes
        storage_canaries_absent = all(
            canary not in sql_bytes and canary not in redis_bytes for canary in (_KEY, _BINDING.folder.encode())
        )
        assert expected_records_present
        assert storage_canaries_absent
        assert list(bus.client.scan_iter(match=bus.prefix + "*", count=100)) == [key]
        assert first.terminal(_ISSUER, locator, 3, exit_confirmed=True)
        assert _decision(other, codec).state == "refused"
        cleanup = first.reserve_cleanup(_ISSUER, locator)
        assert cleanup is not None and first.submit(cleanup).wire.remote == RemoteResult.DELETE
        assert first.retire(_ISSUER, locator, 3)
        assert _decision(other, codec).state == "claimed"
        with pytest.raises(LaunchUnavailable):
            other.reserve_recovery(_ISSUER, locator, 3, _BINDING, codec.verifier(_KEY), codec)


def test_lost_real_sql_commit_acknowledgment_reconciles_same_reservation(protected_core, monkeypatch):
    db, _bus_owner, authority, codec, master = protected_core
    with db.connection() as conn:
        connection = conn
    original = type(connection)._exec_command
    lost = 0

    def execute(self, command, *args, **kwargs):
        nonlocal lost
        answer = yield from original(self, command, *args, **kwargs)
        if self is connection and command == b"COMMIT" and not lost:
            lost += 1
            raise psycopg.OperationalError("synthetic lost reservation commit acknowledgment")
        return answer

    with monkeypatch.context() as fault:
        fault.setattr(type(connection), "_exec_command", execute)
        publication = _open(authority, codec)
    assert lost == 1 and publication.ticket.sequence == 1
    row, _keys = _registry(db, master)
    assert (row.entries, row.bindings) == (1, 1)
    with db.connection() as conn:
        assert conn.execute("SELECT op_id FROM launch_entry").fetchall() == [(publication.ticket.operation,)]
    _sent(authority, publication)


def test_lost_real_set_reply_keeps_one_signed_unresolved_writer(protected_core, monkeypatch):
    db, bus, authority, codec, master = protected_core
    publication = _open(authority, codec)
    ticket = publication.ticket
    key = binding_key(bus, ticket.epoch, ticket.locator)
    with monkeypatch.context() as fault:
        counts = _mutating_wire(fault, bus, key, b"SET", lose=True)
        completion = authority.submit(publication)
    assert counts == {"sent": 1, "lost": 1}
    assert completion.wire.remote == RemoteResult.UNKNOWN and not completion.settled
    with _bus(bus.prefix) as observer:
        binding_present = type(observer.client.get(key)) is bytes
        assert binding_present
    assert _decision(authority, codec).state == "refused"
    assert authority.terminal(_ISSUER, ticket.locator, 0, exit_confirmed=True)
    assert authority.reserve_cleanup(_ISSUER, ticket.locator) is None
    assert not authority.retire(_ISSUER, ticket.locator, 0)
    row, _keys = _registry(db, master)
    assert (row.entries, row.bindings) == (1, 1)
    with db.connection() as conn:
        assert conn.execute("SELECT op_id, op_kind FROM launch_entry").fetchall() == [
            (ticket.operation, Operation.OPEN)
        ]
    with pytest.raises(LaunchUnavailable):
        authority.submit(publication)


def test_lost_real_delete_reply_does_not_refund_a_binding(protected_core, monkeypatch):
    db, bus, authority, codec, master = protected_core
    opened = _open(authority, codec)
    _sent(authority, opened)
    locator = opened.ticket.locator
    key = binding_key(bus, opened.ticket.epoch, locator)
    assert authority.terminal(_ISSUER, locator, 0, exit_confirmed=True)
    cleanup = authority.reserve_cleanup(_ISSUER, locator)
    assert cleanup is not None
    with monkeypatch.context() as fault:
        counts = _mutating_wire(fault, bus, key, b"DEL", lose=True)
        completion = authority.submit(cleanup)
    assert counts == {"sent": 1, "lost": 1}
    assert completion.wire.remote == RemoteResult.UNKNOWN and not completion.settled
    with _bus(bus.prefix) as observer:
        binding_absent = observer.client.get(key) is None
        assert binding_absent
    assert authority.reserve_cleanup(_ISSUER, locator) is None and not authority.retire(_ISSUER, locator, 0)
    row, _keys = _registry(db, master)
    assert (row.entries, row.bindings) == (1, 1)
    with db.connection() as conn:
        assert conn.execute("SELECT op_id, op_kind FROM launch_entry").fetchall() == [
            (cleanup.ticket.operation, Operation.DELETE)
        ]
