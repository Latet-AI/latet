"""Signed records and the migration-owned launch registry root."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from psycopg import Error, sql

from latet.gateway.launch_keys import BINDINGS, ENTRIES, IDENTITY_LIVE, MAX_GENERATION
from latet.server.sealing import DERIVE, derived, version_of
from latet.server.start_errors import DatabaseError

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.db import Database
    from latet.server.master import MasterKey

BOOTSTRAP_MIGRATION = "latet.server.migrations:014_launch_authority"
MASTER_REQUIRED = "launch migration requires an explicit master key"
FORMAT = 1
MAX_TIME = (1 << 63) - 1
LIFETIME_US = 60_000_000
_PURPOSE = b"launch-registry-v1"
_MAGIC = b"latet-launch-registry-v1\0"
ISSUER = re.compile(r"u_[a-z2-7]{10}")


class LaunchUnavailable(DatabaseError):
    def __init__(self) -> None:
        super().__init__("protected launch authority is unavailable")


class State(IntEnum):
    PENDING = 1
    LIVE = 2
    TERMINAL = 3


class Reason(IntEnum):
    NONE = 0
    ENDED = 1
    EXPIRED = 2
    FAILED = 3


class Operation(IntEnum):
    NONE = 0
    OPEN = 1
    HOLD = 2
    REFRESH = 3
    RECOVER = 4
    DELETE = 5


@dataclass(frozen=True, slots=True, repr=False)
class Keys:
    epoch: bytes
    locator: bytes
    auth: bytes

    @classmethod
    def derive(cls, epoch: bytes, secret: bytes) -> Keys:
        return cls(epoch, derived(secret, DERIVE, b"launch-locator-v1"), derived(secret, DERIVE, b"launch-row-auth-v1"))


@dataclass(frozen=True, slots=True, repr=False)
class Registry:
    singleton: int
    format: int
    epoch: bytes
    sealed_secret: bytes
    wrap_version: int
    revision: int
    entries: int
    bindings: int


@dataclass(frozen=True, slots=True, repr=False)
class Issuer:
    issuer: str
    epoch: bytes
    revision: int
    entries: int
    bindings: int


@dataclass(frozen=True, slots=True, repr=False)
class Entry:
    locator: bytes
    launch_id: bytes
    issuer: str
    epoch: bytes
    revision: int
    generation: int
    op_seq: int
    state: int
    deadline_us: int
    binding_reserved: bool
    binding_digest: bytes
    published_token: bytes | None
    published_blob_digest: bytes | None
    terminal_reason: int
    disclosure_possible: bool
    exit_confirmed: bool
    op_kind: int
    op_id: bytes | None
    op_owner: bytes | None
    op_base_gen: int | None
    op_target_gen: int | None
    op_payload_digest: bytes | None


Record = Registry | Issuer | Entry
_TABLES = {
    Registry: ("launch_registry", "launch-global-v1"),
    Issuer: ("launch_identity", "launch-identity-v1"),
    Entry: ("launch_entry", "launch-entry-v1"),
}


def _require(condition: bool) -> None:
    if not condition:
        raise LaunchUnavailable()


def exact_bytes(value: object, width: int) -> bool:
    return type(value) is bytes and len(value) == width


def uint(value: object, upper: int = MAX_GENERATION) -> bool:
    return type(value) is int and 0 <= value <= upper


def advance(value: int) -> int:
    _require(uint(value, MAX_GENERATION - 1))
    return value + 1


def deadline(now: int) -> int:
    _require(uint(now, MAX_TIME - LIFETIME_US))
    return now + LIFETIME_US


def _width(value: object, width: int) -> bool:
    return value is None or exact_bytes(value, width)


def _valid(row: Record) -> None:
    _require(exact_bytes(row.epoch, 16) and uint(row.revision))
    if isinstance(row, (Registry, Issuer)):
        cap = BINDINGS if isinstance(row, Registry) else IDENTITY_LIVE
        _require(uint(row.entries, ENTRIES) and uint(row.bindings, cap) and row.bindings <= row.entries)
        if isinstance(row, Registry):
            _require(type(row.singleton) is int and row.singleton == 1 and type(row.format) is int and row.format == 1)
            _require(type(row.sealed_secret) is bytes and 30 <= len(row.sealed_secret) <= 256)
            _require(
                uint(row.wrap_version, 255)
                and row.wrap_version > 0
                and version_of(row.sealed_secret) == row.wrap_version
            )
        else:
            _require(type(row.issuer) is str and ISSUER.fullmatch(row.issuer) is not None and row.entries > 0)
        return
    _require(type(row.issuer) is str and ISSUER.fullmatch(row.issuer) is not None)
    _require(exact_bytes(row.locator, 32) and exact_bytes(row.launch_id, 16) and exact_bytes(row.binding_digest, 32))
    _require(uint(row.generation) and uint(row.op_seq) and uint(row.deadline_us, MAX_TIME))
    _require(type(row.state) in (int, State) and row.state in (State.PENDING, State.LIVE, State.TERMINAL))
    _require(type(row.terminal_reason) in (int, Reason) and row.terminal_reason in tuple(Reason))
    _require(type(row.op_kind) in (int, Operation) and row.op_kind in tuple(Operation))
    _require(all(type(flag) is bool for flag in (row.binding_reserved, row.disclosure_possible, row.exit_confirmed)))
    _require(_width(row.published_token, 16) and _width(row.published_blob_digest, 32))
    _require((row.published_token is None) == (row.published_blob_digest is None))
    _require((row.state == State.TERMINAL) == (row.terminal_reason != Reason.NONE))
    _require(not row.exit_confirmed or row.state == State.TERMINAL)
    _require(row.state != State.LIVE or (row.published_token is not None and row.disclosure_possible))
    _require(
        row.binding_reserved
        or (row.state == State.TERMINAL and row.op_kind == Operation.NONE and row.published_token is None)
    )
    if row.op_kind == Operation.NONE:
        operation = (row.op_id, row.op_owner, row.op_base_gen, row.op_target_gen, row.op_payload_digest)
        _require(all(value is None for value in operation))
    else:
        _require(exact_bytes(row.op_id, 16) and exact_bytes(row.op_owner, 16) and row.op_seq > 0)
        _require(uint(row.op_base_gen) and uint(row.op_target_gen) and row.op_base_gen == row.generation)
        if row.op_kind in (Operation.HOLD, Operation.RECOVER):
            _require(row.op_target_gen == row.generation + 1)
        else:
            _require(row.op_target_gen == row.generation)
        _require(
            row.op_payload_digest is None if row.op_kind == Operation.DELETE else exact_bytes(row.op_payload_digest, 32)
        )
        _require(row.op_kind != Operation.DELETE or row.state == State.TERMINAL)


def packed(parts: tuple[bytes, ...]) -> bytes:
    return b"".join(len(part).to_bytes(4, "big") + part for part in parts)


def _field(value: Any) -> bytes:
    if value is None:
        return b"-"
    if type(value) is bytes:
        return value.hex().encode("ascii")
    if type(value) is bool:
        return b"1" if value else b"0"
    if isinstance(value, int):
        return str(int(value)).encode("ascii")
    if type(value) is str:
        return value.encode("ascii")
    raise LaunchUnavailable()


def _values(row: Record) -> tuple[Any, ...]:
    values = (getattr(row, field.name) for field in fields(row))
    return tuple(int(value) if isinstance(value, IntEnum) else value for value in values)


def _tag(row: Record, keys: Keys, schema: str) -> bytes:
    _valid(row)
    _require(row.epoch == keys.epoch)
    values = tuple(
        hashlib.sha256(row.sealed_secret).digest()
        if isinstance(row, Registry) and field.name == "sealed_secret"
        else getattr(row, field.name)
        for field in fields(row)
        if field.name not in ("epoch", "format")
    )
    label = _TABLES[type(row)][1]
    return hmac.digest(keys.auth, packed(tuple(map(_field, (label, schema, FORMAT, row.epoch, *values)))), "sha256")


def names(kind: type[Record]) -> tuple[str, ...]:
    return (*(field.name for field in fields(kind)), "tag")


def columns(kind: type[Record], alias: str | None = None) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(alias, name) if alias else sql.Identifier(name) for name in names(kind))


def _decoded[R: (Registry, Issuer, Entry)](kind: type[R], raw: tuple[Any, ...]) -> R:
    _require(len(raw) == len(names(kind)))
    values = []
    for value in raw[:-1]:
        if type(value) is Decimal:
            _require(value.is_finite() and value.as_tuple().exponent == 0 and 0 <= value <= MAX_GENERATION)
            value = int(value)
        values.append(value)
    row = kind(*values)
    _valid(row)
    return row


def verified[R: (Registry, Issuer, Entry)](kind: type[R], raw: tuple[Any, ...], keys: Keys, schema: str) -> R:
    row = _decoded(kind, raw)
    _require(exact_bytes(raw[-1], 32) and hmac.compare_digest(raw[-1], _tag(row, keys, schema)))
    return row


def read_raw(conn: Connection[Any], kind: type[Record], key: object, *, lock: bool = False) -> tuple[Any, ...] | None:
    table = _TABLES[kind][0]
    query = sql.SQL("SELECT {} FROM {} WHERE {} = %s{}").format(
        columns(kind), sql.Identifier(table), sql.Identifier(names(kind)[0]), sql.SQL(" FOR UPDATE" if lock else "")
    )
    return conn.execute(query, (key,)).fetchone()


def read[R: (Registry, Issuer, Entry)](
    conn: Connection[Any], kind: type[R], key: object, keys: Keys, schema: str, *, lock: bool = False
) -> R | None:
    raw = read_raw(conn, kind, key, lock=lock)
    return None if raw is None else verified(kind, raw, keys, schema)


def write(conn: Connection[Any], row: Record, keys: Keys, schema: str, *, insert: bool = False) -> None:
    kind = type(row)
    table = sql.Identifier(_TABLES[kind][0])
    values = (*_values(row), _tag(row, keys, schema))
    if insert:
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in values)
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(table, columns(kind), placeholders)
        result = conn.execute(query, values)
    else:
        assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in names(kind)[1:])
        query = sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(table, assignments, sql.Identifier(names(kind)[0]))
        result = conn.execute(query, (*values[1:], values[0]))
    _require(result.rowcount == 1)


def _aad(schema: str, epoch: bytes) -> str:
    return f"launch-registry-v1:{schema}:1:{epoch.hex()}"


def _check_legacy_proof(conn: Connection[Any], master: MasterKey) -> None:
    from latet.server.master import _CHECK, _NOT_THE_FIRST, MasterKeyError, _opened

    # Read existing legacy proof without creating a check or choosing a second master.
    legacy = conn.execute("SELECT sealed FROM master_check WHERE id = 1").fetchone()
    if legacy is not None:
        if _opened(master, legacy[0], "master-check") != _CHECK:
            raise MasterKeyError(_NOT_THE_FIRST)
    else:
        kept = conn.execute(
            "SELECT identity_id, sealed FROM root_keys ORDER BY created, identity_id LIMIT 1"
        ).fetchone()
        if kept is not None and _opened(master, kept[1], f"root-key:{kept[0]}") is None:
            raise MasterKeyError(_NOT_THE_FIRST)


def bootstrap(conn: Connection[Any], schema: str, master: MasterKey) -> None:
    _check_legacy_proof(conn, master)
    epoch, secret = secrets.token_bytes(16), secrets.token_bytes(32)
    sealed = master.sealer(_PURPOSE).seal(_MAGIC + epoch + secret, _aad(schema, epoch))
    row = Registry(1, FORMAT, epoch, sealed, master.version, 0, 0, 0)
    write(conn, row, Keys.derive(epoch, secret), schema, insert=True)


def open_registry(
    conn: Connection[Any], schema: str, master: MasterKey, *, lock: bool = False
) -> tuple[Registry, Keys, bytes]:
    raw = read_raw(conn, Registry, 1, lock=lock)
    if raw is None:
        raise LaunchUnavailable()
    row = _decoded(Registry, raw)
    try:
        plain = master.sealer(_PURPOSE).open(row.sealed_secret, _aad(schema, row.epoch))
    except (InvalidTag, ValueError):
        raise LaunchUnavailable() from None
    prefix = _MAGIC + row.epoch
    _require(len(plain) == len(prefix) + 32 and plain.startswith(prefix))
    secret = plain[len(prefix) :]
    keys = Keys.derive(row.epoch, secret)
    return verified(Registry, raw, keys, schema), keys, secret


def check_applied_launch_registry(db: Database, master: MasterKey) -> None:
    """Before legacy proof creation, validate the root of an already-applied 014."""
    kind = ""  # an error of PostgreSQL's by its type alone, raised past the except: its words never the context
    try:
        with db.connection() as conn:
            table = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
            if table is not None and table[0] is not None:
                applied = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = %s", (BOOTSTRAP_MIGRATION,)
                ).fetchone()
                if applied is not None:
                    _check_legacy_proof(conn, master)
                    open_registry(conn, db.schema, master)
    except Error as error:
        kind = type(error).__name__
    if kind:
        raise DatabaseError(f"the protected launch registry could not be checked ({kind})")


def _startup_census(db: Database, master: MasterKey) -> Keys:
    """Authenticate and reconcile the bounded protected census at startup, without repair."""
    with db.transaction() as conn:
        _check_legacy_proof(conn, master)
        global_row, keys, _secret = open_registry(conn, db.schema, master, lock=True)
        issuers = {}
        for raw in conn.execute(sql.SQL("SELECT {} FROM launch_identity LIMIT 4097").format(columns(Issuer))):
            issuer = verified(Issuer, raw, keys, db.schema)
            issuers[issuer.issuer] = (issuer.entries, issuer.bindings)
        counts: dict[str, tuple[int, int]] = {}
        entries = bindings = 0
        for raw in conn.execute(sql.SQL("SELECT {} FROM launch_entry LIMIT 4097").format(columns(Entry))):
            entry = verified(Entry, raw, keys, db.schema)
            n, b = counts.get(entry.issuer, (0, 0))
            counts[entry.issuer] = (n + 1, b + entry.binding_reserved)
            entries += 1
            bindings += entry.binding_reserved
        _require(issuers == counts and (entries, bindings) == (global_row.entries, global_row.bindings))
    return keys


def check_launch_registry(db: Database, master: MasterKey) -> Keys:
    try:
        return _startup_census(db, master)
    except Error:
        raise LaunchUnavailable() from None


def rewrap_launch_registry(db: Database, master: MasterKey) -> bool:
    with db.transaction() as conn:
        row, keys, secret = open_registry(conn, db.schema, master, lock=True)
        _require(master.version >= row.wrap_version)
        if master.version == row.wrap_version:
            return False
        blob = master.sealer(_PURPOSE).seal(_MAGIC + row.epoch + secret, _aad(db.schema, row.epoch))
        updated = replace(row, sealed_secret=blob, wrap_version=master.version, revision=advance(row.revision))
        tag = _tag(updated, keys, db.schema)
        result = conn.execute(
            "UPDATE launch_registry SET sealed_secret = %s, wrap_version = %s, revision = %s, tag = %s "
            "WHERE singleton = 1 AND sealed_secret = %s AND revision = %s",
            (blob, updated.wrap_version, updated.revision, tag, row.sealed_secret, row.revision),
        )
        _require(result.rowcount == 1)
    return True
