"""Protected launch transitions. SQL locks never span a Valkey operation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import psycopg
from psycopg import sql

from latet.gateway.launch_keys import BINDING_BYTES, BINDINGS, ENTRIES, IDENTITY_LIVE
from latet.server.launch_binding import (
    Binding,
    BindingAddress,
    BindingCodec,
    RemoteResult,
    WireResult,
    binding_key,
    binding_once,
    canonical_credential,
)
from latet.server.launch_records import (
    ISSUER,
    MAX_TIME,
    Entry,
    Issuer,
    Keys,
    LaunchUnavailable,
    Operation,
    Reason,
    Registry,
    State,
    advance,
    check_launch_registry,
    columns,
    deadline,
    exact_bytes,
    names,
    read,
    uint,
    verified,
    write,
)

if TYPE_CHECKING:
    from latet.server.bus import Bus
    from latet.server.db import Database
    from latet.server.master import MasterKey

_NOW = "floor(extract(epoch FROM clock_timestamp()) * 1000000)::bigint"
_T = TypeVar("_T")


def _require(condition: bool) -> None:
    if not condition:
        raise LaunchUnavailable()


def _now(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(sql.SQL("SELECT " + _NOW)).fetchone()
    if row is None or not uint(row[0], MAX_TIME):
        raise LaunchUnavailable()
    return row[0]


def _hash(blob: bytes) -> bytes:
    return hashlib.sha256(blob).digest()


@dataclass(frozen=True, slots=True, repr=False)
class Receipt:
    address: BindingAddress
    binding_digest: bytes
    blob_digest: bytes


@dataclass(frozen=True, slots=True, repr=False)
class Decision:
    state: Literal["claimed", "refused", "unavailable", "live"]
    receipt: Receipt | None = None
    binding: Binding | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Ticket:
    issuer: str
    locator: bytes
    launch_id: bytes
    epoch: bytes
    owner: bytes
    operation: bytes
    kind: Operation
    sequence: int
    base: int
    target: int
    payload_digest: bytes | None


class Publication:
    """An admitted operation and one bounded, sealed dispatch payload."""

    __slots__ = ("_attempted", "_blob", "_confirmed", "_lock", "ticket")

    def __init__(self, ticket: Ticket, blob: bytes | None) -> None:
        self.ticket = ticket
        self._blob = blob
        self._lock = threading.Lock()
        self._attempted = False
        self._confirmed = False

    def _confirm(self) -> None:
        with self._lock:
            self._confirmed = True

    @property
    def attempted(self) -> bool:
        with self._lock:
            return self._attempted

    def take(self) -> bytes | None:
        with self._lock:
            _require(self._confirmed and not self._attempted)
            self._attempted = True
            blob, self._blob = self._blob, None
            return blob


class ReservationUncertain(LaunchUnavailable):
    """Keep this exact bounded owner. A fresh lock can inspect, never replay, its admission."""

    def __init__(self, publication: Publication) -> None:
        super().__init__()
        self.publication = publication


class _TransactionUncertain(LaunchUnavailable):
    pass


class LaunchCapacity(Exception):
    """The authenticated protected counts conclusively refused a new OPEN."""


class TerminalContended(LaunchUnavailable):
    """The old hold cannot stop an already admitted successor."""


@dataclass(frozen=True, slots=True, repr=False)
class Completion:
    ticket: Ticket
    wire: WireResult
    settled: bool
    receipt: Receipt | None = None


@dataclass(frozen=True, slots=True, repr=False)
class TerminalTarget:
    locator: bytes
    generation: int
    binding_reserved: bool
    has_operation: bool


def _receipt(row: Entry) -> Receipt:
    if row.published_token is None or row.published_blob_digest is None:
        raise LaunchUnavailable()
    return Receipt(
        BindingAddress(row.epoch, row.issuer, row.locator, row.launch_id, row.generation, row.published_token),
        row.binding_digest,
        row.published_blob_digest,
    )


def _terminal(row: Entry, reason: Reason, *, exit_confirmed: bool = False) -> Entry:
    return replace(
        row,
        state=State.TERMINAL,
        terminal_reason=row.terminal_reason if row.state == State.TERMINAL else reason,
        exit_confirmed=row.exit_confirmed or exit_confirmed,
    )


def _clear(row: Entry) -> Entry:
    return replace(
        row,
        op_kind=Operation.NONE,
        op_id=None,
        op_owner=None,
        op_base_gen=None,
        op_target_gen=None,
        op_payload_digest=None,
    )


def _matches(row: Entry, ticket: Ticket) -> bool:
    return (
        row.epoch == ticket.epoch
        and row.issuer == ticket.issuer
        and row.locator == ticket.locator
        and row.launch_id == ticket.launch_id
        and row.op_owner == ticket.owner
        and row.op_id == ticket.operation
        and row.op_kind == ticket.kind
        and row.op_seq == ticket.sequence
        and row.op_base_gen == ticket.base
        and row.op_target_gen == ticket.target
        and row.op_payload_digest == ticket.payload_digest
    )


class LaunchAuthority:
    """The synchronous protected core. A caller owns each submitted operation until completion."""

    __slots__ = ("_bus", "_db", "_keys", "_owner", "_schema")

    def __init__(self, db: Database, bus: Bus, master: MasterKey) -> None:
        self._initialize(db, bus, check_launch_registry(db, master))

    @classmethod
    def _from_keys(cls, db: Database, bus: Bus, keys: Keys) -> LaunchAuthority:
        authority = cls.__new__(cls)
        authority._initialize(db, bus, keys)
        return authority

    def _initialize(self, db: Database, bus: Bus, keys: Keys) -> None:
        self._db, self._bus, self._schema = db, bus, db.schema
        self._keys = keys
        self._owner = secrets.token_bytes(16)

    def locator(self, credential: bytes) -> bytes:
        """Project a bounded credential on the caller's thread before worker submission."""
        message = b"latet-launch-locator-v1\0" + canonical_credential(credential)
        return hmac.digest(self._keys.locator, message, "sha256")

    def _transaction(self, action: Callable[[psycopg.Connection[Any], Registry, bool], _T]) -> _T:
        for attempt in range(2):
            try:
                with self._db.transaction() as conn:
                    registry = read(conn, Registry, 1, self._keys, self._schema, lock=True)
                    if registry is None:
                        raise LaunchUnavailable()
                    answer = action(conn, registry, attempt != 0)
                return answer
            except psycopg.Error:
                if attempt:
                    raise _TransactionUncertain() from None
            except (LaunchUnavailable, LaunchCapacity):
                if attempt:
                    raise _TransactionUncertain() from None
                raise
        raise LaunchUnavailable()

    def _locked(self, conn: psycopg.Connection[Any], locator: bytes, issuer: str) -> tuple[Issuer | None, Entry | None]:
        # The singleton lock is already held. Authenticate the target before locking its issuer.
        seen = read(conn, Entry, locator, self._keys, self._schema)
        if seen is not None and seen.issuer != issuer:
            raise LaunchUnavailable()
        counter = read(conn, Issuer, issuer, self._keys, self._schema, lock=True)
        if counter is None:
            dangling = conn.execute("SELECT 1 FROM launch_entry WHERE issuer = %s LIMIT 1", (issuer,)).fetchone()
            _require(dangling is None)
        row = read(conn, Entry, locator, self._keys, self._schema, lock=True)
        _require(row == seen and (row is None or counter is not None))
        return counter, row

    def _save(self, conn: psycopg.Connection[Any], row: Entry) -> None:
        write(conn, replace(row, revision=advance(row.revision)), self._keys, self._schema)

    def _ticket(self, row: Entry, operation: bytes, kind: Operation, target: int, payload: bytes | None) -> Ticket:
        return Ticket(
            row.issuer,
            row.locator,
            row.launch_id,
            row.epoch,
            self._owner,
            operation,
            kind,
            advance(row.op_seq),
            row.generation,
            target,
            payload,
        )

    @staticmethod
    def _mark(row: Entry, ticket: Ticket) -> Entry:
        return replace(
            row,
            op_seq=ticket.sequence,
            op_kind=ticket.kind,
            op_id=ticket.operation,
            op_owner=ticket.owner,
            op_base_gen=ticket.base,
            op_target_gen=ticket.target,
            op_payload_digest=ticket.payload_digest,
        )

    def _key(self, locator: bytes) -> bytes:
        return binding_key(self._bus, self._keys.epoch, locator)

    def reserve_open(
        self, issuer: str, locator: bytes, launch_id: bytes, binding: Binding, verifier: bytes, codec: BindingCodec
    ) -> Publication:
        _require(type(issuer) is str and ISSUER.fullmatch(issuer) is not None)
        _require(exact_bytes(locator, 32) and exact_bytes(launch_id, 16) and exact_bytes(verifier, 32))
        operation = secrets.token_bytes(16)
        address = BindingAddress(self._keys.epoch, issuer, locator, launch_id, 0, operation)
        digest = binding.digest(self._keys.auth, issuer, locator, launch_id)
        blob = codec.seal(self._key(locator), address, verifier, binding)
        candidate = Entry(
            locator,
            launch_id,
            issuer,
            self._keys.epoch,
            0,
            0,
            0,
            State.PENDING,
            0,
            True,
            digest,
            None,
            None,
            Reason.NONE,
            False,
            False,
            Operation.NONE,
            None,
            None,
            None,
            None,
            None,
        )
        ticket = self._ticket(candidate, operation, Operation.OPEN, 0, _hash(blob))

        def reserve(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> None:
            counter, existing = self._locked(conn, locator, issuer)
            if existing is not None:
                _require(retry and _matches(existing, ticket))
                return
            if registry.entries >= ENTRIES or registry.bindings >= BINDINGS:
                raise LaunchCapacity()
            fresh = counter is None
            if counter is None:
                counter = Issuer(issuer, self._keys.epoch, 0, 0, 0)
            if counter.bindings >= IDENTITY_LIVE:
                raise LaunchCapacity()
            now = _now(conn)
            write(
                conn,
                replace(
                    counter,
                    entries=counter.entries + 1,
                    bindings=counter.bindings + 1,
                    revision=0 if fresh else advance(counter.revision),
                ),
                self._keys,
                self._schema,
                insert=fresh,
            )
            entry = self._mark(replace(candidate, deadline_us=deadline(now)), ticket)
            write(conn, entry, self._keys, self._schema, insert=True)
            write(
                conn,
                replace(
                    registry,
                    entries=registry.entries + 1,
                    bindings=registry.bindings + 1,
                    revision=advance(registry.revision),
                ),
                self._keys,
                self._schema,
            )

        publication = Publication(ticket, blob)
        try:
            self._reserve(publication, reserve)
        except (LaunchUnavailable, LaunchCapacity):
            raise
        except Exception:
            raise ReservationUncertain(publication) from None
        return publication

    def _reserve(
        self, publication: Publication, reserve: Callable[[psycopg.Connection[Any], Registry, bool], None]
    ) -> None:
        try:
            self._transaction(reserve)
        except _TransactionUncertain:
            raise ReservationUncertain(publication) from None
        publication._confirm()

    def reconcile_reservation(self, publication: Publication) -> bool:
        """Inspect the same unsubmitted marker under the common lock, without a new reservation."""
        ticket = publication.ticket
        _require(ticket.owner == self._owner)
        try:
            with self._db.transaction() as conn:
                registry = read(conn, Registry, 1, self._keys, self._schema, lock=True)
                _require(registry is not None)
                _, row = self._locked(conn, ticket.locator, ticket.issuer)
                confirmed = row is not None and _matches(row, ticket)
                if row is not None and not confirmed:
                    _require(row.launch_id == ticket.launch_id and row.op_seq < ticket.sequence)
        except (psycopg.Error, LaunchUnavailable):
            raise ReservationUncertain(publication) from None
        if confirmed:
            publication._confirm()
        return confirmed

    def _snapshot(self, key: bytes, *, by_id: bool = False) -> tuple[Entry | None, int]:
        query = sql.SQL(
            "SELECT {}, {}, {}, {} FROM launch_registry g "
            "LEFT JOIN launch_entry e ON e.{} = %s "
            "LEFT JOIN launch_identity i ON i.issuer = e.issuer WHERE g.singleton = 1"
        ).format(
            columns(Registry, "g"),
            columns(Entry, "e"),
            columns(Issuer, "i"),
            sql.SQL(_NOW),
            sql.Identifier("launch_id" if by_id else "locator"),
        )
        try:
            with self._db.connection() as conn:
                raw = conn.execute(query, (key,)).fetchone()
        except psycopg.Error:
            raise LaunchUnavailable() from None
        if raw is None or not uint(raw[-1], MAX_TIME):
            raise LaunchUnavailable()
        g, e = len(names(Registry)), len(names(Entry))
        registry = verified(Registry, raw[:g], self._keys, self._schema)
        now = raw[-1]
        if raw[g] is None:
            return None, now
        entry = verified(Entry, raw[g : g + e], self._keys, self._schema)
        counter = verified(Issuer, raw[g + e : -1], self._keys, self._schema)
        _require(
            entry.issuer == counter.issuer
            and 0 < counter.entries <= registry.entries
            and int(entry.binding_reserved) <= counter.bindings <= registry.bindings
        )
        return entry, now

    def expired_locators(self, cursor: bytes | None, limit: int) -> list[bytes]:
        """Project a single ordered page. Each chosen locator needs its own authenticated snapshot."""
        query = sql.SQL(
            "SELECT locator FROM launch_entry WHERE binding_reserved AND op_kind = 0 AND deadline_us <= {}"
            " AND locator > %s ORDER BY locator LIMIT %s"
        ).format(sql.SQL(_NOW))
        with self._db.connection() as conn:
            return [row[0] for row in conn.execute(query, (cursor if cursor is not None else b"", limit))]

    def _binding(self, receipt: Receipt, codec: BindingCodec) -> tuple[bytes, Binding, bytes]:
        address = receipt.address
        key = self._key(address.locator)
        try:
            blob = self._bus.client.getrange(key, 0, BINDING_BYTES)
        except Exception:
            raise LaunchUnavailable() from None
        _require(
            type(blob) is bytes
            and 30 <= len(blob) <= BINDING_BYTES
            and hmac.compare_digest(_hash(blob), receipt.blob_digest)
        )
        verifier, binding = codec.open(key, address, blob)
        digest = binding.digest(self._keys.auth, address.issuer, address.locator, address.launch_id)
        _require(hmac.compare_digest(digest, receipt.binding_digest))
        return verifier, binding, blob

    def decide(
        self, issuer: str, locator: bytes, verifier: bytes, codec: BindingCodec, *, claimed_folder: str | None = None
    ) -> Decision:
        try:
            _require(exact_bytes(locator, 32) and exact_bytes(verifier, 32))
            row, now = self._snapshot(locator)
            if row is None:
                return Decision("claimed")
            if row.issuer != issuer or row.state != State.LIVE or row.deadline_us <= now:
                return Decision("refused")
            receipt = _receipt(row)
            expected, binding, _blob = self._binding(receipt, codec)
            if not hmac.compare_digest(verifier, expected) or (
                claimed_folder is not None and not binding.contains(claimed_folder)
            ):
                return Decision("refused")
            if not self.revalidate(receipt):
                return Decision("refused")
            return Decision("live", receipt, binding)
        except LaunchUnavailable:
            return Decision("unavailable")

    def revalidate(self, receipt: Receipt) -> bool:
        row, now = self._snapshot(receipt.address.locator)
        return row is not None and row.state == State.LIVE and row.deadline_us > now and _receipt(row) == receipt

    def _admit(self, row: Entry, ticket: Ticket, blob: bytes) -> Publication:
        def reserve(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> None:
            _, current = self._locked(conn, ticket.locator, ticket.issuer)
            if current is None:
                raise LaunchUnavailable()
            if retry and _matches(current, ticket):
                return
            now = _now(conn)
            _require(current == row and current.op_kind == Operation.NONE and current.deadline_us > now)
            self._save(conn, self._mark(current, ticket))

        publication = Publication(ticket, blob)
        try:
            self._reserve(publication, reserve)
        except LaunchUnavailable:
            raise
        except Exception:
            raise ReservationUncertain(publication) from None
        return publication

    def _replace(
        self, row: Entry, kind: Operation, binding: Binding, verifier: bytes, codec: BindingCodec
    ) -> Publication:
        _require(binding.digest(self._keys.auth, row.issuer, row.locator, row.launch_id) == row.binding_digest)
        operation, target = secrets.token_bytes(16), advance(row.generation)
        address = BindingAddress(row.epoch, row.issuer, row.locator, row.launch_id, target, operation)
        blob = codec.seal(self._key(row.locator), address, verifier, binding)
        return self._admit(row, self._ticket(row, operation, kind, target, _hash(blob)), blob)

    def reserve_hold(self, issuer: str, launch_id: bytes, generation: int, codec: BindingCodec) -> Publication:
        _require(exact_bytes(launch_id, 16) and uint(generation))
        row, now = self._snapshot(launch_id, by_id=True)
        if (
            row is None
            or row.issuer != issuer
            or row.generation != generation
            or row.deadline_us <= now
            or row.state not in (State.PENDING, State.LIVE)
            or row.op_kind != Operation.NONE
        ):
            raise LaunchUnavailable()
        verifier, binding, _blob = self._binding(_receipt(row), codec)
        return self._replace(row, Operation.HOLD, binding, verifier, codec)

    def reserve_recovery(
        self, issuer: str, locator: bytes, generation: int, binding: Binding, verifier: bytes, codec: BindingCodec
    ) -> Publication:
        _require(exact_bytes(locator, 32) and uint(generation) and exact_bytes(verifier, 32))
        row, now = self._snapshot(locator)
        if (
            row is None
            or row.issuer != issuer
            or row.generation != generation
            or row.deadline_us <= now
            or row.state != State.LIVE
            or row.op_kind != Operation.NONE
        ):
            raise LaunchUnavailable()
        return self._replace(row, Operation.RECOVER, binding, verifier, codec)

    def recover_current(
        self,
        issuer: str,
        locator: bytes,
        launch_id: bytes,
        epoch: bytes,
        retirement: int,
        selected: Binding,
        verifier: bytes,
        codec: BindingCodec,
    ) -> tuple[Publication, Binding]:
        _require(
            exact_bytes(locator, 32)
            and exact_bytes(launch_id, 16)
            and exact_bytes(verifier, 32)
            and type(epoch) is bytes
            and epoch == self._keys.epoch
            and type(retirement) is int
            and retirement == 0
        )
        row, now = self._snapshot(locator)
        if (
            row is None
            or row.issuer != issuer
            or row.launch_id != launch_id
            or row.state != State.LIVE
            or row.deadline_us <= now
            or row.op_kind != Operation.NONE
        ):
            raise LaunchUnavailable()
        stored_verifier, stored, _blob = self._binding(_receipt(row), codec)
        _require(
            hmac.compare_digest(verifier, stored_verifier)
            and selected.fields()[:2] == stored.fields()[:2]
            and (selected.flavor, selected.source) == (stored.flavor, stored.source)
        )
        return self._replace(row, Operation.RECOVER, stored, verifier, codec), stored

    def accepts_epoch(self, epoch: bytes) -> bool:
        return type(epoch) is bytes and epoch == self._keys.epoch

    def reserve_refresh(self, completed_hold: Completion, codec: BindingCodec) -> Publication:
        held = completed_hold.receipt
        _require(
            completed_hold.settled
            and completed_hold.ticket.owner == self._owner
            and completed_hold.ticket.kind in (Operation.HOLD, Operation.REFRESH)
        )
        if held is None:
            raise LaunchUnavailable()
        row, now = self._snapshot(held.address.locator)
        if (
            row is None
            or row.state != State.LIVE
            or row.deadline_us <= now
            or row.op_kind != Operation.NONE
            or _receipt(row) != held
        ):
            raise LaunchUnavailable()
        _verifier, _binding, blob = self._binding(held, codec)
        ticket = self._ticket(row, secrets.token_bytes(16), Operation.REFRESH, row.generation, _hash(blob))
        return self._admit(row, ticket, blob)

    def terminal(self, issuer: str, locator: bytes, generation: int, *, exit_confirmed: bool = False) -> bool:
        _require(exact_bytes(locator, 32) and uint(generation) and type(exit_confirmed) is bool)

        def end(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> bool:
            _, row = self._locked(conn, locator, issuer)
            if row is None or row.generation != generation:
                return False
            updated = _terminal(row, Reason.ENDED, exit_confirmed=exit_confirmed)
            if updated != row:
                self._save(conn, updated)
            return True

        return self._transaction(end)

    def terminal_control(
        self,
        issuer: str,
        launch_id: bytes,
        *,
        generation: int | None = None,
        operation: Ticket | None = None,
        exit_confirmed: bool = False,
    ) -> TerminalTarget | None:
        _require(
            exact_bytes(launch_id, 16) and (generation is None or uint(generation)) and type(exit_confirmed) is bool
        )
        if operation is not None:
            _require(
                generation is None
                and not exit_confirmed
                and operation.owner == self._owner
                and operation.issuer == issuer
                and operation.launch_id == launch_id
                and operation.kind in (Operation.HOLD, Operation.REFRESH)
            )

        def end(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> TerminalTarget | None:
            located = conn.execute("SELECT locator FROM launch_entry WHERE launch_id = %s", (launch_id,)).fetchone()
            if located is None:
                return None
            _, row = self._locked(conn, located[0], issuer)
            _require(row is not None and row.launch_id == launch_id)
            if row is None or (generation is not None and row.generation != generation):
                return None
            if generation is not None and row.op_target_gen is not None and row.op_target_gen > generation:
                raise TerminalContended()
            if operation is not None:
                successor = (
                    row.op_kind == Operation.NONE
                    and row.epoch == operation.epoch
                    and row.locator == operation.locator
                    and row.generation == operation.target
                    and row.op_seq == operation.sequence
                    and row.published_blob_digest == operation.payload_digest
                    and (operation.kind == Operation.REFRESH or row.published_token == operation.operation)
                )
                if not (_matches(row, operation) or successor):
                    return None
            updated = _terminal(row, Reason.ENDED, exit_confirmed=exit_confirmed)
            if updated != row:
                self._save(conn, updated)
            return TerminalTarget(row.locator, row.generation, row.binding_reserved, row.op_kind != Operation.NONE)

        return self._transaction(end)

    def reserve_cleanup(
        self, issuer: str, locator: bytes, *, launch_id: bytes | None = None, generation: int | None = None
    ) -> Publication | None:
        _require(exact_bytes(locator, 32))
        operation = secrets.token_bytes(16)
        owned: Publication | None = None

        def reserve(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> Ticket | None:
            nonlocal owned
            _, row = self._locked(conn, locator, issuer)
            if row is None or not row.binding_reserved:
                return None
            if launch_id is not None and (row.launch_id != launch_id or row.generation != generation):
                return None
            if retry and row.op_id == operation and row.op_owner == self._owner and row.op_kind == Operation.DELETE:
                return Ticket(
                    issuer,
                    locator,
                    row.launch_id,
                    row.epoch,
                    self._owner,
                    operation,
                    Operation.DELETE,
                    row.op_seq,
                    row.generation,
                    row.generation,
                    None,
                )
            if launch_id is not None and row.op_kind != Operation.NONE:
                return None
            now = _now(conn)
            updated = _terminal(row, Reason.EXPIRED) if row.deadline_us <= now else row
            if updated.state != State.TERMINAL or updated.op_kind != Operation.NONE:
                if updated != row:
                    self._save(conn, updated)
                return None
            ticket = self._ticket(updated, operation, Operation.DELETE, updated.generation, None)
            owned = Publication(ticket, None)
            self._save(conn, self._mark(updated, ticket))
            return ticket

        try:
            ticket = self._transaction(reserve)
        except Exception as error:
            ambiguous = isinstance(error, _TransactionUncertain) or not isinstance(error, LaunchUnavailable)
            if owned is not None and ambiguous:
                raise ReservationUncertain(owned) from None
            raise
        if ticket is None:
            return None
        publication = Publication(ticket, None)
        publication._confirm()
        return publication

    def submit(self, publication: Publication) -> Completion:
        ticket = publication.ticket
        _require(ticket.owner == self._owner)
        blob = publication.take()
        _require(ticket.payload_digest == (_hash(blob) if blob is not None else None))

        wire = binding_once(self._bus, ticket.epoch, ticket.locator, blob)
        return self._complete(ticket, wire)

    def abandon_unsubmitted(self, publication: Publication) -> Completion:
        """Consume a confirmed owner that never started its one allowed Redis attempt."""
        ticket = publication.ticket
        _require(ticket.owner == self._owner)
        publication.take()
        return self._complete(ticket, WireResult(RemoteResult.NOT_SUBMITTED))

    def _complete(self, ticket: Ticket, wire: WireResult) -> Completion:
        if wire.remote == RemoteResult.UNKNOWN:
            return Completion(ticket, wire, False)
        try:
            receipt = self._finish(ticket, wire.remote)
        except LaunchUnavailable:
            return Completion(ticket, wire, False)
        return Completion(ticket, wire, True, receipt)

    def reconcile_completion(self, completion: Completion) -> Completion:
        """Settle the original known wire result in SQL, without another wire or pool action."""
        ticket, wire = completion.ticket, completion.wire
        _require(
            ticket.owner == self._owner
            and not completion.settled
            and wire.remote != RemoteResult.UNKNOWN
            and wire.cleanup_pending is None
        )
        receipt = self._finish(ticket, wire.remote, reconcile=True)
        return replace(completion, settled=True, receipt=receipt)

    def _finish(self, ticket: Ticket, outcome: RemoteResult, *, reconcile: bool = False) -> Receipt | None:
        def finish(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> Receipt | None:
            counter, row = self._locked(conn, ticket.locator, ticket.issuer)
            if counter is None or row is None or row.launch_id != ticket.launch_id:
                raise LaunchUnavailable()
            now = _now(conn)
            if not _matches(row, ticket):
                # A successful COMMIT whose acknowledgement was lost. Only the exact signed successor is usable.
                _require(
                    (retry or reconcile)
                    and row.epoch == ticket.epoch
                    and row.op_seq == ticket.sequence
                    and row.op_kind == Operation.NONE
                    and row.generation in (ticket.base, ticket.target)
                )
                if outcome == RemoteResult.SET and row.state != State.TERMINAL:
                    token_matches = ticket.kind == Operation.REFRESH or row.published_token == ticket.operation
                    _require(
                        row.generation == ticket.target
                        and token_matches
                        and row.published_blob_digest == ticket.payload_digest
                    )
                    return _receipt(row) if row.deadline_us > now else None
                _require(
                    outcome == RemoteResult.NOT_SUBMITTED
                    or (row.state == State.TERMINAL and (outcome != RemoteResult.DELETE or not row.binding_reserved))
                )
                return None
            updated = _clear(row)
            if outcome == RemoteResult.DELETE:
                _require(ticket.kind == Operation.DELETE and row.state == State.TERMINAL and row.binding_reserved)
                _require(registry.bindings > 0 and counter.bindings > 0)
                updated = replace(updated, binding_reserved=False, published_token=None, published_blob_digest=None)
                write(
                    conn,
                    replace(counter, bindings=counter.bindings - 1, revision=advance(counter.revision)),
                    self._keys,
                    self._schema,
                )
                write(
                    conn,
                    replace(registry, bindings=registry.bindings - 1, revision=advance(registry.revision)),
                    self._keys,
                    self._schema,
                )
            elif row.deadline_us <= now and row.state != State.TERMINAL:
                updated = _terminal(updated, Reason.EXPIRED)
            elif outcome == RemoteResult.NOT_SUBMITTED and ticket.kind == Operation.OPEN:
                updated = _terminal(updated, Reason.FAILED)
            elif outcome == RemoteResult.SET and row.state != State.TERMINAL:
                if ticket.kind == Operation.REFRESH:
                    updated = replace(updated, deadline_us=deadline(now))
                else:
                    updated = replace(
                        updated,
                        generation=ticket.target,
                        published_token=ticket.operation,
                        published_blob_digest=ticket.payload_digest,
                        disclosure_possible=True,
                        state=State.PENDING if ticket.kind == Operation.OPEN else State.LIVE,
                        deadline_us=deadline(now) if ticket.kind == Operation.HOLD else row.deadline_us,
                    )
            self._save(conn, updated)
            return _receipt(updated) if outcome == RemoteResult.SET and updated.state != State.TERMINAL else None

        return self._transaction(finish)

    def retire(self, issuer: str, locator: bytes, generation: int, *, undisclosed: Completion | None = None) -> bool:
        _require(exact_bytes(locator, 32) and uint(generation))

        def retire(conn: psycopg.Connection[Any], registry: Registry, retry: bool) -> bool:
            counter, row = self._locked(conn, locator, issuer)
            if row is None or counter is None:
                if retry:
                    raise LaunchUnavailable()
                return False
            if (
                row.generation != generation
                or row.state != State.TERMINAL
                or row.binding_reserved
                or row.op_kind != Operation.NONE
            ):
                return False
            original = undisclosed is not None and (
                undisclosed.settled
                and undisclosed.receipt is None
                and undisclosed.ticket.kind == Operation.OPEN
                and undisclosed.ticket.owner == self._owner
                and undisclosed.ticket.locator == locator
                and undisclosed.ticket.issuer == issuer
                and undisclosed.ticket.epoch == row.epoch
                and undisclosed.ticket.launch_id == row.launch_id
                and not row.disclosure_possible
            )
            if not row.exit_confirmed and not original:
                return False
            _require(registry.entries > 0 and counter.entries > 0)
            result = conn.execute("DELETE FROM launch_entry WHERE locator = %s", (locator,))
            _require(result.rowcount == 1)
            if counter.entries == 1:
                _require(counter.bindings == 0)
                result = conn.execute("DELETE FROM launch_identity WHERE issuer = %s", (issuer,))
                _require(result.rowcount == 1)
            else:
                write(
                    conn,
                    replace(counter, entries=counter.entries - 1, revision=advance(counter.revision)),
                    self._keys,
                    self._schema,
                )
            write(
                conn,
                replace(registry, entries=registry.entries - 1, revision=advance(registry.revision)),
                self._keys,
                self._schema,
            )
            return True

        return self._transaction(retire)
