"""Computer-owned launch authority. Control transports borrow it; server authority is separate.

Charges are reserved owner envelopes, not process/RSS measurements. No credential is retained in
an entry, ticket, hold or authority receipt. Only the short authenticated open response carries it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

if TYPE_CHECKING:
    from starlette.types import Scope

IDENTITY_LIVE = 64
BINDINGS = 1024
ENTRIES = 4096
HOLDS = 256
ENTRY_BYTES = 1024
BINDING_BYTES = 16384
HOLD_BYTES = 65536
DIRECTORY_BYTES = 65536
FOLDER_BYTES = 4096
IDENTIFIER_BYTES = 1024
PENDING_SECONDS = 60.0
# How long an ended launch whose child's exit was never confirmed (its launcher killed: a closed terminal) is kept, its
# key refused, before it is forgotten; a full registry forgets its oldest first. A forgotten key is decided as claimed,
# which only tightens (the review's Minor 1 on P2-6a).
TOMBSTONE_SECONDS = 24 * 3600.0
MAX_GENERATION = (1 << 64) - 1
_KEY = re.compile(r"latet_lk_[A-Za-z0-9_-]{43}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"[0-9a-f]{12}\Z")
_CONTENT_KEY = re.compile(rb"latet_lk_[A-Za-z0-9_-]{43}")


def canonical_launch_key(key: str) -> bool:
    if type(key) is not str or _KEY.fullmatch(key) is None:
        return False
    raw = base64.urlsafe_b64decode(key[9:] + "=")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == key[9:]


class LaunchRefused(ValueError):
    """Fixed, value-free control failure; caller-selected text never becomes its words."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class Binding:
    folder: bytes
    root: bytes
    project: str

    def __post_init__(self) -> None:
        if any(type(value) is not bytes or not 0 < len(value) <= FOLDER_BYTES for value in (self.folder, self.root)):
            raise LaunchRefused("launch-invalid")
        if type(self.project) is not str or _PROJECT.fullmatch(self.project) is None:
            raise LaunchRefused("launch-invalid")
        if any(_CONTENT_KEY.search(value) is not None for value in (self.folder, self.root)):
            raise LaunchRefused("launch-invalid")  # control metadata is never another credential carrier


@dataclass(frozen=True, slots=True, repr=False)
class Authority:
    epoch: str
    launch_id: str
    identity: bytes
    generation: int
    project: str
    binding: bytes
    tag: bytes


@dataclass(frozen=True, slots=True, repr=False)
class Check:
    code: str
    authority: Authority | None = None


@dataclass(slots=True, repr=False)
class _Entry:
    launch_id: str
    identity: bytes
    locator: bytes = b""
    keyed: bytes = b""
    state: str = "opening"
    generation: int = 0
    deadline: float = 0.0
    binding: Binding | None = None
    reserved: bool = True
    physical: int = 0
    mutation: object | None = None
    confirmed: bool = False
    unpublished: bool = False
    ended: float = 0.0  # when it ended, by the registry's clock
    recovered: bool = False  # registered again by its launcher after a restart, its agent running meanwhile


@dataclass(slots=True, repr=False)
class Mutation:
    _owner: LocalLaunchKeys | None
    _entry: _Entry | None
    _marker: object
    _epoch: str
    _retirement: int


class RecoveryFence(TypedDict):
    epoch: str
    retirement: int


class LaunchPrepare(Protocol):
    def prepare(self) -> RecoveryFence: ...


class HeldLaunch(Protocol):
    @property
    def generation(self) -> int: ...

    def acknowledge(self) -> int: ...

    async def wait(self) -> None: ...

    def stop(self) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class StartingHold(HeldLaunch, Protocol):
    async def ready(self, generation: int) -> None: ...


class LaunchControls(Protocol):
    """The bridge's launch routes, one implementation for each machine injected as the bridge is built (R-114): a
    server's protected controls, or a computer's over its own launch keys (``bridge.launch_controls``). ``read`` reads
    the control's JSON body with exactly the given fields, when the implementation is ready for it."""

    @property
    def fields(self) -> frozenset[str]: ...

    async def open(
        self, scope: Scope, read: Callable[[frozenset[str]], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]: ...

    @property
    def recovery_fields(self) -> frozenset[str]: ...

    async def recover(
        self, scope: Scope, read: Callable[[frozenset[str]], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]: ...

    def hold(self, scope: Scope, launch_id: str, generation: int) -> StartingHold: ...

    async def end(self, scope: Scope, launch_id: str, *, exit_epoch: str | None = None) -> bool: ...


class Hold:
    __slots__ = ("_ack", "_base", "_entry", "_marker", "_owner", "_pending", "_signalled", "_wake", "generation")

    def __init__(self, owner: LocalLaunchKeys, entry: _Entry, marker: object) -> None:
        self._owner: LocalLaunchKeys | None = owner
        self._entry: _Entry | None = entry
        self._marker = marker
        self._base, self.generation = entry.generation, entry.generation + 1
        self._pending, self._ack = entry.state == "pending", False
        self._signalled = False
        self._wake: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    def acknowledge(self) -> int:
        owner = self._owner
        if owner is None:
            raise LaunchRefused("launch-ended")
        return owner._acknowledge(self)

    def _signal(self) -> None:
        if self._signalled:
            return
        self._signalled = True
        if self._wake is not None:
            loop, event = self._wake
            if not loop.is_closed():
                # Its loop may close meanwhile; detach still owns the charge.
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(event.set)

    async def wait(self) -> None:
        event = asyncio.Event()
        self._wake = asyncio.get_running_loop(), event
        owner = self._owner
        if owner is None or not owner._held(self):
            event.set()
        try:
            await event.wait()
        finally:
            self._wake = None

    async def drain(self) -> None:
        """The local hold has no independent backend work to drain."""

    async def ready(self, generation: int) -> None:
        """The local hold is ready as it is reserved: nothing to wait for."""

    def close(self) -> None:
        owner = self._owner
        if owner is not None:
            owner._detach(self)

    def stop(self) -> None:
        owner = self._owner
        if owner is not None:
            owner._stop(self)


class LocalLaunchKeys:
    __slots__ = (
        "_binding_key",
        "_bindings",
        "_clock",
        "_closed",
        "_entries",
        "_epoch",
        "_holds",
        "_identities",
        "_locator_key",
        "_locators",
        "_lock",
        "_retirement",
        "ceiling",
    )

    def __init__(self, root_key: bytes, *, clock: Callable[[], float] = time.monotonic) -> None:
        if type(root_key) is not bytes or len(root_key) != 32:
            raise LaunchRefused("launch-invalid")
        self._binding_key = hmac.digest(root_key, b"latet-launch-binding-v1\0", "sha256")
        self._locator_key = hmac.digest(root_key, b"latet-launch-locator-v1\0", "sha256")
        self._epoch, self._retirement = secrets.token_bytes(16).hex(), 0
        self._clock, self._lock = clock, threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._locators: dict[bytes, _Entry] = {}
        self._identities: dict[bytes, int] = {}
        self._bindings = 0
        self._holds: set[Hold] = set()
        self._closed = False
        self.ceiling: Callable[[int], str | None] | None = None

    def __repr__(self) -> str:
        return "LocalLaunchKeys()"

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            if self._closed and not self._entries and not self._holds:
                return 0
            return (
                DIRECTORY_BYTES
                + len(self._entries) * ENTRY_BYTES
                + self._bindings * BINDING_BYTES
                + len(self._holds) * HOLD_BYTES
            )

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            self._prune()
            return {"entries": len(self._entries), "bindings": self._bindings, "holds": len(self._holds)}

    def _identity(self, identity: str) -> bytes:
        if (
            type(identity) is not str
            or not identity
            or len(identity) > IDENTIFIER_BYTES
            or len(identity.encode("utf-8")) > IDENTIFIER_BYTES
        ):
            raise LaunchRefused("launch-invalid")
        return hmac.digest(self._binding_key, b"identity\0" + identity.encode("utf-8"), "sha256")

    def _credential(self, key: str, identity: bytes) -> tuple[bytes, bytes]:
        if not canonical_launch_key(key):
            raise LaunchRefused("launch-invalid")
        encoded = key.encode("ascii")
        return (
            hmac.digest(self._locator_key, encoded, "sha256"),
            hmac.digest(self._binding_key, identity + encoded, "sha256"),
        )

    def _charge(self, added: int) -> None:
        if self._closed:
            raise LaunchRefused("launch-unavailable")
        if self.ceiling is not None and self.ceiling(added) is not None:
            raise LaunchRefused("launch-capacity")

    def _reserve(self, launch_id: str, identity: bytes) -> _Entry:
        if len(self._entries) >= ENTRIES:
            self._forget_oldest()
        if (
            len(self._entries) >= ENTRIES
            or self._bindings >= BINDINGS
            or self._identities.get(identity, 0) >= IDENTITY_LIVE
        ):
            raise LaunchRefused("launch-capacity")
        if launch_id in self._entries:
            raise LaunchRefused("launch-mismatch")
        self._charge(ENTRY_BYTES + BINDING_BYTES)
        entry = _Entry(launch_id, identity)
        previous = self._identities.get(identity, 0)
        try:
            self._identities[identity] = previous + 1
            self._entries[launch_id] = entry
        except BaseException:
            self._entries.pop(launch_id, None)
            if previous:
                self._identities[identity] = previous
            else:
                self._identities.pop(identity, None)
            raise
        self._bindings += 1
        return entry

    def _unreserve(self, entry: _Entry) -> None:
        if entry.reserved:
            entry.reserved = False
            entry.binding = None
            self._bindings -= 1
            count = self._identities[entry.identity] - 1
            if count:
                self._identities[entry.identity] = count
            else:
                del self._identities[entry.identity]

    def _cleanup(self, entry: _Entry) -> None:
        if self._entries.get(entry.launch_id) is not entry:
            return
        if entry.physical or entry.mutation is not None:
            return
        if entry.state == "ended" or self._closed:
            self._unreserve(entry)
        retire = entry.confirmed and self._retirement < MAX_GENERATION
        if retire or entry.unpublished or self._closed:
            if retire:
                # The final identifying deletion and its global recovery fence are one transition.
                self._retirement += 1
            self._entries.pop(entry.launch_id, None)
            if entry.locator:
                self._locators.pop(entry.locator, None)
            if len(self._entries) % 128 == 0:
                # Deleted dict slots do not become uncharged retained directory capacity.
                self._entries = dict(self._entries)
                self._locators = dict(self._locators)
                self._identities = dict(self._identities)
        if self._closed and not self._entries:
            self._entries, self._locators, self._identities = {}, {}, {}
            self._holds = set()
            self._binding_key = self._locator_key = b""

    def _forgettable(self, entry: _Entry) -> bool:
        return (
            entry.state == "ended"
            and not entry.physical
            and entry.mutation is None
            and not entry.reserved
            and self._entries.get(entry.launch_id) is entry
        )

    def _forget(self, entry: _Entry) -> None:
        """A tombstone forgotten: its record and its locator gone, as a confirmed exit's are, behind the same recovery
        fence; a request with its key is then decided as claimed."""
        if self._retirement < MAX_GENERATION:
            self._retirement += 1
        self._entries.pop(entry.launch_id, None)
        if entry.locator:
            self._locators.pop(entry.locator, None)

    def _forget_oldest(self) -> None:
        oldest = min(
            (entry for entry in self._entries.values() if self._forgettable(entry)),
            key=lambda entry: entry.ended,
            default=None,
        )
        if oldest is not None:
            self._forget(oldest)

    def _terminal(self, entry: _Entry) -> None:
        if entry.state != "ended":
            entry.state = "ended"
            entry.ended = self._clock()
            entry.generation += 1
        for hold in self._holds:
            if hold._entry is entry:
                hold._signal()
        self._cleanup(entry)

    def _prune(self) -> None:
        now = self._clock()
        for entry in tuple(self._entries.values()):
            if entry.state == "pending" and entry.deadline <= now:
                self._terminal(entry)
            elif self._forgettable(entry) and now - entry.ended >= TOMBSTONE_SECONDS:
                self._forget(entry)

    def prepare(self) -> RecoveryFence:
        with self._lock:
            self._charge(0)
            self._prune()
            return {"epoch": self._epoch, "retirement": self._retirement}

    def begin_open(self, identity: str) -> Mutation:
        who = self._identity(identity)
        with self._lock:
            self._prune()
            entry = self._reserve(secrets.token_hex(16), who)
            marker = entry.mutation = object()
            return Mutation(self, entry, marker, self._epoch, self._retirement)

    def _ticket(self, ticket: Mutation, *, fence: bool) -> _Entry:
        entry = ticket._entry
        if ticket._owner is not self or entry is None or entry.mutation is not ticket._marker:
            raise LaunchRefused("launch-stale")
        if (
            self._closed
            or entry.state == "ended"
            or (fence and (ticket._epoch != self._epoch or ticket._retirement != self._retirement))
        ):
            raise LaunchRefused("launch-stale")
        return entry

    def _finished(self, ticket: Mutation) -> None:
        entry = ticket._entry
        if entry is not None and entry.mutation is ticket._marker:
            entry.mutation = None
            self._cleanup(entry)
        ticket._owner = ticket._entry = None

    def stage_binding(self, ticket: Mutation, binding: Binding) -> None:
        """The metadata worker returns None; the prepaid mutation owns its result until commit/abort."""
        with self._lock:
            self._prune()
            entry = self._ticket(ticket, fence=True)
            if type(binding) is not Binding or (entry.binding is not None and entry.binding != binding):
                raise LaunchRefused("launch-mismatch")
            entry.binding = binding

    def commit_open(self, ticket: Mutation, binding: Binding | None = None) -> dict[str, str]:
        with self._lock:
            entry = self._ticket(ticket, fence=False)
            binding = binding if binding is not None else entry.binding
            if entry.state != "opening" or type(binding) is not Binding:
                raise LaunchRefused("launch-invalid")
            key = "latet_lk_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
            locator, keyed = self._credential(key, entry.identity)
            if locator in self._locators:
                raise LaunchRefused("launch-mismatch")
            deadline = self._clock() + PENDING_SECONDS
            self._locators[locator] = entry
            entry.locator, entry.keyed, entry.binding = locator, keyed, binding
            entry.state, entry.deadline = "pending", deadline
            launch_id = entry.launch_id
            self._finished(ticket)
            return {"key": key, "launch_id": launch_id}

    def abort(self, ticket: Mutation) -> None:
        with self._lock:
            if ticket._owner is not self or ticket._entry is None:
                return
            entry = ticket._entry
            if entry.mutation is ticket._marker and entry.state in {"opening", "recovering"}:
                entry.unpublished = True  # failure before publication is not confirmed-child retirement
                self._terminal(entry)
            self._finished(ticket)

    def begin_recovery(self, key: str, launch_id: str, identity: str, epoch: str, retirement: int) -> Mutation:
        who = self._identity(identity)
        locator, keyed = self._credential(key, who)
        if type(launch_id) is not str or _ID.fullmatch(launch_id) is None:
            raise LaunchRefused("launch-invalid")
        with self._lock:
            self._charge(0)
            self._prune()
            if (
                epoch != self._epoch
                or type(retirement) is not int
                or retirement != self._retirement
                or retirement >= MAX_GENERATION
            ):
                raise LaunchRefused("launch-stale")
            entry = self._locators.get(locator)
            if entry is not None:
                if entry.state == "ended":
                    raise LaunchRefused("launch-ended")
                if entry.launch_id != launch_id or not hmac.compare_digest(entry.keyed, keyed):
                    raise LaunchRefused("launch-mismatch")
                if entry.mutation is not None:
                    raise LaunchRefused("launch-busy")
            else:
                entry = self._reserve(launch_id, who)
                entry.state, entry.locator, entry.keyed = "recovering", locator, keyed
                entry.recovered = True
                try:
                    self._locators[locator] = entry
                except BaseException:
                    entry.unpublished = True
                    self._terminal(entry)
                    raise
            marker = entry.mutation = object()
            return Mutation(self, entry, marker, epoch, retirement)

    def commit_recovery(self, ticket: Mutation, binding: Binding | None = None) -> dict[str, str | int]:
        with self._lock:
            self._prune()
            entry = self._ticket(ticket, fence=True)
            binding = binding if binding is not None else entry.binding
            if type(binding) is not Binding or (entry.binding is not None and entry.binding != binding):
                raise LaunchRefused("launch-mismatch")
            if entry.state == "recovering":
                entry.state, entry.deadline, entry.binding = "pending", self._clock() + PENDING_SECONDS, binding
            result = {"launch_id": entry.launch_id, "generation": entry.generation}
            self._finished(ticket)
            return result

    def reserve_hold(self, launch_id: str, identity: str, generation: int) -> Hold:
        who = self._identity(identity)
        if type(launch_id) is not str or _ID.fullmatch(launch_id) is None:
            raise LaunchRefused("launch-invalid")
        with self._lock:
            self._prune()
            self._charge(HOLD_BYTES)
            entry = self._entries.get(launch_id)
            if entry is None or not hmac.compare_digest(entry.identity, who):
                raise LaunchRefused("launch-mismatch")
            if entry.state not in {"pending", "active"}:
                raise LaunchRefused("launch-ended")
            if type(generation) is not int or generation != entry.generation or generation >= MAX_GENERATION:
                raise LaunchRefused("launch-stale")
            if entry.mutation is not None:
                raise LaunchRefused("launch-busy")
            if len(self._holds) >= HOLDS:
                raise LaunchRefused("launch-capacity")
            marker = object()
            hold = Hold(self, entry, marker)
            physical = entry.physical + 1
            self._holds.add(hold)
            entry.physical = physical
            entry.mutation = marker
            return hold

    def _acknowledge(self, hold: Hold) -> int:
        with self._lock:
            self._prune()
            entry = hold._entry
            if (
                self._closed
                or entry is None
                or entry.state not in {"pending", "active"}
                or entry.mutation is not hold._marker
            ):
                raise LaunchRefused("launch-ended")
            entry.generation, entry.state, entry.mutation = hold.generation, "active", None
            hold._ack = True
            for previous in self._holds:
                if previous is not hold and previous._entry is entry:
                    previous._signal()
            return hold.generation

    def _held(self, hold: Hold) -> bool:
        with self._lock:
            entry = hold._entry
            return entry is not None and entry.state == "active" and entry.generation == hold.generation

    def _detach(self, hold: Hold) -> None:
        with self._lock:
            entry = hold._entry
            if entry is None or hold not in self._holds:
                return
            self._stop(hold)
            self._holds.remove(hold)
            entry.physical -= 1
            self._cleanup(entry)
            hold._signal()
            hold._owner = hold._entry = None

    def _stop(self, hold: Hold) -> None:
        with self._lock:
            entry = hold._entry
            if entry is None:
                return
            if entry.mutation is hold._marker:
                entry.mutation = None
            if (hold._ack and entry.generation == hold.generation) or (not hold._ack and hold._pending):
                self._terminal(entry)
            hold._signal()

    def verify(self, key: str, identity: str) -> Check:
        who = self._identity(identity)
        try:
            locator, keyed = self._credential(key, who)
        except LaunchRefused:
            return Check("invalid")
        with self._lock:
            self._prune()
            entry = self._locators.get(locator)
            if entry is None:
                return Check("unknown" if not self._closed else "unavailable")
            if not hmac.compare_digest(entry.keyed, keyed):
                return Check("mismatch")
            if entry.state == "ended":
                return Check("ended")
            binding = entry.binding
            if binding is None or entry.state != "active":
                # A recovered launch's agent runs while it is registered again and its hold comes back: its requests
                # are claims (R-107; the re-review's R-6), where a new launch's agent has not started before its hold's
                # acknowledgment.
                recovering = entry.recovered and entry.state in ("recovering", "pending")
                return Check("recovering" if recovering else "pending")
            digest = hashlib.sha256(binding.folder + b"\0" + binding.root).digest()
            authority = Authority(self._epoch, entry.launch_id, who, entry.generation, binding.project, digest, b"")
            return Check(
                "active",
                Authority(
                    authority.epoch,
                    authority.launch_id,
                    who,
                    authority.generation,
                    authority.project,
                    digest,
                    self._tag(authority),
                ),
            )

    def verify_request(self, key: str, identity: str) -> tuple[Check, Binding | None]:
        """Project the verified binding from the same protected registry entry."""
        with self._lock:
            checked = self.verify(key, identity)
            if checked.authority is None:
                return checked, None
            return checked, self._entries[checked.authority.launch_id].binding

    def _tag(self, authority: Authority) -> bytes:
        data = (authority.epoch + authority.launch_id + str(authority.generation) + ":" + authority.project).encode(
            "ascii"
        )
        return hmac.digest(self._binding_key, b"receipt\0" + data + authority.identity + authority.binding, "sha256")

    def validate(self, authority: Authority) -> bool:
        with self._lock:
            self._prune()
            if type(authority) is not Authority or not hmac.compare_digest(authority.tag, self._tag(authority)):
                return False
            entry = self._entries.get(authority.launch_id)
            return bool(
                not self._closed
                and authority.epoch == self._epoch
                and entry is not None
                and entry.state == "active"
                and entry.generation == authority.generation
                and hmac.compare_digest(entry.identity, authority.identity)
                and entry.binding is not None
                and entry.binding.project == authority.project
                and hmac.compare_digest(
                    hashlib.sha256(entry.binding.folder + b"\0" + entry.binding.root).digest(), authority.binding
                )
            )

    def end(self, launch_id: str, identity: str) -> bool:
        who = self._identity(identity)
        if type(launch_id) is not str or _ID.fullmatch(launch_id) is None:
            raise LaunchRefused("launch-invalid")
        with self._lock:
            entry = self._entries.get(launch_id)
            if entry is None:
                return False
            if not hmac.compare_digest(entry.identity, who):
                raise LaunchRefused("launch-mismatch")
            self._terminal(entry)
            return True

    def confirmed_exit(self, launch_id: str, identity: str, epoch: str) -> bool:
        who = self._identity(identity)
        if type(launch_id) is not str or _ID.fullmatch(launch_id) is None:
            raise LaunchRefused("launch-invalid")
        with self._lock:
            if self._closed or epoch != self._epoch:
                raise LaunchRefused("launch-stale")
            entry = self._entries.get(launch_id)
            if entry is not None and not hmac.compare_digest(entry.identity, who):
                raise LaunchRefused("launch-mismatch")
            if entry is not None:
                self._terminal(entry)
                entry.confirmed = True
                self._cleanup(entry)
                return self._entries.get(launch_id) is not entry
            if self._retirement == MAX_GENERATION:
                return False
            self._retirement += 1  # an absent row must fence a not-yet-entered delayed registration too
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.ceiling = None
            for entry in tuple(self._entries.values()):
                self._terminal(entry)
            if not self._entries:
                self._binding_key = self._locator_key = b""
                self._entries, self._locators, self._identities, self._holds = {}, {}, {}, set()
