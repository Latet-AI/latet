"""Bounded runtime-local decision visibility. These records grant no execution authority.

All retained growth is published before its admission check. The retained read
is deliberately lock-free on the qualified ordinary-GIL CPython runtime: Echoes
calls it while holding E, whereas Audit admissions observe E through Spaces.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from latet.gateway.body_coverage import BodyProof
from latet.gateway.cloud import BedrockUpstream, VertexUpstream
from latet.gateway.coverage import EmptyBodyProof
from latet.gateway.outcome import Block, Forward, Pass, PolicyEvidence, ResponsePlan
from latet.gateway.routes import DEFAULT_UPSTREAMS

if TYPE_CHECKING:
    from typing import Protocol, TypedDict

    from latet.server.live import ValkeyRequestStatus

    class _NativeRLock(Protocol):
        def _is_owned(self) -> bool: ...

    class _LocalStatus(TypedDict):
        epoch: str
        sequence: int
        counts: list[list[int | str]]
        overflow: int
        partial: bool
        reset: bool
        removed_headers: int
        saturated: bool


BASE_BYTES = 4096
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 1024
MAX_EVENT_BYTES = 4096
MAX_COUNTER = (1 << 63) - 1
STATUS_BYTES = 1024
SWEEP_BYTES = 2048
EXCEPTION_SLOTS = 128
BUCKETS = 8
RESERVATION_BYTES = 512
_RLOCK_TYPE = type(threading.RLock())


def _rounded(length: int) -> int:
    return (length + 255) // 256 * 256


def _event_charge(length: int) -> int:
    return 512 + _rounded(length + 64)


EVENT_RESERVE_BYTES = _event_charge(MAX_EVENT_BYTES)


@dataclass(frozen=True, slots=True)
class SafeDecisionRecord:
    """Finite codes only; unknown execution is None, never a guessed zero.

    Sources 0..5, outcomes 0..4, coverage 0..2, protocol 0..7, method
    0..9, route 0..8 and profile 0..4 are closed reporting vocabularies.
    Producers must map actual registry/proof facts; these codes are not policy.
    """

    source: int = 0
    outcome: int = 0
    coverage: int = 0
    protocol: int = 0
    method: int = 0
    route: int = 0
    profile: int = 0
    revision: int = 0
    reason: int = 0
    inspected: int | None = None
    checked: int | None = None
    removed: int | None = None
    authorized: int | None = None
    opaque: int | None = None
    rules: tuple[tuple[int, int], ...] = ()
    attempts: int | None = None
    answered: int | None = None
    status: int | None = None


def _integer(value: object, maximum: int, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _valid(record: SafeDecisionRecord) -> bool:
    if type(record) is not SafeDecisionRecord:
        return False
    for value, cap in (
        (record.source, 5),
        (record.outcome, 4),
        (record.coverage, 2),
        (record.protocol, 7),
        (record.method, 9),
        (record.route, 8),
        (record.profile, 4),
        (record.revision, MAX_COUNTER),
        (record.reason, 31),
    ):
        if not _integer(value, cap):
            return False
    for count in (record.inspected, record.checked, record.removed, record.authorized, record.opaque):
        if count is not None and not _integer(count, MAX_COUNTER):
            return False
    if type(record.rules) is not tuple or len(record.rules) > 8:
        return False
    seen = 0
    for pair in record.rules:
        if type(pair) is not tuple or len(pair) != 2:
            return False
        code, revision = pair
        if (
            not _integer(code, 8, 1)
            or code == _RETIRED
            or type(revision) is not int
            or revision != 1
            or seen & (1 << code)
        ):
            return False
        seen |= 1 << code
    attempts, answered = record.attempts, record.answered
    if attempts is not None and not _integer(attempts, 2 if record.source == 1 else 1):
        return False
    if answered is not None and (attempts is None or not _integer(answered, attempts)):
        return False
    return not (
        record.status is not None and (not _integer(record.status, 599, 100) or answered is None or answered == 0)
    )


def _encode(record: SafeDecisionRecord, sequence: int) -> bytes:
    # No caller document, repr, user string or arbitrary json encoder is used.
    return json.dumps(
        (
            1,
            sequence,
            record.source,
            record.outcome,
            record.coverage,
            record.protocol,
            record.method,
            record.route,
            record.profile,
            record.revision,
            record.reason,
            record.inspected,
            record.checked,
            record.removed,
            record.authorized,
            record.opaque,
            record.rules,
            record.attempts,
            record.answered,
            record.status,
        ),
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


@dataclass(frozen=True, slots=True, init=False)
class ExceptionReference:
    """Generated infrastructure identifier; it conveys no permission."""

    _id: str
    _revision: int
    _scope: int

    def __init__(self, revision: int, scope: int) -> None:
        if not _integer(revision, MAX_COUNTER, 1) or not _integer(scope, 7, 1):
            raise ValueError("invalid exception reference")
        object.__setattr__(self, "_id", secrets.token_hex(16))
        object.__setattr__(self, "_revision", revision)
        object.__setattr__(self, "_scope", scope)


def new_exception_reference(revision: int, scope: int) -> ExceptionReference:
    return ExceptionReference(revision, scope)


@dataclass(slots=True)
class _Status:
    uses: int = 0
    attempts: int = 0
    answers: int = 0
    active: int = 0


class Reservation:
    """Opaque active visibility lease; completion clears its owner reference."""

    __slots__ = ("_owner", "_sequence")

    def __init__(self, owner: RequestAudit, sequence: int) -> None:
        self._owner: RequestAudit | None = owner
        self._sequence = sequence


@dataclass(slots=True)
class _Entry:
    payload: bytes | None
    status: int | tuple[str, int, int]
    reservation: Reservation | _ProjectionScope | None
    charge: int
    sequence: int


class _ProjectionScope:
    """One synchronous, prepaid projection in the SAME event population."""

    __slots__ = ("_attempted", "_bucket", "_committed", "_new_status", "_owner", "_slot")

    def __init__(self, owner: RequestAudit, slot: int, bucket: int) -> None:
        self._owner, self._slot, self._bucket = owner, slot, bucket
        self._attempted = self._committed = self._new_status = False

    def commit(self, record: SafeDecisionRecord | None) -> bool:
        owner = self._owner
        if owner is None:
            return False
        with owner._lock:
            if self._attempted:
                return False
            self._attempted = True
            if owner._closed:
                return False
            if type(record) is not SafeDecisionRecord or not _valid(record):
                owner._missed()
                return False
            payload = row = None
            previous = owner._statuses.get(self._bucket)
            new = previous is None
            entry = owner._events[self._slot]
            try:
                sequence = owner._sum(owner._sequence, 1)
                payload = _encode(record, sequence)
                if len(payload) > MAX_EVENT_BYTES:
                    raise ValueError("audit event exceeds bound")
                # Count into an unpublished exact row, even for an existing
                # bucket. A failing integer allocation cannot partially mutate
                # the visible cumulative row or create an uncharged new one.
                row = (
                    _Status()
                    if previous is None
                    else _Status(previous.uses, previous.attempts, previous.answers, previous.active)
                )
                owner._count(row, record)
                owner._statuses[self._bucket] = row
                self._new_status = new
                entry.payload, entry.sequence = payload, sequence
                self._committed = True
                owner._sequence = sequence
            except BaseException as error:
                # Exact owned scalars only under A. Encoder/exception frame
                # aliases still retain the FULL escrow until outer cleanup.
                if new:
                    owner._statuses.pop(self._bucket, None)
                else:
                    if previous is not None:
                        owner._statuses[self._bucket] = previous
                entry.payload, entry.sequence = None, 0
                self._committed = self._new_status = False
                payload = row = None
                owner._missed()
                if isinstance(error, (MemoryError, ValueError)):
                    return False
                raise
            # FULL escrow stays charged across this return and caller aliases.
            return True

    def close(self) -> None:
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            entry = owner._events[self._slot]
            if self._committed and not owner._closed and entry.payload is not None:
                entry.reservation = None
                charge = _event_charge(len(entry.payload))
                entry.charge = charge
                owner._retained -= EVENT_RESERVE_BYTES - charge
            else:
                # No source/record/encoder frames remain. Exact internal bytes
                # end before compaction/refund, with the slot still protected.
                entry.payload = None
                owner._events.pop(self._slot)
                entry.charge = 0
                entry.reservation = None
                owner._compact(EVENT_RESERVE_BYTES)
            if not self._new_status:
                owner._retained -= STATUS_BYTES
            self._owner = None
            owner._projection = None
            if owner._closed:
                owner._ended()


class _SweepLease:
    """Small independent status-sweep claim; never a Work or event slot."""

    __slots__ = ("_owner",)

    def __init__(self, owner: RequestAudit) -> None:
        self._owner: RequestAudit | None = owner

    def close(self) -> None:
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            if self._owner is not owner:
                return
            self._owner = None
            owner._sweep = False
            owner._retained -= SWEEP_BYTES
            if owner._closed:
                owner._ended()


class RequestAudit:
    __slots__ = (
        "_active",
        "_ceiling",
        "_closed",
        "_cursor",
        "_epoch",
        "_events",
        "_lock",
        "_overflow",
        "_partial",
        "_pending",
        "_projection",
        "_retained",
        "_retired",
        "_saturated",
        "_sequence",
        "_snapshot",
        "_status_owner",
        "_statuses",
        "_sweep",
    )

    def __init__(self) -> None:
        # The fixed construction contribution is installed before Runtime is
        # published. All later owner allocations occur under positive precharge.
        self._retained = BASE_BYTES
        self._lock = threading.RLock()
        self._events: OrderedDict[int, _Entry] = OrderedDict()
        self._statuses: OrderedDict[int | tuple[str, int, int], _Status] = OrderedDict()
        self._sequence = self._overflow = self._active = self._cursor = self._retired = 0
        self._partial = self._saturated = self._closed = self._pending = False
        self._ceiling: Callable[[int], str | None] | None = None
        self._epoch = secrets.token_hex(16)
        self._snapshot: SnapshotWork | None = None
        self._status_owner: tuple[object, str, str] | None = None
        self._projection: _ProjectionScope | None = None
        self._sweep = False

    @property
    def retained_bytes(self) -> int:
        # Never take A here: Echoes can hold E while asking Runtime's sum.
        return self._retained

    @property
    def active(self) -> int:
        return self._active

    @property
    def protects_idle(self) -> bool:
        # Reporting active reservations is separate from a mutation's short
        # pending ownership. This read never takes A or an engine-owner lock.
        return (
            self._pending
            or self._active > 0
            or self._snapshot is not None
            or self._projection is not None
            or self._sweep
        )

    def try_retire_if_idle(self) -> bool:
        """Atomically seal an actual idle selection, without waiting on A.

        The lock-free protection read is only a fast conservative check.
        Acquiring A here arbitrates a worker starting after that earlier read:
        either its mutation/lease stays protected, or it sees the sealed owner.
        Cleanup/refunds remain with normal close, not the selection callback.
        """
        if type(self._lock) is not _RLOCK_TYPE:
            return False
        try:
            owned = cast("_NativeRLock", self._lock)._is_owned()
        except (AttributeError, RuntimeError):
            return False
        if type(owned) is not bool or owned:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if (
                self._pending
                or self._active
                or self._snapshot is not None
                or self._projection is not None
                or self._sweep
            ):
                return False
            self._closed = True
            return True
        finally:
            self._lock.release()

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def overflow(self) -> int:
        return self._overflow

    @property
    def partial(self) -> bool:
        return self._partial

    @property
    def saturated(self) -> bool:
        return self._saturated

    def bind_status(self, bus: object, identity: str, instance: str) -> bool:
        """Construction only: bounded descriptor in the already charged base.

        Bus/client are borrowed server owners; the tuple and generated IDs fit
        BASE_BYTES before Runtime publication/first Spaces weighing. Transports,
        keys, Script shells and buffers are created only inside charged work.
        """
        if (
            bus is None
            or type(identity) is not str
            or len(identity) != 12
            or not identity.startswith("u_")
            or any(c not in "abcdefghijklmnopqrstuvwxyz234567" for c in identity[2:])
            or type(instance) is not str
            or len(instance) != 12
            or any(c not in "0123456789abcdef" for c in instance)
        ):
            return False
        with self._lock:
            if self._closed or self._status_owner is not None:
                return False
            try:
                self._status_owner = (bus, identity, instance)
            except MemoryError:
                return False
            return True

    def bind_ceiling(self, ceiling: Callable[[int], str | None] | None) -> None:
        # Runtime construction/Spaces binding, before exposing that runtime.
        with self._lock:
            self._ceiling = ceiling

    def _sum(self, value: int, added: int) -> int:
        result = value + added
        if result > MAX_COUNTER:
            self._saturated = True
            return MAX_COUNTER
        return result

    def _missed(self) -> None:
        self._overflow = self._sum(self._overflow, 1)
        self._partial = True

    def _compact(self, retired: int) -> None:
        # A sparse OrderedDict keeps old table capacity. Keep the retired full
        # charge until both new compact containers exist and old capacity ends.
        self._retired += retired
        events = statuses = None
        try:
            events = OrderedDict(self._events)
            statuses = OrderedDict(self._statuses)
        except MemoryError:
            events = statuses = None
            self._partial = True
            return
        self._events, self._statuses = events, statuses
        self._retained -= self._retired
        self._retired = 0

    def _discard(self, slot: int) -> None:
        entry = self._events.pop(slot)
        charge = entry.charge
        del entry
        self._compact(charge)

    def _slot(self) -> int:
        # Storage identity is independent of the saturating public sequence.
        while self._cursor in self._events:
            self._cursor = (self._cursor + 1) % MAX_EVENTS
        slot = self._cursor
        self._cursor = (slot + 1) % MAX_EVENTS
        return slot

    def _room(self, charge: int, status: int | tuple[str, int, int]) -> bool:
        if status not in self._statuses:
            exceptions = sum(type(key) is tuple for key in self._statuses)
            if type(status) is tuple and exceptions >= EXCEPTION_SLOTS:
                victim = next(
                    (key for key, row in self._statuses.items() if type(key) is tuple and not row.active), None
                )
                if victim is None:
                    return False
                # Completed events retain their own bounded copies of the key;
                # they do not keep the retired mutable status row.
                del self._statuses[victim]
                self._compact(STATUS_BYTES)
                self._partial = True
        while len(self._events) >= MAX_EVENTS or self._retained + charge > MAX_BYTES:
            victim = next((seq for seq, entry in self._events.items() if entry.reservation is None), None)
            if victim is None:
                return False
            self._discard(victim)
        return True

    def _precharge(self, charge: int) -> bool:
        before = self._retained
        self._retained = before + charge
        try:
            if self._ceiling is not None and self._ceiling(0) is not None:
                self._retained = before
                return False
        except Exception:
            self._retained = before
            return False
        except BaseException:
            self._retained = before
            raise
        return True

    def _count(self, row: _Status, record: SafeDecisionRecord) -> None:
        if record.attempts is None or record.answered is None:
            self._partial = True
        row.uses = self._sum(row.uses, 1)
        # Unknown facts stay unknown in the event; totals only sum observed
        # invocations/answers. They are not presented as complete execution.
        row.attempts = self._sum(row.attempts, record.attempts or 0)
        row.answers = self._sum(row.answers, record.answered or 0)

    def _reserve_projection(self, bucket: int) -> _ProjectionScope | None:
        with self._lock:
            if self._closed:
                return None
            if self._projection is not None or not _integer(bucket, BUCKETS - 1):
                self._missed()
                return None
            self._pending = True
            try:
                # Even an existing status keeps the extra escrow until actual
                # source-projection/record/rules/encoder aliases have ended.
                charge = EVENT_RESERVE_BYTES + STATUS_BYTES
                if not self._room(charge, bucket) or not self._precharge(charge):
                    self._missed()
                    return None
                scope = entry = None
                slot = self._slot()
                try:
                    scope = _ProjectionScope(self, slot, bucket)
                    entry = _Entry(None, bucket, scope, EVENT_RESERVE_BYTES, 0)
                    self._events[slot] = entry
                    self._projection = scope
                except MemoryError:
                    self._events.pop(slot, None)
                    if scope is not None:
                        scope._owner = None
                    scope = entry = None
                    self._retained -= charge
                    self._missed()
                    return None
                return scope
            finally:
                self._pending = False

    def _reserve_sweep(self) -> _SweepLease | None:
        with self._lock:
            if self._closed or self._sweep:
                return None
            self._pending = True
            try:
                while self._retained + SWEEP_BYTES > MAX_BYTES:
                    slot = next((s for s, entry in self._events.items() if entry.reservation is None), None)
                    if slot is None:
                        self._missed()
                        return None
                    self._discard(slot)
                if not self._precharge(SWEEP_BYTES):
                    self._missed()
                    return None
                try:
                    lease = _SweepLease(self)
                except MemoryError:
                    self._retained -= SWEEP_BYTES
                    self._missed()
                    return None
                self._sweep = True
                return lease
            finally:
                self._pending = False

    def record(self, record: SafeDecisionRecord, *, bucket: int = 0) -> bool:
        with self._lock:
            if self._closed:
                return False
            if not _integer(bucket, BUCKETS - 1) or not _valid(record):
                self._missed()
                return False
            self._pending = True
            try:
                new = bucket not in self._statuses
                charge = EVENT_RESERVE_BYTES + (STATUS_BYTES if new else 0)
                if not self._room(charge, bucket) or not self._precharge(charge):
                    self._missed()
                    return False
                payload = entry = row = None
                sequence = self._sum(self._sequence, 1)
                slot = self._slot()
                try:
                    payload = _encode(record, sequence)
                    if len(payload) > MAX_EVENT_BYTES:
                        raise ValueError("audit event exceeds bound")
                    event_charge = _event_charge(len(payload))
                    row = _Status() if new else self._statuses[bucket]
                    entry = _Entry(payload, bucket, None, event_charge, sequence)
                    if new:
                        self._statuses[bucket] = row
                    self._events[slot] = entry
                except (MemoryError, ValueError):
                    self._events.pop(slot, None)
                    if new:
                        self._statuses.pop(bucket, None)
                    payload = entry = row = None
                    self._retained -= charge
                    self._missed()
                    return False
                self._count(row, record)
                self._sequence = sequence
                # Encoding temporaries are ended; canonical bytes only remain.
                self._retained -= EVENT_RESERVE_BYTES - event_charge
                return True
            finally:
                self._pending = False

    def reserve_exception(self, reference: ExceptionReference) -> Reservation | None:
        with self._lock:
            if self._closed or type(reference) is not ExceptionReference:
                return None
            self._pending = True
            try:
                # Reference was generated internally, never converted from a client
                # label. Its immutable scalar members are copied into owned status.
                key = (reference._id, reference._revision, reference._scope)
                new = key not in self._statuses
                charge = EVENT_RESERVE_BYTES + RESERVATION_BYTES + (STATUS_BYTES if new else 0)
                if not self._room(charge, key) or not self._precharge(charge):
                    self._missed()
                    return None
                sequence = self._sum(self._sequence, 1)
                handle = row = entry = None
                slot = self._slot()
                try:
                    handle = Reservation(self, slot)
                    row = _Status() if new else self._statuses[key]
                    entry = _Entry(None, key, handle, EVENT_RESERVE_BYTES + RESERVATION_BYTES, sequence)
                    if new:
                        self._statuses[key] = row
                    self._events[slot] = entry
                except MemoryError:
                    self._events.pop(slot, None)
                    if new:
                        self._statuses.pop(key, None)
                    if handle is not None:
                        handle._owner = None
                    handle = entry = row = None
                    self._retained -= charge
                    self._missed()
                    return None
                row.active += 1
                self._active += 1
                self._sequence = sequence
                return handle
            finally:
                self._pending = False

    def reserve_snapshot(self) -> SnapshotWork | None:
        with self._lock:
            if self._closed or self._snapshot is not None:
                return None
            self._pending = True
            try:
                while self._retained + SNAPSHOT_WORK_BYTES > MAX_BYTES:
                    slot = next((s for s, entry in self._events.items() if entry.reservation is None), None)
                    if slot is None:
                        self._missed()
                        return None
                    self._discard(slot)
                if not self._precharge(SNAPSHOT_WORK_BYTES):
                    self._missed()
                    return None
                try:
                    work = SnapshotWork(self)
                except MemoryError:
                    self._retained -= SNAPSHOT_WORK_BYTES
                    self._missed()
                    return None
                self._snapshot = work
                return work
            finally:
                self._pending = False

    def finalize(self, reservation: Reservation, record: SafeDecisionRecord) -> bool:
        with self._lock:
            if type(reservation) is not Reservation or reservation._owner is not self or not _valid(record):
                return False
            entry = self._events[reservation._sequence]
            if self._closed:
                self._finish(reservation, entry, None)
                return True
            try:
                payload = _encode(record, entry.sequence)
                if len(payload) > MAX_EVENT_BYTES:
                    return False
            except MemoryError:
                return False
            row = self._statuses[entry.status]
            self._count(row, record)
            self._finish(reservation, entry, payload)
            return True

    def abandon(self, reservation: Reservation) -> bool:
        with self._lock:
            if type(reservation) is not Reservation or reservation._owner is not self:
                return False
            entry = self._events[reservation._sequence]
            self._finish(reservation, entry, None)
            return True

    def _finish(self, reservation: Reservation, entry: _Entry, payload: bytes | None) -> None:
        sequence = reservation._sequence
        row = self._statuses[entry.status]
        row.active -= 1
        self._active -= 1
        reservation._owner, reservation._sequence = None, 0
        entry.reservation = None
        if payload is None or self._closed:
            # caller still holds entry/row during this function: release after
            # clearing their retained contents, not while live private bytes
            # or a reservation still refer into this owner.
            entry.payload = None
            charge = entry.charge
            self._events.pop(sequence)
            entry.charge = 0
            self._compact(charge)
        else:
            charge = _event_charge(len(payload))
            entry.payload = payload
            old = entry.charge
            entry.charge = charge
            self._retained -= old - charge
        if self._closed:
            self._ended()

    def _ended(self) -> None:
        # No whole-container tuple remains aliased as individual charges end.
        while (slot := next((s for s, e in self._events.items() if e.reservation is None), None)) is not None:
            self._discard(slot)
        while (
            key := next(
                (
                    k
                    for k, row in self._statuses.items()
                    if not row.active and (self._projection is None or k != self._projection._bucket)
                ),
                None,
            )
        ) is not None:
            del self._statuses[key]
            self._compact(STATUS_BYTES)
        if not self._active and self._snapshot is None and self._projection is None and not self._sweep:
            self._ceiling = None
            self._status_owner = None
            # Drop actual empty container capacity before ending the base.
            self._events = OrderedDict()
            self._statuses = OrderedDict()
            self._retired = 0
            self._retained = 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ended()


SNAPSHOT_WORK_BYTES = 768 * 1024
MAX_SNAPSHOT_BYTES = 32768


def _status_shape(value: object) -> bool:
    """Validate local status and an optional finite unavailable aggregate."""
    if type(value) is not dict or set(value) != {"schema", "local", "server"}:
        return False
    if type(value["schema"]) is not int or value["schema"] != 1:
        return False
    server = value["server"]
    if server is not None:
        if type(server) is not dict or set(server) != {
            "available",
            "complete",
            "contributors",
            "counts",
            "overflow",
            "saturated",
            "published",
            "removed_headers",
        }:
            return False
        if any(type(server[key]) is not bool for key in ("available", "complete", "saturated", "published")):
            return False
        if not server["available"]:
            if any(server[key] for key in ("complete", "saturated")):
                return False
            if type(server["contributors"]) is not int or server["contributors"] != 0:
                return False
            if any(server[key] is not None for key in ("counts", "overflow", "removed_headers")):
                return False
        else:
            if not _integer(server["contributors"], 64, 1):
                return False
            if not _integer(server["overflow"], MAX_COUNTER) or not _integer(server["removed_headers"], MAX_COUNTER):
                return False
            checked = {
                "epoch": "0" * 32,
                "sequence": 0,
                "counts": server["counts"],
                "overflow": server["overflow"],
                "partial": not server["complete"],
                "reset": True,
                "removed_headers": server["removed_headers"],
                "saturated": server["saturated"],
            }
            if not _status_shape({"schema": 1, "local": checked, "server": None}):
                return False
            if server["complete"] and not server["published"]:
                return False
    local = value["local"]
    if type(local) is not dict or set(local) != {
        "epoch",
        "sequence",
        "counts",
        "overflow",
        "partial",
        "reset",
        "removed_headers",
        "saturated",
    }:
        return False
    epoch = local["epoch"]
    if type(epoch) is not str or len(epoch) != 32 or any(c not in "0123456789abcdef" for c in epoch):
        return False
    if any(not _integer(local[key], MAX_COUNTER) for key in ("sequence", "overflow", "removed_headers")):
        return False
    if any(type(local[key]) is not bool for key in ("partial", "reset", "saturated")):
        return False
    rows = local["counts"]
    if type(rows) is not list or len(rows) > EXCEPTION_SLOTS + BUCKETS:
        return False
    seen = set()
    exceptions = builtins = 0
    for row in rows:
        if type(row) is not list or len(row) != 7 or any(not _integer(v, MAX_COUNTER) for v in row[1:]):
            return False
        key, revision, scope = row[:3]
        if type(key) is int:
            if not _integer(key, BUCKETS - 1) or revision != 0 or scope != 0:
                return False
            builtins += 1
        elif type(key) is str:
            if len(key) != 32 or any(c not in "0123456789abcdef" for c in key) or revision < 1 or not 1 <= scope <= 7:
                return False
            exceptions += 1
        else:
            return False
        identity = (key, revision, scope)
        if identity in seen:
            return False
        seen.add(identity)
    return exceptions <= EXCEPTION_SLOTS and builtins <= BUCKETS


class SnapshotWork:
    """One precharged read/publication work lease, never detached raw results.

    encoded/decode/output are borrowed while this lease lives. Pass the lease
    itself across a queue/consumer boundary, and end all borrows before close.
    Native terminal N and actual queued/call input Q are independent claims.
    Consumer close refunds only after BOTH end, including submit exceptions.
    """

    __slots__ = (
        "__weakref__",
        "_body",
        "_d2_counts",
        "_d2_decoded",
        "_d2_directory",
        "_d2_entries",
        "_d2_payload",
        "_d2_published",
        "_d2_raw",
        "_d2_transport",
        "_decoded",
        "_encoded",
        "_ending",
        "_native",
        "_output",
        "_owner",
        "_projection",
        "_queued",
        "_response",
        "_submission",
    )

    def __init__(self, owner: RequestAudit) -> None:
        self._owner: RequestAudit | None = owner
        self._projection: dict | None = None
        self._encoded: bytes | None = None
        self._decoded: dict | None = None
        self._output: bytes | None = None
        self._native = self._queued = self._ending = False
        self._body: bytes | None = None
        self._submission: object | None = None
        self._response: object | None = None
        self._d2_transport: ValkeyRequestStatus | None = None
        self._d2_payload = self._d2_directory = self._d2_entries = None
        self._d2_raw = self._d2_decoded = self._d2_counts = None
        self._d2_published = False

    @property
    def encoded(self) -> bytes | None:
        return self._encoded

    @property
    def output(self) -> bytes | None:
        return self._output

    def build(self, *, removed_headers: int = 0, server: bool = False) -> bool:
        owner = self._owner
        if owner is None:
            return False
        with owner._lock:
            if (
                self._ending
                or owner._closed
                or self._projection is not None
                or not _integer(removed_headers, MAX_COUNTER)
                or type(server) is not bool
            ):
                return False
            # All projection/encoding buffers are inside the full work charge,
            # not the conditional old encoded-only snapshot proposal.
            try:
                rows = []
                for key, status in owner._statuses.items():
                    code, revision, scope = key if type(key) is tuple else (key, 0, 0)
                    rows.append([code, revision, scope, status.uses, status.attempts, status.answers, status.active])
                self._projection = {
                    "schema": 1,
                    "local": {
                        "epoch": owner._epoch,
                        "sequence": owner._sequence,
                        "counts": rows,
                        "overflow": owner._overflow,
                        "partial": owner._partial,
                        "reset": True,
                        "removed_headers": removed_headers,
                        "saturated": owner._saturated,
                    },
                    "server": {
                        "available": False,
                        "complete": False,
                        "contributors": 0,
                        "counts": None,
                        "overflow": None,
                        "saturated": False,
                        "published": False,
                        "removed_headers": None,
                    }
                    if server
                    else None,
                }
                if not _status_shape(self._projection):
                    return False
                encoded = json.dumps(self._projection, separators=(",", ":"), ensure_ascii=True).encode("ascii")
                if len(encoded) > MAX_SNAPSHOT_BYTES:
                    return False
                self._encoded = encoded
                return True
            except (MemoryError, ValueError):
                return False

    def decode(self) -> dict | None:
        owner = self._owner
        if owner is None:
            return None
        with owner._lock:
            if self._ending or self._encoded is None:
                return None
            if self._decoded is None:
                try:
                    self._decoded = json.loads(self._encoded)
                except (MemoryError, ValueError):
                    return None
            return self._decoded

    def encode_output(self, value: object) -> bool:
        owner = self._owner
        if owner is None:
            return False
        with owner._lock:
            if self._ending or (value is not self._projection and value is not self._decoded):
                return False
            if self._output is not None:
                return True
            if not _status_shape(value):
                return False
            try:
                encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
                if len(encoded) > MAX_SNAPSHOT_BYTES:
                    return False
                self._output = encoded
                return True
            except (MemoryError, ValueError):
                return False

    def _server_unavailable(self, published: bool) -> bool:
        if type(published) is not bool:
            return False
        return self._server_projection(
            {
                "available": False,
                "complete": False,
                "contributors": 0,
                "counts": None,
                "overflow": None,
                "saturated": False,
                "published": published,
                "removed_headers": None,
            }
        )

    def _server_projection(self, server: dict) -> bool:
        owner = self._owner
        if owner is None:
            return False
        with owner._lock:
            if self._ending or self._projection is None:
                return False
            previous = self._projection["server"]
            self._projection["server"] = server
            try:
                # Combined output omits only explicitly counted server rows.
                for _ in range(EXCEPTION_SLOTS + 1):
                    if not _status_shape(self._projection):
                        break
                    encoded = json.dumps(self._projection, separators=(",", ":"), ensure_ascii=True).encode("ascii")
                    if len(encoded) <= MAX_SNAPSHOT_BYTES:
                        self._encoded = encoded
                        self._decoded = self._output = None
                        return True
                    rows = server["counts"]
                    if not rows or type(rows[-1][0]) is not str:
                        break
                    rows.pop()
                    server["complete"] = False
                    if server["overflow"] == MAX_COUNTER:
                        server["saturated"] = True
                    else:
                        server["overflow"] += 1
            except (MemoryError, ValueError):
                pass
            self._projection["server"] = previous
            return False

    def _clear_projection(self) -> None:
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            self._projection = self._encoded = self._decoded = self._output = None

    @property
    def _worker(self) -> bool:
        return self._native or self._queued

    def claim_worker(self, *, queued: bool = False) -> bool:
        owner = self._owner
        if owner is None or type(queued) is not bool:
            return False
        with owner._lock:
            if owner._closed or self._ending or self._worker:
                return False
            self._native, self._queued = True, queued
            return True

    def worker_finished(self) -> None:
        """End N only; Future terminal never proves queued input disposal."""
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            if not self._native:
                return
            self._native = False
            if self._ending and not self._queued:
                self._finish(owner)

    def queue_finished(self) -> None:
        """Exact disarmed carrier's final transition; all inputs already gone."""
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            if not self._queued:
                return
            self._queued = False
            if self._ending and not self._native:
                self._finish(owner)

    def _publication_disposed(self) -> None:
        """Publication-only C/N/Q end at ACTUAL disarmed input disposal.

        No Future callback/terminal proxy. The exact carrier has already cleared
        its context/mediator/inputs outside A; only empty shells may outlive this.
        """
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            if not self._queued:
                return
            self._native = self._queued = False
            self._ending = True
            self._finish(owner)

    def close(self) -> None:
        owner = self._owner
        if owner is None:
            return
        with owner._lock:
            self._ending = True
            if not self._worker:
                self._finish(owner)

    def _finish(self, owner: RequestAudit) -> None:
        # Native Future terminal/consumer finalization has ended all borrows.
        # Clear every owned stage before its charge can disappear.
        self._projection = self._encoded = self._decoded = self._output = None
        self._body = self._submission = self._response = None
        if self._d2_transport is not None:
            self._d2_transport._end()  # exact internal transport field clearing, never I/O
        self._d2_transport = self._d2_payload = self._d2_directory = self._d2_entries = None
        self._d2_raw = self._d2_decoded = self._d2_counts = None
        self._owner = None
        owner._snapshot = None
        owner._retained -= SNAPSHOT_WORK_BYTES
        if owner._closed:
            owner._ended()


# D2 uses only numeric field positions and generated IDs. Row deadline is
# authoritative Valkey TIME metadata, never a Python/process clock.
_D2_DEADLINE = 9_999_999_999_999
_D2_ROW = 32768
_D2_PAYLOAD = 44 + 33 * 8 + 57 * 128  # binaryv2 structural max7604; normative row cap unchanged
_D2_DIRECTORY = 64 * 22


def _d2_encode(local: object) -> bytes | None:
    if not _status_shape({"schema": 1, "local": local, "server": None}):
        return None
    local = cast("_LocalStatus", local)
    if local["reset"] is not True:
        return None
    try:
        rows = local["counts"]
        builtins = sum(type(row[0]) is int for row in rows)
        exceptions = len(rows) - builtins
        out = bytearray(44 + 33 * builtins + 57 * exceptions)
        out[:4] = bytes((2, int(local["partial"]) | (int(local["saturated"]) << 1), builtins, exceptions))
        out[4:20] = bytes.fromhex(local["epoch"])
        at = 20
        for value in (local["sequence"], local["overflow"], local["removed_headers"]):
            out[at : at + 8] = value.to_bytes(8, "big")
            at += 8
        for row in rows:  # preserve original row order, including mixed classes
            if type(row[0]) is int:
                out[at] = cast("int", row[0])
                at += 1
            else:
                out[at] = 0x80 | cast("int", row[2])
                out[at + 1 : at + 17] = bytes.fromhex(cast("str", row[0]))
                out[at + 17 : at + 25] = cast("int", row[1]).to_bytes(8, "big")
                at += 25
            for value in row[3:]:
                out[at : at + 8] = cast("int", value).to_bytes(8, "big")
                at += 8
        return bytes(out)  # bytearray+bytes overlap belongs to same Work charge
    except (MemoryError, ValueError, OverflowError):
        return None


def _d2_decode(raw: object) -> tuple[int, dict] | None:
    # Structural reachability is narrower than the unchanged32768 ceiling.
    if type(raw) is not bytes or not 58 <= len(raw) <= _D2_PAYLOAD + 14 or raw[13:14] != b"\n":
        return None
    prefix = raw[:13]
    if any(byte < 48 or byte > 57 for byte in prefix):
        return None
    deadline = int(prefix)
    if deadline > _D2_DEADLINE:
        return None
    view = None
    try:
        view = memoryview(raw)  # no full payload slice/JSON/canonical re-encoding
        flags, builtins, exceptions = view[15], view[16], view[17]
        if view[14] != 2 or flags > 3 or builtins > 8 or exceptions > 128:
            return None
        if len(raw) != 14 + 44 + 33 * builtins + 57 * exceptions:
            return None
        if any(view[at] >= 128 for at in (34, 42, 50)):
            return None
        local = {
            "epoch": view[18:34].hex(),
            "sequence": int.from_bytes(view[34:42], "big"),
            "overflow": int.from_bytes(view[42:50], "big"),
            "removed_headers": int.from_bytes(view[50:58], "big"),
            "partial": bool(flags & 1),
            "saturated": bool(flags & 2),
            "reset": True,
            "counts": [],
        }
        at = 58
        found_builtins = found_exceptions = 0
        for _ in range(builtins + exceptions):
            if at >= len(raw):
                return None
            tag = view[at]
            if tag <= 7:
                if at + 33 > len(raw):
                    return None
                row = [tag, 0, 0]
                found_builtins += 1
                at += 1
            elif 0x81 <= tag <= 0x87:
                if at + 57 > len(raw) or view[at + 17] >= 128:
                    return None
                revision = int.from_bytes(view[at + 17 : at + 25], "big")
                if not revision:
                    return None
                row = [view[at + 1 : at + 17].hex(), revision, tag - 0x80]
                found_exceptions += 1
                at += 25
            else:
                return None
            for _ in range(4):
                if view[at] >= 128:
                    return None
                row.append(int.from_bytes(view[at : at + 8], "big"))
                at += 8
            local["counts"].append(row)
        if at != len(raw) or found_builtins != builtins or found_exceptions != exceptions:
            return None
        # Existing finite validator also rejects exact duplicate rows. Same ID
        # with distinct scope/revision remains legal; counters stay independent.
        return (deadline, local) if _status_shape({"schema": 1, "local": local, "server": None}) else None
    except (MemoryError, ValueError, TypeError, IndexError):
        return None
    finally:
        if view is not None:
            view.release()  # no view/raw alias is returned or parked


def _d2_directory(raw: object) -> list[tuple[str, int]] | None:
    if type(raw) is not bytes or len(raw) > _D2_DIRECTORY or len(raw) % 22:
        return None
    out = []
    previous = b""
    for at in range(0, len(raw), 22):
        instance, deadline = raw[at : at + 12], raw[at + 12 : at + 22]
        if (
            instance <= previous
            or any(byte not in b"0123456789abcdef" for byte in instance)
            or any(byte not in b"0123456789abcdefghijklmnopqrstuvwxyz" for byte in deadline)
        ):
            return None
        stamp = int(deadline, 36)
        if stamp > _D2_DEADLINE:
            return None
        out.append((instance.decode("ascii"), stamp))
        previous = instance
    return out


class _D2Counts:
    """A bounded reporting fold; Python integers never traverse Lua doubles."""

    __slots__ = ("_exceptions", "_overflow", "_own", "_partial", "_removed_headers", "_rows", "_saturated", "_seen")

    def __init__(self, instance: str, local: dict) -> None:
        if type(instance) is not str or len(instance) != 12 or any(c not in "0123456789abcdef" for c in instance):
            raise ValueError("invalid request status instance")
        if not _status_shape({"schema": 1, "local": local, "server": None}):
            raise ValueError("invalid request status counts")
        self._own = instance
        self._seen = {instance}
        self._rows: dict[tuple, list] = {}
        self._exceptions = self._overflow = self._removed_headers = 0
        self._partial = self._saturated = False
        self._fold(local)

    def _sum(self, left: int, right: int) -> int:
        if left > MAX_COUNTER - right:
            self._saturated = True
            return MAX_COUNTER
        return left + right

    def incomplete(self, omitted: int = 0) -> None:
        self._partial = True
        self._overflow = self._sum(self._overflow, omitted)

    def _fold(self, local: dict) -> None:
        self._overflow = self._sum(self._overflow, local["overflow"])
        self._removed_headers = self._sum(self._removed_headers, local["removed_headers"])
        self._partial |= local["partial"] or local["overflow"] > 0
        self._saturated |= local["saturated"]
        for row in local["counts"]:
            key = tuple(row[:3])
            previous = self._rows.get(key)
            if previous is None:
                if type(row[0]) is str and self._exceptions >= EXCEPTION_SLOTS:
                    self._overflow = self._sum(self._overflow, 1)
                    self._partial = True
                    continue
                previous = list(row)
                self._rows[key] = previous
                self._exceptions += type(row[0]) is str
            else:
                for n in range(3, 7):
                    previous[n] = self._sum(previous[n], row[n])

    def add(self, instance: str, local: dict) -> bool:
        if type(instance) is not str or len(instance) != 12 or any(c not in "0123456789abcdef" for c in instance):
            self._overflow = self._sum(self._overflow, 1)
            self._partial = True
            return False
        if instance == self._own:
            return False  # own remote epoch/totals are replaced by current local
        if (
            instance in self._seen
            or len(self._seen) >= 64
            or not _status_shape({"schema": 1, "local": local, "server": None})
        ):
            self._overflow = self._sum(self._overflow, 1)
            self._partial = True
            return False
        self._seen.add(instance)
        self._fold(local)
        return True

    @property
    def removed_headers(self) -> int:
        return self._removed_headers

    def result(self, *, published: bool, complete: bool) -> dict:
        rows = sorted(self._rows.values(), key=lambda row: (type(row[0]) is str, *row[:3]))
        return {
            "available": True,
            "complete": bool(complete and published and not self._partial),
            "contributors": len(self._seen),
            "counts": rows,
            "overflow": self._overflow,
            "saturated": self._saturated,
            "published": published,
            "removed_headers": self._removed_headers,
        }


# Closed reporting catalogs, not policy/transport authority.
_METHOD_CODES = {"GET": 1, "POST": 2, "PUT": 3, "PATCH": 4, "DELETE": 5, "HEAD": 6, "OPTIONS": 7, "CONNECT": 8}
_PROTOCOL_CODES = {"anthropic": 1, "chat": 2, "responses": 3, "gemini": 4, "ws-cancel": 6}
_PROFILE_CODES = {"anthropic.body.1": 1, "chat.body.1": 2, "responses.body.1": 3, "gemini.body.1": 4}
_RULE_CODES = {
    "checked only": 1,
    "image remainder after text recognition": 2,
    "sent before in this conversation": 3,
    "reported as written by the model": 4,
    "observed opaque provider echo (1)": 5,
    "provider encrypted field (1)": 6,
}
# The category of every request exception's rule (``body_selectors.exception_rule``, "exception <id>") and of an
# envelope's exception: one code for all, never the exception's id. 7 named Phase 1's internal rule, deleted by P2-6:
# retired, never written again, so that an old record's 7 still means only that rule.
_EXCEPTION_RULE = 8
_RETIRED = 7
_REASON_CODES = {
    "refused": 1,
    "too-large": 2,
    "coverage-limit": 2,
    "proof-incomplete": 3,
    "proof-foreign": 3,
    "evidence": 4,
    "no-route": 5,
    "unread": 5,
    "no-signing-route": 5,
    "envelope-route": 5,
    "envelope-profile": 5,
    "credentials": 6,
    "envelope-credential": 6,
    "egress-refused": 7,
    "conversation-unread": 8,
    "busy": 9,
    "envelope-escape": 10,
    "envelope-encoding": 10,
    "envelope-control": 10,
    "envelope-path": 10,
    "envelope-header-unknown": 11,
    "envelope-exception-expired": 11,
    "envelope-header-duplicate": 11,
    "envelope-header-syntax": 11,
    "envelope-header-value": 11,
    "envelope-query-unknown": 12,
    "envelope-query-duplicate": 12,
    "envelope-query-value": 12,
    "envelope-state-unavailable": 13,
    "envelope-binding": 14,
    "envelope-region": 14,
    "envelope-location": 14,
    "envelope-resource": 14,
    "envelope-sensitive": 14,
    "masking-failed": 15,
    "envelope-bodyless": 16,
    "body-unknown-member": 17,
    "body-unknown-member-no-exception": 18,
    "settings-changed": 19,
}


def reason_code(code: object) -> int:
    return _REASON_CODES.get(code, 31) if type(code) is str and len(code) <= 64 else 31


def _profile_code(profile: object) -> int:
    return _PROFILE_CODES.get(profile, 0) if type(profile) is str and len(profile) <= 32 else 0


def method_code(method: str) -> int:
    return _METHOD_CODES.get(method, 0) if type(method) is str and len(method) <= 7 else 0


def route_code(upstream: object) -> int:
    for index, fixed in enumerate(DEFAULT_UPSTREAMS, 1):
        if upstream is fixed:
            return index
    if type(upstream) is BedrockUpstream:
        return 6
    if type(upstream) is VertexUpstream:
        return 7
    # A plain Upstream has no configured-origin seal. Custom provenance cannot
    # be established here without a new lookup/route field: report unknown.
    return 0


def _decision_bucket(decision: object) -> int:
    if type(decision) is Forward:
        return 1
    if type(decision) is Pass and type(decision.evidence) is PolicyEvidence:
        return 2
    return 3 if type(decision) is Block else 0


def decision_record(
    scope: _ProjectionScope,
    decision: object,
    *,
    source: int,
    method: int,
    route: int,
    attempts: int | None,
    answered: int | None,
    status: int | None,
    reason: int | None = None,
) -> SafeDecisionRecord | None:
    """Finite scalar projection ONLY inside positive prepaid scope, outside A.

    Exact cached classes/fields only: never proves(), policy/source callbacks,
    arbitrary getattr/iterators, body/treatment/receipt/session/content walks.
    """
    if type(scope) is not _ProjectionScope or scope._owner is None:
        return None
    protocol = profile = revision = coverage = 0
    rules = ()
    if type(decision) is Block:
        outcome = 3
        reason = reason_code(decision.code)
    elif type(decision) is Forward:
        if type(decision.proof) not in (BodyProof, EmptyBodyProof):
            return None
        outcome, coverage = 1, 1
        if type(decision.proof) is BodyProof:
            profile = _profile_code(decision.proof.profile)
        else:
            protocol = 5  # actual EmptyBodyProof, never None protocol alone
    elif type(decision) is Pass:
        outcome = 2
        evidence = decision.evidence
        if type(evidence) is PolicyEvidence:
            if type(evidence.rules) is not tuple or len(evidence.rules) > 192:
                return None
            mask = 0
            pairs = []
            excepted = ("exception",) if type(evidence.exceptions) is tuple and evidence.exceptions else ()
            for rule in (*evidence.rules, *excepted):
                if type(rule) is not str or len(rule) > 96:
                    return None  # bounded loss, never silently drop a used rule
                code = _RULE_CODES.get(rule) or (_EXCEPTION_RULE if rule.startswith("exception") else None)
                if code is None:
                    return None
                if not mask & (1 << code):
                    pairs.append((code, 1))
                    mask |= 1 << code
            rules = tuple(pairs)
            coverage = 1
            profile = _profile_code(evidence.profile)
        else:
            return None
    else:
        return None
    if (type(decision) is Forward or type(decision) is Pass) and type(decision.response) is ResponsePlan:
        cached = decision.response.protocol
        if protocol != 5 and type(cached) is str and len(cached) <= 16:
            protocol = _PROTOCOL_CODES.get(cached, 0)
    revision = 1 if profile else 0
    category = (
        reason
        if type(reason) is int and 0 <= reason <= 31
        else 0
        if reason is None and type(decision) is not Block
        else 31
    )
    return SafeDecisionRecord(
        source=source,
        outcome=outcome,
        coverage=coverage,
        protocol=protocol,
        method=method,
        route=route,
        profile=profile,
        revision=revision,
        reason=category,
        rules=rules,
        attempts=attempts,
        answered=answered,
        status=status,
    )


def _project_record(
    scope: _ProjectionScope,
    decision: object,
    *,
    source: int,
    method: int,
    route: int,
    attempts: int | None,
    answered: int | None,
    status: int | None,
    reason: int | None,
    advisory: bool = False,
    reason_present: bool = False,
) -> BaseException | None:
    """End projection frames/tracebacks OUTSIDE A before the emitter settles.

    Only a terminal interrupt crosses this helper, with no projection traceback.
    Ordinary observability failures stay best effort; caller cancellation and
    other BaseException interruptions propagate after exact owned cleanup.
    """
    record = None
    try:
        record = (
            SafeDecisionRecord(source=4, outcome=4, reason=31 if reason_present else 0)
            if advisory
            else decision_record(
                scope,
                decision,
                source=source,
                method=method,
                route=route,
                attempts=attempts,
                answered=answered,
                status=status,
                reason=reason,
            )
        )
        scope.commit(record)
    except BaseException as error:
        # commit accounts its own failures. A projection failure before commit
        # still gets ONE bounded loss. No exception/raw decision enters Audit.
        if not scope._attempted:
            scope.commit(None)
        BaseException.__setattr__(error, "__traceback__", None)
        BaseException.__setattr__(error, "__context__", None)
        BaseException.__setattr__(error, "__cause__", None)
        if not isinstance(error, Exception):
            return error
    finally:
        record = None
    return None


def emit_decision(
    owner: RequestAudit | None,
    decision: object,
    *,
    source: int,
    method: int = 0,
    route: int = 0,
    attempts: int | None = None,
    answered: int | None = None,
    status: int | None = None,
    reason: int | None = None,
) -> None:
    if type(owner) is not RequestAudit or type(decision) not in (Block, Forward, Pass):
        return  # no actual typed decision: SKIP, no synthetic denominator
    scope = owner._reserve_projection(_decision_bucket(decision))
    if scope is None:
        return
    interrupted = _project_record(
        scope,
        decision,
        source=source,
        method=method,
        route=route,
        attempts=attempts,
        answered=answered,
        status=status,
        reason=reason,
    )
    scope.close()  # helper/record/rules/encoder/exception frames have ENDED
    if interrupted is not None:
        raise interrupted


def emit_advisory(owner: RequestAudit | None, bucket: int, *, reason_present: bool) -> None:
    if type(owner) is not RequestAudit or bucket not in (4, 5, 6):
        return
    scope = owner._reserve_projection(bucket)
    if scope is None:
        return
    interrupted = _project_record(
        scope,
        None,
        source=4,
        method=0,
        route=0,
        attempts=None,
        answered=None,
        status=None,
        reason=None,
        advisory=True,
        reason_present=reason_present,
    )
    scope.close()
    if interrupted is not None:
        raise interrupted
