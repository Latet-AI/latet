"""The Enterprise Edition on a server: its license, its settings and their secrets in PostgreSQL (the tables of
latet.ee.migrations), the same for every instance. An instance keeps a copy for REFRESH_SECONDS, or until it is
dropped: by its own change, or by a message another instance published on Valkey (latet.ee.listener). A change can be
written in a caller's transaction (``conn``), beside the entry that records it: the caller then drops the copy and
tells the others once it commits. psycopg is imported where it is needed, so that this module loads without the
server's extra."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING, Any, Final, Literal

from latet.ee import settings as ee_settings
from latet.ee.license import MAX_BYTES as LICENSE_BYTES
from latet.ee.license import REFRESH_SECONDS, Stored, not_a_license
from latet.ee.secrets import secret_name, value_problem

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.server.audit import AuditLog
    from latet.server.bus import Bus
    from latet.server.db import Database
    from latet.server.master import MasterKey

log = logging.getLogger(__name__)

# document, features (those the installer verified), version, over_since, trials_ended
_Row = tuple[bytes | None, frozenset[str] | None, int, float | None, frozenset[str]]
_NO_ROW: Final[_Row] = (None, None, 0, None, frozenset())


def publish(bus: Bus, channel: str, message: Mapping[str, Any]) -> bool:
    """Tell the other instances: whether the message went out. When Valkey does not answer, they read it again within
    REFRESH_SECONDS. Its failure is logged by its type: the words of one can name Valkey's address."""
    try:
        bus.publish(channel, message)
    except Exception as exc:
        log.warning(
            "could not tell the other instances on %s (%s): they read it again within %g seconds",
            channel,
            type(exc).__name__,
            REFRESH_SECONDS,
        )
        return False
    return True


class Counted:
    """A count asked at most every ``seconds`` (a server's enabled people and API keys): what the license is weighed
    with at each look, without a query at each. ``drop()``: asked again at the next use. A weighing asks it
    (``Licensing._count``) without the license's lock, so that what protects never waits behind a slow count; ``count``
    never calls back into that Licensing."""

    def __init__(
        self,
        count: Callable[[], int],
        *,
        seconds: float = REFRESH_SECONDS,
        ticker: Callable[[], float] = time.monotonic,
    ) -> None:
        self.count = count
        self.seconds = seconds
        self.ticker = ticker
        self._lock = threading.Lock()
        self._value: int | None = None
        self._at = 0.0

    def __call__(self) -> int:
        with self._lock:  # a drop waits for a count under way, so that one read before it is never kept after it
            now = self.ticker()
            if self._value is None or now - self._at >= self.seconds:
                self._value, self._at = self.count(), now
            return self._value

    def drop(self) -> None:
        with self._lock:
            self._value = None


def _fetched(conn: Connection[Any], *, share: bool = False) -> _Row:
    """The license's row as ``conn`` reads it; ``share``: held there (shared) until its transaction ends."""
    if share:
        query = "SELECT document, features, version, over_since, trials_ended FROM ee_license WHERE id = 1 FOR SHARE"
    else:
        query = "SELECT document, features, version, over_since, trials_ended FROM ee_license WHERE id = 1"
    row = conn.execute(query).fetchone()
    if row is None:
        return _NO_ROW
    return (
        None if row[0] is None else bytes(row[0]),
        None if row[1] is None else frozenset(row[1]),
        int(row[2]),
        row[3],
        frozenset(row[4]),
    )


@contextmanager
def _within(db: Database, conn: Connection[Any] | None) -> Iterator[Connection[Any]]:
    """``conn``, a connection in the caller's transaction, or a transaction of its own."""
    if conn is not None:
        yield conn
        return
    with db.transaction() as own:
        yield own


class DatabaseLicenses:
    """The server's license (ee_license, one row): every instance reads the same one. Removing it keeps the row, with
    its over_since, so removing it and installing it again starts no new grace, and its trials_ended, so that a trial
    that ended stays ended for every instance whatever a clock says. Never calls back into a ``Licensing``, which asks
    it without its own lock."""

    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], float] = time.time,
        seconds: float = REFRESH_SECONDS,
        ticker: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.clock = clock
        self.seconds = seconds
        self.ticker = ticker
        self._lock = threading.Lock()
        self._cached = _NO_ROW
        self._at: float | None = None
        self._dropped = 0  # how many times the copy was dropped: a read that a drop overtook is not kept

    def _row(self) -> _Row:
        """(document, features, version, over_since, trials_ended): the copy, or the database's once the copy is
        ``seconds`` old."""
        with self._lock:
            if self._at is not None and self.ticker() - self._at < self.seconds:
                return self._cached
            dropped = self._dropped
        with self.db.connection() as conn:
            fresh = _fetched(conn)
        with self._lock:
            if self._dropped == dropped:  # no change, nor a message, came while it was read: it is the copy
                self._cached, self._at = fresh, self.ticker()
        return fresh

    def drop(self) -> None:
        """Read the database again at the next look."""
        with self._lock:
            self._at = None
            self._dropped += 1

    def held_one(self) -> bool:
        """The last copy this instance read held a license, however old: what the edition says of the License page
        while the database does not answer."""
        with self._lock:
            return self._cached[0] is not None

    def read(self, *, conn: Connection[Any] | None = None) -> Stored | None:
        """The license, its version the row's number and the digest of the document: the number starts again at 1
        when the row is made again (a restore, a schema made again), and another document under a number seen before
        is never taken for the one read then. Given ``conn``, as the caller's transaction finds it, its row held there
        (shared) until that transaction ends, past the copy."""
        document, features, version, _, _ = self._row() if conn is None else _fetched(conn, share=True)
        if document is None:
            return None
        digest = hashlib.sha256(document).hexdigest()
        return Stored(document, "server", ("server", version, digest), features=features)

    def write(
        self, data: bytes, features: frozenset[str] | None = None, *, conn: Connection[Any] | None = None
    ) -> None:
        """Keep ``data`` as the server's license, with the ``features`` the instance installing it verified (None:
        nothing said them). Given ``conn``, in the caller's transaction: the caller drops the copy (``drop``) and tells
        the others once it commits."""
        if len(data) > LICENSE_BYTES:  # a license is about half a kilobyte: what parse_license would refuse to read
            raise not_a_license(f"it is larger than {LICENSE_BYTES // 1024} KB")
        said = None if features is None else sorted(features)
        with _within(self.db, conn) as tx:
            tx.execute(
                "INSERT INTO ee_license (id, document, features, version, installed) VALUES (1, %s, %s, 1, %s) "
                "ON CONFLICT (id) DO UPDATE SET document = EXCLUDED.document, features = EXCLUDED.features, "
                "version = ee_license.version + 1, installed = EXCLUDED.installed",
                (data, said, self.clock()),
            )
        if conn is None:
            self.drop()

    def held(self, conn: Connection[Any]) -> bytes | None:
        """The document as the caller's transaction finds it (``conn``), its row locked until that transaction ends:
        what a removal written there removes, past this instance's copy (another instance may have replaced it, its
        message lost). None when none is held."""
        row = conn.execute("SELECT document FROM ee_license WHERE id = 1 FOR UPDATE").fetchone()
        return None if row is None or row[0] is None else bytes(row[0])

    def delete(self, *, conn: Connection[Any] | None = None) -> bool:
        """Remove the license and the features said with it; whether there was one. Given ``conn``, as ``write``."""
        with _within(self.db, conn) as tx:
            done = tx.execute(
                "UPDATE ee_license SET document = NULL, features = NULL, version = version + 1 "
                "WHERE id = 1 AND document IS NOT NULL"
            ).rowcount
        if conn is None:
            self.drop()
        return done > 0

    def over_since(self, *, conn: Connection[Any] | None = None) -> float | None:
        """Since when more users than bought are counted; given ``conn``, as the caller's transaction finds it."""
        return self._row()[3] if conn is None else _fetched(conn)[3]

    def keep_over_since(self, moment: float | None) -> float | None:
        """Clear it (None), or start it at ``moment`` unless an instance already did: the one kept. A look asks it, and
        never waits for the row: while a change of the license holds it, nothing is kept this time (None), and the next
        look keeps it (the row taken only when no other transaction holds it)."""
        with self.db.transaction() as conn:
            if moment is None:
                conn.execute(
                    "UPDATE ee_license SET over_since = NULL "
                    "WHERE id = (SELECT id FROM ee_license WHERE id = 1 FOR UPDATE SKIP LOCKED)"
                )
                kept = None
            else:
                row = conn.execute(
                    "UPDATE ee_license SET over_since = COALESCE(over_since, %s) "
                    "WHERE id = (SELECT id FROM ee_license WHERE id = 1 FOR UPDATE SKIP LOCKED) RETURNING over_since",
                    (moment,),
                ).fetchone()
                kept = None if row is None else row[0]
        self.drop()
        return kept

    def ended_trials(self, *, conn: Connection[Any] | None = None) -> frozenset[str]:
        """The trials kept as ended; given ``conn``, as the caller's transaction finds them (no second connection)."""
        return self._row()[4] if conn is None else _fetched(conn)[4]

    def keep_trial_ended(self, license_id: str, last_day: date) -> bool:
        """Keep ``license_id`` as a trial that ended, for every instance, only when PostgreSQL's own clock says that
        ``last_day`` has passed (in UTC): an instance whose clock runs ahead ends no trial for the others. Whether it is
        kept. The copy is dropped only when the row changed, or when it lacks a trial the row already keeps: an
        instance whose clock runs ahead does not read the row again at each look. A look asks it, and it never waits
        for the row: while a change of the license holds it, nothing is kept this time (False), and the store is asked
        again after REFRESH_SECONDS."""
        changed = kept = False
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT %(id)s = ANY(trials_ended), (now() AT TIME ZONE 'UTC')::date > %(day)s "
                "FROM ee_license WHERE id = 1 FOR UPDATE SKIP LOCKED",
                {"id": license_id, "day": last_day},
            ).fetchone()
            if row is not None:
                already, passed = bool(row[0]), bool(row[1])
                changed, kept = not already and passed, already or passed
                if changed:
                    conn.execute(
                        "UPDATE ee_license SET trials_ended = array_append(trials_ended, %s) WHERE id = 1",
                        (license_id,),
                    )
        with self._lock:
            stale = kept and license_id not in self._cached[4]
        if changed or stale:
            self.drop()
        return kept


def settings_in(conn: Connection[Any]) -> ee_settings.EnterpriseSettings:
    """The settings the rows of ee_settings make, read through ``conn``; a section a newer Latet left is skipped."""
    rows = conn.execute("SELECT section, value FROM ee_settings").fetchall()
    return ee_settings.settings_from({name: value for name, value in rows}, skip_unknown=True)


class DatabaseSettings:
    """The Enterprise settings of a server: a row for each section an administrator set (ee_settings), the defaults
    for the rest. Each change is recorded in the administration log and published on ee:settings, so every instance
    reads them again."""

    def __init__(
        self,
        db: Database,
        bus: Bus,
        audit: AuditLog,
        *,
        clock: Callable[[], float] = time.time,
        seconds: float = REFRESH_SECONDS,
        ticker: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.bus = bus
        self.audit = audit
        self.clock = clock
        self.seconds = seconds
        self.ticker = ticker
        self._lock = threading.Lock()
        self._cached: ee_settings.EnterpriseSettings | None = None
        self._at = 0.0
        self._dropped = 0  # how many times the copy was dropped: a read that a drop overtook is not kept

    def load(self) -> ee_settings.EnterpriseSettings:
        with self._lock:
            if self._cached is not None and self.ticker() - self._at < self.seconds:
                return self._cached
            dropped = self._dropped
        with self.db.connection() as conn:
            settings = settings_in(conn)
        with self._lock:
            if self._dropped == dropped:  # no change, nor a message, came while they were read: they are the copy
                self._cached, self._at = settings, self.ticker()
        return settings

    def where(self) -> str | None:
        return "server"

    def drop(self) -> None:
        with self._lock:
            self._cached = None
            self._dropped += 1

    def set_section(
        self,
        name: str,
        value: Mapping[str, Any] | None,
        *,
        actor: str,
        address: str | None = None,
        action: str | None = None,
        conn: Connection[Any] | None = None,
    ) -> ee_settings.EnterpriseSettings:
        """Set the section ``name`` (None: back to its defaults), recorded as ``action`` (by default
        `<name>.changed`, its underscores as hyphens) by ``actor`` from ``address``, the change and its entry in one
        transaction. UnknownSection, or EnterpriseSettingsError saying what is wrong in ``value`` (a section larger than
        ``MAX_BYTES`` included): then nothing changes. Given ``conn``, a connection in the caller's transaction,
        only the row is written there: the caller records the entry in it and, once it commits, calls ``changed``."""
        from psycopg.types.json import Jsonb

        if conn is None:
            self.drop()
            current = self.load()
        else:  # read in the caller's transaction: no second connection while it holds one
            current = settings_in(conn)
        settings = ee_settings.with_section(current, name, value)
        kept = ee_settings.section_value(settings, name)
        if value is not None:
            size = len(json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if size > ee_settings.MAX_BYTES:
                raise ee_settings.EnterpriseSettingsError(
                    f"the section {name} holds {ee_settings.MAX_BYTES // 1024} KB at most"
                )
        with _within(self.db, conn) as tx:
            if value is None:
                tx.execute("DELETE FROM ee_settings WHERE section = %s", (name,))
            else:
                tx.execute(
                    "INSERT INTO ee_settings (section, value, changed, changed_by) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (section) DO UPDATE SET value = EXCLUDED.value, changed = EXCLUDED.changed, "
                    "changed_by = EXCLUDED.changed_by",
                    (name, Jsonb(kept), self.clock(), actor),
                )
            if conn is None:  # last in the transaction: the change and its entry are kept together, or neither
                what = action or f"{name.replace('_', '-')}.changed"
                self.audit.record(actor, what, target=name, details={"section": name}, address=address, conn=tx)
        if conn is None:
            self.changed(name)
        return settings

    def changed(self, name: str) -> None:
        """Once a change of the section ``name`` is committed: this instance's copy dropped, and every other instance
        told on ee:settings."""
        self.drop()
        publish(self.bus, "ee:settings", {"section": name})


def _place(name: str, bound: str | None = None) -> str:
    """What a secret is sealed for: its name, so that a sealed value moved to another name does not open; and
    ``bound``, what it may be used for (the client secret: its issuer's origin), so that it opens for nothing else."""
    return f"ee-secret:{name}" if bound is None else f"ee-secret:{name}{chr(0)}{bound}"


class DatabaseSecrets:
    """Secrets of the Enterprise settings on a server (an identity provider's client secret, a SIEM token): sealed
    with the master key in ee_secrets, so that PostgreSQL never holds one in clear. A name and a value are checked by
    latet.ee.secrets: a plain name, and 64 KB of UTF-8 text at most."""

    def __init__(self, db: Database, master: MasterKey, *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.master = master
        self.clock = clock

    def save(
        self, name: str, value: str, *, bound: str | None = None, conn: Connection[Any] | None = None
    ) -> Literal["server"]:
        """Keep ``value`` sealed under ``name``; ``bound``: what it may be used for, which ``load`` must name to open it
        (InvalidTag otherwise). Given ``conn``, in the caller's transaction, beside the entry that records it."""
        name = secret_name(name)
        if (problem := value_problem(value)) is not None:  # raised here: no error of the value goes with it
            raise ValueError(problem)
        sealed = self.master.seal(value.encode("utf-8"), _place(name, bound))
        with _within(self.db, conn) as tx:
            tx.execute(
                "INSERT INTO ee_secrets (name, sealed, changed) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET sealed = EXCLUDED.sealed, changed = EXCLUDED.changed",
                (name, sealed, self.clock()),
            )
        return "server"

    def load(self, name: str, *, bound: str | None = None) -> str | None:
        """The secret ``name``, or None; InvalidTag when it was sealed for another name or another ``bound``."""
        name = secret_name(name)
        with self.db.connection() as conn:
            row = conn.execute("SELECT sealed FROM ee_secrets WHERE name = %s", (name,)).fetchone()
        return None if row is None else self.master.open(bytes(row[0]), _place(name, bound)).decode("utf-8")

    def remove(self, name: str, *, conn: Connection[Any] | None = None) -> bool:
        name = secret_name(name)
        with _within(self.db, conn) as tx:
            done = tx.execute("DELETE FROM ee_secrets WHERE name = %s", (name,)).rowcount
        return done > 0
