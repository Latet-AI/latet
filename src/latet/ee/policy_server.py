"""A Latet server's policy (Latet Enterprise, locked rules): one row of ee_policy in PostgreSQL, the same floor for
every identity, people and API keys, on every instance. Its text holds values (the company's terms, values never to
mask), so it is sealed with the server's master key; its etag, and when and by whom it was set, are beside it, and
the sealing names the etag, the time and the author, so none can be changed apart from the text. It is written in the
transaction of the administration route that sets it (`latet server policy set`), which records it in the
administration log; once that commits, it is announced on Valkey (`ee:policy`), which E1's listener hands to
``refresh`` on every instance: the instance reads it again, weighs the license, and tells the edition, whose watcher
(the server's spaces) raises every space it holds to the new floor, each in its own worker. A message only says "look
again". A check of the stored etag and time every CHECK seconds catches a message an instance missed, and a license
installed or removed. A stored policy an instance cannot read leaves the floor it read last in force; one it never
read builds no space (PolicyUnavailable).

The store is read, and the license weighed, outside the lock that ``floor`` takes: a slow database never holds a space's
build. Looks that run at once (the listener's, the check's, a route's) are numbered, and one that read before another
never replaces what that one applied."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from latet.ee.license import REFRESH_SECONDS
from latet.ee.policy import Floor, PolicyError, parse_policy
from latet.ee.policy_sources import source_view
from latet.ee.server import _within, publish
from latet.events import make_event
from latet.floor import FloorUnavailable
from latet.logline import Paced
from latet.transfer import KEYS

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.app import Server
    from latet.server.db import Database
    from latet.server.master import MasterKey

log = logging.getLogger(__name__)

CHANNEL = "ee:policy"
CHECK = REFRESH_SECONDS  # seconds between two looks of an instance's own: what every instance agrees within
AAD = "ee_policy"
_NOT_IN_FORCE = "no license switches locked rules on"
_LICENSE_UNREAD = "this instance has not read the server's license yet: no space is built until it has"

# The etag and when it was set: what says the stored policy changed (an etag alone would let a row set again, or a
# text of the same etag, go unread).
Stamp = tuple[str, float]


class PolicyUnavailable(FloorUnavailable):
    """A policy is stored that this instance cannot read and never read (made by a newer Latet, sealed with another
    master key, a text an older release fails on), or the license was never read here: no space is built below it
    (ruling 12). Its words never quote the policy. The dashboard and the administration answer it with fixed words."""


@dataclass(frozen=True)
class StoredPolicy:
    """The row: the policy's text (never printed: it holds values), its etag, when and by whom it was set."""

    text: str = field(repr=False)
    etag: str
    set_at: float
    set_by: str


class PolicyStore(Protocol):
    # The stored policy's etag and when it was set; None: none stored.
    def stamp(self, *, conn: Connection[Any] | None = None) -> Stamp | None: ...
    # The same in the caller's transaction, every other change of the policy held off until it ends.
    def held(self, conn: Connection[Any]) -> Stamp | None: ...
    def load(self) -> StoredPolicy | None: ...  # PolicyError when what is stored cannot be opened
    def save(self, policy: StoredPolicy, *, conn: Connection[Any] | None = None) -> None: ...
    def delete(self, *, conn: Connection[Any] | None = None) -> bool: ...


def _place(etag: str, set_at: float, set_by: str) -> str:
    """The sealed text's associated data: its row's etag, time and author (L3), the author last (it may hold a colon:
    the etag is hex and the time a number). The time as the float the row gives back: an int written as `1791191520`
    would be read back as `1791191520.0`, and the text would open nowhere."""
    return f"{AAD}:{etag}:{float(set_at)!r}:{set_by}"


class PgPolicyStore:
    """ee_policy in PostgreSQL. A change is written on ``conn``, a connection in the caller's transaction (the
    administration route's, beside its entry), or in a transaction of its own."""

    def __init__(self, db: Database, master: MasterKey) -> None:
        self.db = db
        self.master = master

    def stamp(self, *, conn: Connection[Any] | None = None) -> Stamp | None:
        """The stored policy's etag and time, read on ``conn`` when given (the caller's transaction)."""
        if conn is not None:
            row = conn.execute("SELECT etag, set_at FROM ee_policy WHERE id = 1").fetchone()
        else:
            with self.db.connection() as own:
                row = own.execute("SELECT etag, set_at FROM ee_policy WHERE id = 1").fetchone()
        return None if row is None else (row[0], float(row[1]))

    def held(self, conn: Connection[Any]) -> Stamp | None:
        """The stored policy's etag and time as the caller's transaction finds them, the table locked there against
        every other write until it ends (a row or none: an insert waits too), so that a change that checks what it
        replaces is not overtaken between the check and its write. Reads are never held."""
        conn.execute("LOCK TABLE ee_policy IN SHARE ROW EXCLUSIVE MODE")
        return self.stamp(conn=conn)

    def load(self) -> StoredPolicy | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT etag, sealed, set_at, set_by FROM ee_policy WHERE id = 1").fetchone()
        if row is None:
            return None
        etag, sealed, set_at, set_by = row[0], bytes(row[1]), float(row[2]), row[3]
        try:
            text = self.master.open(sealed, _place(etag, set_at, set_by)).decode("utf-8")
        except Exception as exc:  # another master key, a row changed apart from its text: said by the error's type
            raise PolicyError(
                f"the server's policy cannot be opened with this master key ({type(exc).__name__})"
            ) from None
        return StoredPolicy(text, etag, set_at, set_by)

    def save(self, policy: StoredPolicy, *, conn: Connection[Any] | None = None) -> None:
        sealed = self.master.seal(policy.text.encode("utf-8"), _place(policy.etag, policy.set_at, policy.set_by))
        with _within(self.db, conn) as tx:
            tx.execute(
                "INSERT INTO ee_policy (id, etag, sealed, set_at, set_by) VALUES (1, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET etag = EXCLUDED.etag, sealed = EXCLUDED.sealed, "
                "set_at = EXCLUDED.set_at, set_by = EXCLUDED.set_by",
                (policy.etag, sealed, policy.set_at, policy.set_by),
            )

    def delete(self, *, conn: Connection[Any] | None = None) -> bool:
        with _within(self.db, conn) as tx:
            return tx.execute("DELETE FROM ee_policy WHERE id = 1").rowcount > 0


@dataclass(frozen=True)
class _Look:
    """What one look found: the stamp stored (None: none), and, when it read the row, the floor in it or why not."""

    stamp: Stamp | None
    read: bool  # False: the stamp was the one known, and nothing was read
    floor: Floor | None = None
    problem: str | None = None


def _nothing() -> None:
    return None


def _ignore(event: Mapping[str, Any]) -> None:
    return None


class ServerPolicy:
    """One instance's view of the server's policy (``latet.ee.policy_sources.PolicySource``). ``licensed``: whether the
    license switches locked rules on (None while this instance never read the license), weighed at each look only;
    ``changed``: the edition's floor_changed; ``every``: seconds between two looks of its own once started (None: CHECK,
    read when it is made); ``publish``: tells the other instances, answering whether the message went out (None is
    taken as it did)."""

    def __init__(
        self,
        store: PolicyStore,
        *,
        publish: Callable[[str, Mapping[str, Any]], bool | None],
        origin: str,
        licensed: Callable[[], bool | None],
        changed: Callable[[], None] = _nothing,
        record: Callable[[Mapping[str, Any]], None] = _ignore,
        clock: Callable[[], float] = time.time,
        every: float | None = None,
    ) -> None:
        self._store = store
        self._publish = publish
        self._origin = origin
        self._licensed = licensed
        self._changed = changed
        self._record = record
        self._clock = clock
        self._every = CHECK if every is None else every
        self._lock = threading.RLock()
        self._stamp: Stamp | None = None  # what is stored, as read last
        self._floor: Floor | None = None  # the floor read last: kept while what is stored cannot be read
        self._problem: str | None = None  # why what is stored cannot be read
        self._weighed: bool | None = None  # what licensed() answered at the last look
        self._asked = self._applied = 0  # the looks begun, and the last one applied
        self._unread = Paced()  # the database not answering at a look: said once a minute
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        # Read once, and the license weighed once: the database not answering is raised (the instance does not start).
        look = self._look(None)
        weighed = self._licensed()
        with self._lock:
            said = self._take(look, weighed)
            self._told = self._state()
        self._said(said)

    @classmethod
    def of(
        cls,
        server: Server,
        *,
        licensed: Callable[[], bool | None],
        changed: Callable[[], None] = _nothing,
        record: Callable[[Mapping[str, Any]], None] = _ignore,
    ) -> ServerPolicy:
        """A server instance's, its check started."""
        policy = cls(
            PgPolicyStore(server.db, server.master),
            publish=lambda channel, message: publish(server.bus, channel, message),
            origin=server.settings.url,
            licensed=licensed,
            changed=changed,
            record=record,
            clock=server.clock,
        )
        policy.start()
        return policy

    # --- the floor, as looked at last ---------------------------------------------------------------------------------

    def floor(self) -> Floor | None:
        """The floor in force as the last look found it, the same object until a look finds it changed; it reads
        nothing and weighs nothing (R4). None when no policy is stored or the license does not switch locked rules on;
        PolicyUnavailable while one is stored that was never read here, or the license never was."""
        with self._lock:
            if self._stamp is None or self._weighed is False:
                return None
            if self._weighed is None:
                raise PolicyUnavailable(_LICENSE_UNREAD)
            if self._floor is None:
                raise PolicyUnavailable(self._problem or "this instance has not read the server's policy")
            return self._floor

    def refresh(self) -> bool:
        """Look again now: the stored etag and time read, the policy read again when they changed, the license weighed;
        then, when the floor in force changed, recorded and the edition told (``changed``), and True. The database not
        answering is said by its type and answers False, the floor read last staying."""
        with self._lock:
            self._asked += 1
            asked, known = self._asked, self._stamp
        try:
            look = self._look(known)
        except Exception as exc:  # its words can name the database's address
            if count := self._unread.due():
                log.warning(
                    "the server's policy could not be read (%s): the floor read last stays in force%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return False
        weighed = self._licensed()
        with self._lock:
            if asked < self._applied:  # a look begun after this one was applied already: it is newer
                return False
            self._applied = asked
            said = self._take(look, weighed)
            state = self._state()
            told = state != self._told
            self._told = state
        self._said(said)
        if told:
            self._record(make_event("policy", clock=self._clock, reason="changed"))
            self._changed()
        return told

    def _look(self, known: Stamp | None) -> _Look:
        """Outside the lock: what is stored, read when it is not ``known``. Only the database's errors are raised:
        anything a stored text does is a problem said (L2)."""
        stamp = self._store.stamp()
        if stamp is None:
            return _Look(None, read=True)
        if stamp == known:
            return _Look(stamp, read=False)
        try:
            stored = self._store.load()
        except PolicyError as exc:
            return _Look(stamp, read=True, problem=str(exc))
        if stored is None:  # removed meanwhile
            return _Look(None, read=True)
        stamp = (stored.etag, stored.set_at)
        try:
            floor = parse_policy(stored.text, source="server", received=stored.set_at, origin=self._origin)
        except PolicyError as exc:
            return _Look(stamp, read=True, problem=str(exc))
        except Exception as exc:  # whatever else stops it: by its type, its words can quote the text
            return _Look(stamp, read=True, problem=f"{type(exc).__name__} while it was read")
        return _Look(stamp, read=True, floor=floor)

    def _take(self, look: _Look, weighed: bool | None) -> str | None:
        """Under the lock: what ``look`` found kept, and ``weighed``. The problem to say in the log when one is new."""
        self._weighed = weighed
        if not look.read:
            return None
        self._stamp = look.stamp
        if look.stamp is None:
            self._floor, self._problem = None, None
            return None
        if look.floor is not None:
            self._floor, self._problem = look.floor, None
            return None
        kept = "the policy read last stays in force" if self._floor is not None else "no space is built until it is"
        self._problem = f"this instance cannot read the server's policy ({look.problem}): {kept}"
        return self._problem

    def _said(self, problem: str | None) -> None:
        if problem is not None:  # its words never quote the policy (PolicyError's, or an error's type)
            log.error("%s", problem)

    def _state(self) -> str | None:
        """Under the lock: the floor in force as it is told: its etag, "unavailable", or None for none."""
        if self._stamp is None or self._weighed is False:
            return None
        if self._weighed is None or self._floor is None:
            return "unavailable"
        return self._floor.etag

    # --- a change, made by the administration route in its transaction ----------------------------------------------

    def read(self, text: str) -> Floor:
        """``text`` checked as the server's policy, set now (PolicyError): nothing written. The administration route
        reads it before its transaction, so that the rows it holds are held for the writes alone."""
        return parse_policy(text, source="server", received=self._clock(), origin=self._origin)

    def save(self, text: str, floor: Floor, *, actor: str, conn: Connection[Any] | None = None) -> None:
        """``text``, which ``read`` made ``floor`` of, written as the server's policy, on ``conn`` when given. Nothing
        this instance holds changes and nobody is told: the caller's transaction may still roll back; it calls
        ``committed`` once it commits."""
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != floor.etag:  # never another text's floor
            raise ValueError("the floor given was not read from this text")
        self._store.save(StoredPolicy(text, floor.etag, float(floor.received), actor), conn=conn)

    def set(self, text: str, *, actor: str, conn: Connection[Any] | None = None) -> Floor:
        """``read``, then ``save``: ``text`` checked (PolicyError, nothing written), then written as the policy."""
        floor = self.read(text)
        self.save(text, floor, actor=actor, conn=conn)
        return floor

    def remove(self, *, conn: Connection[Any] | None = None) -> bool:
        """The server's policy taken away, on ``conn`` when given; whether there was one. ``committed`` follows."""
        return self._store.delete(conn=conn)

    def stored_in(self, conn: Connection[Any]) -> bool:
        """Whether a policy is stored, as the caller's transaction finds it (a change of the license that ends it says
        so in its entry, L4)."""
        return self._store.stamp(conn=conn) is not None

    def stored_etag(self) -> str | None:
        """The stored policy's etag, read now (None: none stored), whether or not its text can be opened: what the
        Policy page's editor sends back with the text that replaces it (E3)."""
        stamp = self._store.stamp()
        return None if stamp is None else stamp[0]

    def held_etag(self, conn: Connection[Any]) -> str | None:
        """The stored policy's etag as the caller's transaction finds it, every other change of the policy held off
        until that transaction ends (``PolicyStore.held``): the editor's save checks it, then writes (E3)."""
        stamp = self._store.held(conn)
        return None if stamp is None else stamp[0]

    def committed(self) -> bool:
        """Once the caller's change committed: this instance reads it again, then every other is told on ee:policy (as
        E1 tells a license); whether that message went out. One that misses it looks within ``every`` seconds."""
        self.refresh()
        return self._publish(CHANNEL, {}) is not False

    @property
    def every(self) -> float:
        """Seconds between two looks of an instance's own: within them every instance takes a change it was not told."""
        return self._every

    # --- what is said of it -------------------------------------------------------------------------------------------

    def summary(self) -> dict[str, Any] | None:
        """Where the floor comes from and what is wrong, as /me and `latet server status` say it (no value, notices
        counted); None with nothing stored and nothing wrong."""
        with self._lock:
            sources = [source_view(self._floor)] if self._floor is not None else []
            problems: list[dict[str, str]] = []
            if self._problem is not None:
                problems.append({"level": "fail", "text": self._problem})
            if self._stamp is not None and self._weighed is None:
                problems.append({"level": "fail", "text": _LICENSE_UNREAD})
            elif self._stamp is not None and self._weighed is False:
                problems.append({"level": "warn", "text": f"the server's policy is not in force: {_NOT_IN_FORCE}"})
        if not sources and not problems:
            return None
        return {"sources": sources, "problems": problems}

    def admin_view(self) -> dict[str, Any]:
        """The policy as an administrator reads it (`latet server policy show`): its text, when and by whom it was set,
        the keys it locks (in a rules file's order), whose exceptions count, its notices, and whether it is in force,
        read and weighed now: it answers an administrator's request, not a space's. The database's errors are raised."""
        try:
            stored = self._store.load()
        except PolicyError as exc:
            return {"policy": None, "problems": [str(exc)]}
        if stored is None:
            return {"policy": None, "problems": []}
        try:
            floor = parse_policy(stored.text, source="server", received=stored.set_at, origin=self._origin)
        except PolicyError as exc:
            return {"policy": None, "problems": [str(exc)]}
        except Exception as exc:  # by its type: its words can quote the text
            return {"policy": None, "problems": [f"the server's policy could not be read ({type(exc).__name__})"]}
        weighed = self._licensed()
        problems = []
        if weighed is None:
            problems.append("not in force on this instance: it has not read the server's license yet")
        elif not weighed:
            problems.append(f"not in force: {_NOT_IN_FORCE}")
        view = {
            "etag": stored.etag,
            "set_at": stored.set_at,
            "set_by": stored.set_by,
            "keys": [key for key in KEYS if key in floor.keys],
            "brought": [key for key in KEYS if key in floor.brought],
            "exceptions": floor.exceptions,
            "notices": list(floor.notices),
            "in_force": weighed is True,
            "text": stored.text,
        }
        return {"policy": view, "problems": problems}

    # --- its own looks ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Look again every ``every`` seconds from now on, in a thread of its own, until closed. Starting again does
        nothing more."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._look_every, name="latet-policy", daemon=True)
        self._thread.start()

    def _look_every(self) -> None:
        while not self._stopping.wait(self._every):
            try:
                self.refresh()
            except Exception as exc:  # by its type: its words can quote what it applied; the checking goes on
                log.warning(
                    "a look at the server's policy failed (%s): the next is in %g s", type(exc).__name__, self._every
                )

    def close(self) -> None:
        """No more looks of its own: its thread ends (waited for 5 seconds, never from inside it)."""
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
