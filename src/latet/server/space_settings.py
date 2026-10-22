"""An identity's settings on a server, in PostgreSQL: its own part and its approved snapshot, sealed with its root key
and each bound to its row, over the shared settings (``shared.OwnSettings`` does the layering).

Each instance runs a copy of the identity's settings. A save names the version of the own part its copy was read at
(or that its own last write made), and is refused when another instance wrote one since (``SettingsStale``, an
``OSError`` as a store's failure), never written over that write: ``stale()`` is then called, once the save's
transaction is over, so that the instance reads them again and the change can be made anew. An own part equal to the
one kept is not written.

Every write of the own part bumps the identity's settings' change count (``change_counters``, owner ``<id>:settings``:
its vault has a count of its own, the identity's id, so that a settings change makes no instance read the vault again)
in the transaction of its compare-and-set, so that every instance whose copy is older finds it behind (Task 12's check
of the counts re-applies it, weighing what it reads), and each count is handed to ``counted`` once committed, so that
the space this copy serves knows it applied it itself. But
the settings are told (``changed(counter)``) only once their approved snapshot is kept and differs from the one kept
before, its own count bumped with it, so that an instance that hears it reads an own part and a snapshot written
together. A save alone is told to nobody: what waits for the person's approval is saved so, and the other instances go
on running what was approved until the count makes them read it, and weigh it. When the snapshot cannot be kept, the
own part written for the settings being applied is told with its own count, then the error is raised. What records a
change (``recording``, the administration's entry for an API key's settings) is written last in the transaction of the
snapshot that approves it, so that both are kept or neither, and dropped once kept so: it is written once.

What is there and does not open (written or moved by hand, sealed with another key) is refused, never read as none:
settings that do not open stop the space (``ValueError``), and so does an approved snapshot that does not open, where
none would make whatever the own part holds count as approved. Each is said in the log by the identity and the kind of
row alone, once a minute at most. An own part over ``SETTINGS_LIMIT`` is not kept, nor an API key's snapshot (its own
part), nor a person's snapshot over twice that (the shared settings and her own part, each bound at the limit). The
database's own failures are raised as ``OSError`` naming the type of the error alone (a driver's words can quote an
address or a key).

The approved snapshot is read as it applies now: a person's with what is the server's (where the local reviewer runs,
the port, the capture folder) taken from the shared settings of now (``shared.repinned``), never weighed per person. An
API key's (``kind="key"``) holds its approved own part alone, and is read laid over the shared settings in force: a
key's start and re-applications weigh only what its own part changed (a rollback of its own row waits until the
administration saves its settings again), and every shared change reaches a key unweighed, no person being there to
ask. What that gives up: a rollback of the shared row by whoever writes the database reaches every key's space, as any
shared change does; E4's export shows it.

What no seal shows: whoever writes the database can delete both rows, or write back an older pair he kept (sealed for
this identity, they open): the identity's settings are then reset to that state, approved, as far as the server can
tell. A copy of the database kept elsewhere (E4's export) shows it, and E0c's least-privilege role keeps it from the
instances' own user.

Who calls (``latet.state.SettingsStore``): worker threads, one at a time under the runtime's lock, and the thread that
starts the runtime; never the event loop. Each call is a round trip to PostgreSQL or a few (the shared settings once,
kept by the instance, then the identity's rows); the pool waits ten seconds at most for a connection."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Literal

import psycopg
from cryptography.exceptions import InvalidTag

from latet.config import Config
from latet.server.db import DatabaseError
from latet.server.sealing import Sealer
from latet.server.shared import (
    SETTINGS_LIMIT,
    OwnSettings,
    SettingsRefused,
    SettingsStale,
    SettingsTooLarge,
    SharedSettings,
    layered,
    own_part,
    repinned,
    reused,
    validated,
)

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.db import Database

log = logging.getLogger(__name__)

__all__ = ["SETTINGS_LIMIT", "PgSettings", "SettingsStale", "SettingsTooLarge", "SettingsUnrecorded", "settings_owner"]

SAY_EVERY = 60.0  # seconds between two lines of the log about one identity's row of one kind that does not open
# What each kind of row is called in the log, and what follows when it does not open.
_KINDS = {
    "own": ("settings", "its space does not start"),
    "reviewed": ("approved settings", "its space does not start"),
    "change": ("waiting changes", "it counts as none"),
}

_READ: Final = "SELECT data FROM settings WHERE owner = %s AND kind = %s"
_OWN: Final = "SELECT data, version FROM settings WHERE owner = %s AND kind = 'own'"
# The own part as kept, read in the transaction that writes over it, and locked until it ends: the compare-and-set.
_LOCKED: Final = _OWN + " FOR UPDATE"
# The own part, written over the version a copy was read at: the first when none was kept, else only over that version.
_FIRST: Final = (
    "INSERT INTO settings (owner, identity_id, kind, data, changed, changed_by, version)"
    " VALUES (%s, %s, 'own', %s, %s, %s, 1) ON CONFLICT (owner, kind) DO NOTHING RETURNING version"
)
_NEXT: Final = (
    "UPDATE settings SET data = %s, changed = %s, changed_by = %s, version = version + 1"
    " WHERE owner = %s AND kind = 'own' AND version = %s RETURNING version"
)
_REVIEWED: Final = (
    "INSERT INTO settings (owner, identity_id, kind, data, changed, changed_by) VALUES (%s, %s, 'reviewed', %s, %s, %s)"
    " ON CONFLICT (owner, kind) DO UPDATE SET data = EXCLUDED.data, changed = EXCLUDED.changed,"
    " changed_by = EXCLUDED.changed_by"
)
# The identity's settings' change count, bumped last in a change's transaction (as the vault's changes bump its own).
_COUNT: Final = (
    "INSERT INTO change_counters (owner, counter) VALUES (%s, 1)"
    " ON CONFLICT (owner) DO UPDATE SET counter = change_counters.counter + 1 RETURNING counter"
)


def settings_owner(identity_id: str) -> str:
    """Whose row of ``change_counters`` counts an identity's settings' changes (its vault's is its own id): a change of
    its settings makes no instance read its vault again."""
    return f"{identity_id}:settings"


class SettingsUnrecorded(OSError):
    """What records an approval (``PgSettings.recording``) could not be written: the approval was not kept either, its
    transaction rolled back. Its words name the error's type alone."""


class Unopened:
    """What the database keeps of an identity's settings, or of its waiting changes, that is there and cannot be read
    (written or moved by hand, sealed with another root key; a change kept by another version of Latet): counted, and
    said in the log by the identity and the kind of row alone, the first at once and then one line a minute at most for
    each, with how many since the line before."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: dict[tuple[str, str], int] = {}
        self._said: dict[tuple[str, str], float] = {}
        self._unsaid: dict[tuple[str, str], int] = {}

    def met(self, identity: str, kind: str) -> None:
        now, which = time.monotonic(), (identity, kind)
        with self._lock:
            self.counts[which] = self.counts.get(which, 0) + 1
            self._unsaid[which] = self._unsaid.get(which, 0) + 1
            said = self._said.get(which)
            if said is not None and now - said < SAY_EVERY:
                return
            self._said[which], count, self._unsaid[which] = now, self._unsaid[which], 0
        name, then = _KINDS.get(kind, (kind, "it is not used"))
        log.warning(
            "what the database keeps of %s's %s cannot be read (written or moved there by hand, sealed with another "
            "root key, or kept by another version of Latet): %s%s",
            identity,
            name,
            then,
            f"; {count} times since the line before" if count > 1 else "",
        )


UNOPENED = Unopened()


class PgSettings(OwnSettings):
    def __init__(
        self,
        db: Database,
        shared: SharedSettings,
        identity_id: str,
        root_key: bytes,
        *,
        changed: Callable[[int], None] | None = None,
        stale: Callable[[], None] | None = None,
        counted: Callable[[int], None] | None = None,
        kind: Literal["person", "key"] = "person",
    ) -> None:
        super().__init__(self._shared_settings)
        self.db, self.identity = db, identity_id
        self.kind = kind  # an API key's approved snapshot is its own part (the module's words)
        self._store = shared
        self._sealer = Sealer(root_key, b"settings")
        self._changed: Callable[[int], None] = changed or (lambda _counter: None)
        self._stale: Callable[[], None] = stale or (lambda: None)
        # Every count this copy bumped, told or not, once its transaction has committed: what the space it serves has
        # applied itself (an own part saved alone, or written for the settings being applied, is told to nobody).
        self._counted: Callable[[int], None] = counted or (lambda _counter: None)
        self._version: int | None = None  # the own part's, as the last load read it or this copy last wrote it
        self._untold: int | None = None  # the count of an own part written for the settings being applied, not told
        # The approved snapshot as last read, sealed, with what it holds made settings: read again only when the row
        # changed (each re-application reads it, and a snapshot holds every shared term). A person's only.
        self._snapshot_read: tuple[bytes, Config] | None = None
        # The settings approved as this copy last read or kept them: a save of the same is no write (the settings
        # applied are the approved ones at each start and re-application of a space).
        self._approved: Config | None = None
        # What records the next approval, written last in the transaction of its snapshot, given that transaction's
        # connection, and dropped once that transaction has committed: a save that writes nothing does not call it,
        # and one that fails leaves it for the next. Its owner sets it, and drops it, under the runtime's lock.
        self.recording: Callable[[Connection[Any]], None] | None = None

    # --- the database -------------------------------------------------------------------------------------------------

    def _db[T](self, work: Callable[[Connection[Any]], T], *, transaction: bool = False) -> T:
        """``work`` on a connection of the pool, in a transaction of its own when asked. Raises OSError naming the type
        of the driver's error and nothing else, raised once that error is handled, so that no traceback shows its
        words."""
        kind = ""
        try:
            with self.db.transaction() if transaction else self.db.connection() as conn:
                return work(conn)
        except (psycopg.Error, DatabaseError) as exc:
            kind = type(exc).__name__
        raise OSError(f"the settings of {self.identity} could not be read or kept in the database ({kind})")

    def _shared_settings(self) -> Config:
        """The shared settings, under this identity's: the instance's copy itself, which every use here only reads
        (laid under the own part, weighed, composed), so that a re-application under tens of thousands of shared terms
        makes no copy of them. Raises OSError as ``_db`` does."""
        kind = ""
        try:
            return self._store.get(copy=False)
        except (psycopg.Error, DatabaseError) as exc:
            kind = type(exc).__name__
        raise OSError(f"the shared settings could not be read from the database ({kind})")

    def _opened(self, kind: str, sealed: bytes) -> Any:
        """What the identity's row of ``kind`` holds. Raises ValueError, said in the log, when it does not open."""
        found: Any = None
        opened = False
        with contextlib.suppress(InvalidTag, ValueError):  # another root key, another row's, a byte changed by hand
            found, opened = self._sealer.open_json(sealed, f"{self.identity}:{kind}"), True
        if not opened:
            UNOPENED.met(self.identity, kind)
            raise ValueError(
                f"the {_KINDS[kind][0]} of {self.identity} kept in the database do not open with its root key: written "
                "or moved there by hand, or sealed with another key"
            )
        return found

    def _fits(self, sealed: bytes, what: str, limit: int | None = None) -> None:
        """Raises SettingsTooLarge when ``sealed`` weighs more than ``limit`` (``SETTINGS_LIMIT``: an own part's)."""
        limit = SETTINGS_LIMIT if limit is None else limit
        if len(sealed) > limit:
            raise SettingsTooLarge(
                f"the {what} of {self.identity} would weigh {len(sealed)} bytes sealed, more than a server keeps "
                f"({limit}): they were not kept"
            )

    def _snapshot_limit(self) -> int:
        """What an approved snapshot may weigh sealed: a person's holds the shared settings whole and her own part, and
        is bound at both parts' limits, so that her approval of settings whose two parts each weigh near theirs is kept
        (refused, it would be asked again at every re-application); an API key's holds its own part alone."""
        return SETTINGS_LIMIT if self.kind == "key" else 2 * SETTINGS_LIMIT

    def _count(self, conn: Connection[Any]) -> int:
        """Bump the identity's settings' change count in ``conn``'s transaction: the count of the change it writes."""
        row = conn.execute(_COUNT, (settings_owner(self.identity),)).fetchone()
        assert row is not None  # RETURNING gives the row it counted
        return int(row[0])

    # --- the own part ---------------------------------------------------------------------------------------------

    def _own_row(self) -> tuple[dict[str, Any], int]:
        """The own part as kept, and the version it was written at (0: none is kept)."""
        row = self._db(lambda conn: conn.execute(_OWN, (self.identity,)).fetchone())
        if row is None:
            return {}, 0
        found = self._opened("own", row[0])
        return (found if isinstance(found, dict) else {}), int(row[1])

    def _own(self) -> dict[str, Any]:
        return self._own_row()[0]

    def load(self) -> Config:
        """The settings kept; the version of the own part they were read at is the one the next save names."""
        own, version = self._own_row()
        shared = self._shared()
        config = layered(shared, own)
        self._version, self._laid_over = version, shared
        return config

    def prepare(self, config: Config, *, strict: bool = False) -> Config:
        self._untold = None  # settings being applied from here on: an own part written for them is theirs to tell
        return super().prepare(config, strict=strict)

    def save(self, config: Config) -> None:
        """Keep ``config``'s own part. Raises OSError when it cannot be kept, as ``SettingsStore`` says: the database
        fails, another instance wrote one since this copy was read, it weighs too much, or what is kept and read first
        does not open (nothing is then written over it); and SettingsRefused, nothing written, for what it holds (more
        projects or rules than a server keeps for one identity), as its preparation does."""
        refused = ""
        try:
            super().save(config)
        except SettingsRefused:
            raise  # refused for what the settings hold, nothing written: answered as at their preparation
        except ValueError as exc:  # the own part kept, or the shared settings, do not open: named, never their content
            refused = str(exc)
        else:
            return
        raise OSError(refused)

    def _keep_own(self, own: dict[str, Any]) -> None:
        """``own`` written over the version this copy was read at, and counted, in one transaction; nothing when it is
        the own part kept already. Raises OSError when another instance wrote one since (nothing is written or counted,
        and ``stale()`` is called once the transaction is over), or when it weighs more than ``SETTINGS_LIMIT``. Told
        to nobody here: ``save_reviewed`` tells it once its snapshot is kept, and what waits for the person is not told
        at all."""
        sealed = self._sealer.seal_json(own, f"{self.identity}:own")
        self._fits(sealed, "settings")
        read, now = self._version or 0, time.time()

        def write(conn: Connection[Any]) -> tuple[int, int] | Literal["stale", "same"]:
            kept = conn.execute(_LOCKED, (self.identity,)).fetchone()
            if (int(kept[1]) if kept is not None else 0) != read:
                return "stale"
            if kept is not None and self._kept_as(kept[0], own):
                return "same"
            if kept is None:  # none was kept when this copy was read: the first, unless one was made meanwhile
                row = conn.execute(_FIRST, (self.identity, self.identity, sealed, now, self.identity)).fetchone()
            else:
                row = conn.execute(_NEXT, (sealed, now, self.identity, self.identity, read)).fetchone()
            return (int(row[0]), self._count(conn)) if row is not None else "stale"

        written = self._db(write, transaction=True)
        if written == "same":
            return
        if written == "stale":
            self._call_stale()
            raise SettingsStale(
                f"the settings of {self.identity} changed on another instance since they were read here: read them "
                "again, then make the change"
            )
        self._version, self._untold = written
        self._counted(written[1])

    def _laid_back(self) -> None:
        """A save of the settings in use laid back on the settings kept (``OwnSettings.save``): the space reads its
        settings again (the hook a save refused as stale calls), so that what waits for the person is asked anew over
        what is kept, and her approval of it applies. Its failure is said by its type, never raised into the save."""
        try:
            self._stale()
        except Exception as exc:
            log.warning(
                "the settings of %s: what follows a save laid back on the settings kept failed (%s)",
                self.identity,
                type(exc).__name__,
            )

    def _kept_as(self, sealed: bytes, own: dict[str, Any]) -> bool:
        """Whether the own part kept, sealed, is ``own``; one that does not open is not."""
        with contextlib.suppress(InvalidTag, ValueError):
            return self._sealer.open_json(sealed, f"{self.identity}:own") == own
        return False

    def _call_stale(self) -> None:
        """This copy is older than what is kept: ``stale()``, outside any transaction (it may read the settings again);
        its own failure is said in the log by its type, never raised into the save that was refused."""
        try:
            self._stale()
        except Exception as exc:
            log.warning(
                "the settings of %s: what follows a save refused as stale failed (%s)",
                self.identity,
                type(exc).__name__,
            )

    # --- the approved snapshot ------------------------------------------------------------------------------------

    def reviewed(self) -> Config | None:
        """The settings approved last, as they apply now: a person's with what is the server's taken from the shared
        settings of now; an API key's own part laid over the shared settings in force. None when none are kept, or when
        what is kept is not valid settings (another version of Latet's, as on a computer), or a key's part does not lay
        over the shared settings: the settings kept then count as approved, as for anyone. Raises ValueError, said in
        the log, for a snapshot that does not open: none would make the settings kept now count as approved, whatever
        they hold."""
        row = self._db(lambda conn: conn.execute(_READ, (self.identity, "reviewed")).fetchone())
        if row is None:
            self._approved = None
            return None
        sealed, shared = bytes(row[0]), self._shared()
        if self.kind == "key" and self._laid_over is not None:
            # What the settings in use were laid over: a copy read since may be newer, and an item it adds would be
            # read as the key's approved own (stricter, kept until the administration saves again).
            shared = self._laid_over
        read = self._snapshot_read
        if self.kind != "key" and read is not None and read[0] == sealed:  # the same row: made settings once
            self._approved = repinned(read[1], shared)
            return self._approved
        found = self._opened("reviewed", sealed)
        try:
            if self.kind == "key":
                self._approved = layered(shared, found) if isinstance(found, dict) else None
                return self._approved
            # The shared items it holds as they are taken as the shared settings hold them: no copy of each.
            approved = validated(reused(found, shared))
        except ValueError:
            self._approved = None
            return None
        self._snapshot_read = (sealed, approved)
        self._approved = repinned(approved, shared)
        return self._approved

    def _snapshot(self) -> Any:
        """The snapshot kept, as its JSON; None when there is none, or when it does not open (it is then replaced)."""
        row = self._db(lambda conn: conn.execute(_READ, (self.identity, "reviewed")).fetchone())
        found: Any = None
        if row is not None:
            with contextlib.suppress(ValueError):
                found = self._opened("reviewed", row[0])
        return found

    def _write_reviewed(
        self, conn: Connection[Any], sealed: bytes, recording: Callable[[Connection[Any]], None] | None = None
    ) -> int:
        conn.execute(_REVIEWED, (self.identity, self.identity, sealed, time.time(), self.identity))
        counter = self._count(conn)
        if recording is not None:  # last: the entry of the change, kept with it or not at all
            try:
                recording(conn)
            except (psycopg.Error, DatabaseError):
                raise  # the database's own: named by ``_db``
            except Exception as exc:  # by its type: its words can quote what was given
                raise SettingsUnrecorded(
                    f"the approval of the settings of {self.identity} could not be recorded ({type(exc).__name__}): "
                    "it was not kept"
                ) from None
        return counter

    def _approved_data(self, config: Config, kept: Any) -> Any:
        """What the snapshot of ``config`` holds: the settings whole for a person; for an API key its own part, worked
        out against the shared settings ``config`` was laid over (a copy read later could be newer: an item they dropped
        meanwhile would become the key's own, a setting they tightened would be kept at its older value), what the
        snapshot ``kept`` set staying its own while it has the value it gave."""
        if self.kind != "key":
            return config.model_dump(mode="json")
        before = kept if isinstance(kept, dict) else self._own()
        return own_part(self._laid_over or self._shared(), config, before=before)

    def save_reviewed(self, config: Config) -> None:
        """Keep ``config`` as the settings approved last; when it differs from the snapshot kept, written in one
        transaction with the identity's change count and then told (``changed``). Raises OSError when it cannot be
        kept, once the own part written for these settings, if any, is told with its own count. The settings this copy
        last read or kept as approved are not written again, nor read: each start and re-application of a space applies
        the approved settings (another instance's approval since is told, and read again, never written over). What
        records the change (``recording``) is written last in the snapshot's transaction, when one is written."""
        recording = self.recording
        if self._approved is not None and self._untold is None and config == self._approved:
            return
        try:
            kept = self._snapshot()
            data = self._approved_data(config, kept)
            sealed = self._sealer.seal_json(data, f"{self.identity}:reviewed")
            self._fits(sealed, "approved settings", self._snapshot_limit())
            if kept == data:
                self._approved = config
                return
            counter = self._db(lambda conn: self._write_reviewed(conn, sealed, recording), transaction=True)
        except OSError as exc:
            refused = exc
        else:
            if recording is not None:  # kept with its snapshot: written once
                self.recording = None
            self._untold = None
            self._approved = config
            self._counted(counter)
            self._changed(counter)
            return
        if self._untold is not None:  # no second count: it could fail as the snapshot did
            counter, self._untold = self._untold, None
            self._changed(counter)
        raise refused
