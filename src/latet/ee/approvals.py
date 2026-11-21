"""Central approvals (Latet Enterprise): on a Latet server, changes that lower protection, and the Live Reviewer's texts
if the company chooses, decided by its security team (an API key's changes by the people who control the key) instead
of whoever asked.

What waits stays where it waits without Latet Enterprise: in the asking identity's own queue (``gateway/approvals.py``,
behind E0a's ``ApprovalsBackend``: on a server E0b's, the batch sealed in Valkey, the change in PostgreSQL), where its
request waits, on any instance, and where masks are checked against that identity's vault. ``CentralApprovals`` keeps
the company's record of it in PostgreSQL (``ee_approvals``): who asked, who decides, what a change lowers, how it ended
and who decided; never a text or a value, and what names the person's folders and destinations sealed. A decider's
decision is carried out through the asking identity's space on the decider's instance, and E0b's backend wakes the
request waiting on another. A text is seen by the decider as its author sees it, values included, when they open it,
from a session the company's sign-in opened: a company choice, which each person is told about.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, Protocol

from cryptography.exceptions import InvalidTag

from latet.ee.roles import ACTIONS, Role, can
from latet.gateway.approvals import Batch, Change, Changed, Decides, batch_version
from latet.logline import Paced
from latet.server.shared import SharedItemsError  # a save refused for what it holds (R70)
from latet.state import IdentityRef

if TYPE_CHECKING:
    from psycopg import Connection

    from latet.ee.edition import EnterpriseEdition
    from latet.ee.people import People
    from latet.ee.settings import ApprovalsSettings
    from latet.runtime import Runtime
    from latet.server.app import Server
    from latet.server.db import Database
    from latet.server.identities import Identities
    from latet.server.metrics import Metrics
    from latet.server.sealing import Sealer

log = logging.getLogger(__name__)
Runner = Callable[..., Awaitable[Any]]  # the threads a request's work runs in (``Dashboard._in_pool``, E3's Who.run)

Route = Literal["security", "controllers"]
SECURITY: Final = "approvals.decide"  # E3's action of the people who decide for the security team
NAMES_FOR: Final = 10.0  # seconds the names of who decides are kept before they are asked again: for telling only
# The names of who decides a person is told at most, the first in their order: enough to know whom to ask, on every item
# that waits and in /me, whatever the size of the security team (the review of Task 6).
MOST_NAMED: Final = 20
LOST_AFTER: Final = 60.0  # seconds after which a text whose record is gone from Valkey is taken for lost
PRUNE_EVERY: Final = 3600.0  # seconds between two prunings of what ended past the retention (this instance's)
# Rows one transaction of a prune removes at most (Task 5's review, M2): a retention lowered, or a prune that failed for
# a while, never makes one statement that runs past the database's bound, rolled back the same way every hour.
PRUNE_RUN = 5000
# Ended items one identity keeps in the record at most, its latest (the whole-branch security review's L3): one who
# opens and ends items in a loop never fills it for a retention's length, as E0b's SETTLED_KEPT bounds how changes
# ended. The administration log keeps every decision taken on Company approvals all the same.
ENDED_KEPT = 1000
MOST_LISTED: Final = 200  # rows the deciders' queue lists at most; the rest are counted (``more``)
WAITING_READ: Final = 2000  # rows one queue reads at most, already filtered by what the approver sees (R39)
RECONCILE_EVERY: Final = 5.0  # seconds an instance keeps a row's verdict of its queue: one ask a while (R39)
APPROVED_UNAPPLIED: Final = "approved-unapplied"  # approved, and their save refused meanwhile (R41)
# A text approved by a decider and withheld where it was sent: masked then, it was not what they approved (the review
# of Task 10, fix round 2).
APPROVED_CHANGED: Final = "approved-changed"
_CHANGE_KEYS: Final = ("source", "agent", "loosened", "keys")  # what of a change reaches its deciders: never a value
# What the record keeps of an item at most (Task 5's review: it bounds no summary itself): a change's reasons, each cut
# to REASON_CHARS, the rest counted in one more; a code (an agent, a key changed, a text's kind) cut to CODE_CHARS.
MOST_REASONS: Final = 100
REASON_CHARS: Final = 300
CODE_CHARS: Final = 64
MOST_CODES: Final = 64


@dataclass(frozen=True)
class View:
    """What one person may see of the record: their own requests (the identity's, or made by them in a key's space), the
    security team's items when they decide for it, the items of the keys they control, and, for an admin, the
    controllers' items (the edition keeps those of keys nobody else controls)."""

    person: str
    security: bool
    keys: frozenset[str]
    admin: bool

    def sees(self, row: Row) -> bool:
        if row.identity_id == self.person or row.asked_by == self.person:
            return True
        if row.route == "security":
            return self.security
        return row.identity_id in self.keys or self.admin

    def decides(self, row: Row) -> bool:
        return self.sees(row) and row.identity_id != self.person and row.asked_by != self.person


@dataclass(frozen=True)
class Row:
    """One item of the record: what waited for someone else, and how it ended (``state`` None while it waits)."""

    id: str
    identity_id: str  # whose space it waits in
    identity_name: str  # as it was named when it opened: the history keeps it
    identity_kind: Literal["person", "key"]
    kind: Literal["change", "batch"]
    route: Route
    summary: dict[str, Any]  # a change's reasons; a text's agent, kinds and number: sealed in PostgreSQL
    created: float
    state: str | None = None
    decided: float | None = None
    by_id: str | None = None
    by_name: str | None = None
    asked_by: str | None = None  # who made it in an API key's space (E3's latet.acting): an identity's id


class Records(Protocol):
    """The company's record of central approvals: what waits for someone else, and how it ended."""

    def add(self, row: Row) -> None: ...  # the same id again keeps the first
    def end(self, identity_id: str, item_id: str, state: str, decided: float, by: IdentityRef | None) -> bool: ...
    def forget(self, identity_id: str, item_id: str) -> bool: ...  # a row still waiting deleted: it never waited
    def get(self, item_id: str, *, conn: Connection[Any] | None = None) -> Row | None: ...
    def waiting(self, view: View, limit: int) -> list[Row]: ...  # what ``view`` sees, oldest first
    def count(self, view: View) -> int: ...  # what ``view`` decides on, its own left out
    def history(self, view: View, limit: int) -> list[Row]: ...  # what ended that ``view`` sees, newest first
    def counts(self) -> dict[tuple[str, str], int]: ...  # what waits, by kind and route
    # What ended before ``before``, and an identity's past its ENDED_KEPT latest, removed; how many.
    def prune(self, before: float) -> int: ...
    def unapplied(self, identity_id: str, item_id: str) -> bool: ...  # an approval not applied, said so (R60)
    # A text approved, or about to be ended so, withheld where it was sent: ``approved-changed``, kept past a later end.
    def withheld(self, identity_id: str, item_id: str, decided: float) -> bool: ...


def _names_who_approved(was: str | None, named: str | None, state: str, by: IdentityRef | None) -> bool:
    """Whether an end of ``state`` by ``by`` names who approved a text already said ``approved-changed`` with nobody
    named: the instance that sent it heard the decision before the deciding one wrote its end (the review of Task 10,
    fix round 3, m2). That end then counts as the approval it is, once."""
    return was == APPROVED_CHANGED and named is None and state == "approved" and by is not None


class MemoryRecords:
    """``Records`` in memory: for the tests, and nothing else."""

    def __init__(self) -> None:
        self._rows: dict[str, Row] = {}
        self._lock = threading.Lock()

    def add(self, row: Row) -> None:
        with self._lock:
            self._rows.setdefault(row.id, row)

    def end(self, identity_id: str, item_id: str, state: str, decided: float, by: IdentityRef | None) -> bool:
        with self._lock:
            row = self._rows.get(item_id)
            if row is None or row.identity_id != identity_id:
                return False
            if _names_who_approved(row.state, row.by_id, state, by) and by is not None:
                self._rows[item_id] = replace(row, by_id=by.id, by_name=by.name)  # withheld first: the approval named
                return True
            if row.state is not None:
                return False
            by_id, by_name = (by.id, by.name) if by is not None else (None, None)
            self._rows[item_id] = replace(row, state=state, decided=decided, by_id=by_id, by_name=by_name)
            return True

    def forget(self, identity_id: str, item_id: str) -> bool:
        with self._lock:
            row = self._rows.get(item_id)
            if row is None or row.identity_id != identity_id or row.state is not None:
                return False
            del self._rows[item_id]
            return True

    def get(self, item_id: str, *, conn: Connection[Any] | None = None) -> Row | None:
        with self._lock:
            return self._rows.get(item_id)

    def _all(self) -> list[Row]:
        with self._lock:
            return list(self._rows.values())

    def waiting(self, view: View, limit: int) -> list[Row]:
        rows = [row for row in self._all() if row.state is None and view.sees(row)]
        return sorted(rows, key=lambda row: (row.created, row.id))[:limit]  # as PostgreSQL orders them

    def count(self, view: View) -> int:
        return sum(1 for row in self._all() if row.state is None and view.decides(row))

    def history(self, view: View, limit: int) -> list[Row]:
        rows = [row for row in self._all() if row.state is not None and view.sees(row)]
        return sorted(rows, key=lambda row: (-(row.decided or 0.0), row.id))[:limit]  # as PostgreSQL orders them

    def counts(self) -> dict[tuple[str, str], int]:
        return dict(collections.Counter((row.kind, row.route) for row in self._all() if row.state is None))

    def prune(self, before: float) -> int:
        with self._lock:
            old = {key for key, row in self._rows.items() if row.state is not None and (row.decided or 0.0) < before}
            ended: dict[str, list[Row]] = collections.defaultdict(list)
            for key, row in self._rows.items():
                if row.state is not None and key not in old:
                    ended[row.identity_id].append(row)
            for rows in ended.values():  # past ENDED_KEPT of one identity's, its oldest (L3)
                rows.sort(key=lambda row: (row.decided or 0.0, row.id), reverse=True)
                old.update(row.id for row in rows[ENDED_KEPT:])
            for key in old:
                del self._rows[key]
        return len(old)

    def unapplied(self, identity_id: str, item_id: str) -> bool:
        with self._lock:
            row = self._rows.get(item_id)
            if row is None or row.identity_id != identity_id or row.state != "approved":
                return False
            self._rows[item_id] = replace(row, state="approved-unapplied")
            return True

    def withheld(self, identity_id: str, item_id: str, decided: float) -> bool:
        with self._lock:
            row = self._rows.get(item_id)
            if row is None or row.identity_id != identity_id or row.state not in (None, "approved"):
                return False
            self._rows[item_id] = replace(row, state=APPROVED_CHANGED, decided=row.decided or decided)
            return True


_COLUMNS = (
    "id, identity_id, identity_name, identity_kind, kind, route, summary, created, state, decided, by_id, by_name, "
    "asked_by"
)
# What one person sees, as SQL: their own, the security team's for its members, their keys', an admin's the
# controllers'. The parameters: person (twice), security, keys, admin.
_SEES = (
    "(identity_id = %s OR asked_by = %s OR (route = 'security' AND %s) OR (route = 'controllers' AND (identity_id = "
    "ANY(%s) OR %s)))"
)


class PostgresRecords:
    """``Records`` in the server's PostgreSQL (``ee_approvals``), shared by every instance, each row's summary sealed
    (``sealer``: the master key's purpose ``approvals``, the row's id its additional data). psycopg is imported here
    only: it comes with the server's extra."""

    def __init__(self, db: Database, sealer: Sealer) -> None:
        self.db = db
        self.sealer = sealer
        self._unopened = Paced()  # rows that do not open, said once a minute by the error's type

    def _row(self, found: Any) -> Row | None:
        """The row as read, its summary opened; None, said by its type once a minute, for one that does not open (the
        master key's purpose changed, the row damaged): it is left out rather than breaking every read (R66). Its item
        still ends where it waits (``end`` reads no summary)."""
        values = list(found)
        try:
            values[6] = self.sealer.open_json(bytes(values[6]), f"ee_approvals:{values[0]}")
        except (InvalidTag, ValueError) as exc:  # damaged or sealed under another key; a key version not held (R72)
            if count := self._unopened.due():
                log.warning(
                    "a row of the record of approvals does not open (%s): it is left out%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
            return None
        return Row(*values)

    def _rows(self, found: list[Any]) -> list[Row]:
        return [row for row in map(self._row, found) if row is not None]

    @staticmethod
    def _sees(view: View) -> tuple[Any, ...]:
        return (view.person, view.person, view.security, list(view.keys), view.admin)

    def add(self, row: Row) -> None:
        sealed = self.sealer.seal_json(row.summary, f"ee_approvals:{row.id}")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ee_approvals (id, identity_id, identity_name, identity_kind, kind, route, summary, "
                "created, asked_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (row.id, row.identity_id, row.identity_name, row.identity_kind, row.kind, row.route, sealed,
                 row.created, row.asked_by),
            )  # fmt: skip

    def end(self, identity_id: str, item_id: str, state: str, decided: float, by: IdentityRef | None) -> bool:
        by_id, by_name = (by.id, by.name) if by is not None else (None, None)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE ee_approvals SET state = %s, decided = %s, by_id = %s, by_name = %s "
                "WHERE id = %s AND identity_id = %s AND state IS NULL",
                (state, decided, by_id, by_name, item_id, identity_id),
            )
            if cursor.rowcount == 1:
                return True
            if not _names_who_approved(APPROVED_CHANGED, None, state, by):
                return False
            # Withheld where it was sent before this approval's end was written (m2): who approved it named, once.
            named = conn.execute(
                "UPDATE ee_approvals SET by_id = %s, by_name = %s "
                "WHERE id = %s AND identity_id = %s AND state = %s AND by_id IS NULL",
                (by_id, by_name, item_id, identity_id, APPROVED_CHANGED),
            )
            return named.rowcount == 1

    def forget(self, identity_id: str, item_id: str) -> bool:
        with self.db.transaction() as conn:
            done = conn.execute(
                "DELETE FROM ee_approvals WHERE id = %s AND identity_id = %s AND state IS NULL", (item_id, identity_id)
            )
            return done.rowcount == 1

    def get(self, item_id: str, *, conn: Connection[Any] | None = None) -> Row | None:
        query = f"SELECT {_COLUMNS} FROM ee_approvals WHERE id = %s"  # noqa: S608 - the columns are this module's
        if conn is not None:  # the decision's transaction: no second connection while it holds one
            found = conn.execute(query, (item_id,)).fetchone()
        else:
            with self.db.connection() as own:
                found = own.execute(query, (item_id,)).fetchone()
        return self._row(found) if found is not None else None  # one that does not open: gone to a decision

    def waiting(self, view: View, limit: int) -> list[Row]:
        query = f"SELECT {_COLUMNS} FROM ee_approvals WHERE state IS NULL AND {_SEES} ORDER BY created, id LIMIT %s"  # noqa: S608
        with self.db.connection() as conn:
            return self._rows(conn.execute(query, (*self._sees(view), limit)).fetchall())

    def count(self, view: View) -> int:
        query = (
            f"SELECT count(*) FROM ee_approvals WHERE state IS NULL AND {_SEES} "  # noqa: S608
            "AND identity_id <> %s AND asked_by IS DISTINCT FROM %s"
        )
        with self.db.connection() as conn:
            row = conn.execute(query, (*self._sees(view), view.person, view.person)).fetchone()
        return int(row[0]) if row is not None else 0

    def history(self, view: View, limit: int) -> list[Row]:
        """Never prepared (Task 8's check of the plans): psycopg prepares a statement run five times on a connection,
        and PostgreSQL may then plan it once for every reader (a generic plan), which read a controller's history back
        to the retention's end after an admin's reads. Unprepared, each read is planned for whoever reads it, and
        every arm of what one sees has an index (Task 5's review, M1)."""
        query = (
            f"SELECT {_COLUMNS} FROM ee_approvals WHERE state IS NOT NULL AND {_SEES} "  # noqa: S608
            "ORDER BY decided DESC, id LIMIT %s"  # the same rows in the same order at each read (Task 5's review, M3)
        )
        with self.db.connection() as conn:
            return self._rows(conn.execute(query, (*self._sees(view), limit), prepare=False).fetchall())

    def counts(self) -> dict[tuple[str, str], int]:
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT kind, route, count(*) FROM ee_approvals WHERE state IS NULL GROUP BY kind, route"
            ).fetchall()
        return {(kind, route): int(count) for kind, route, count in rows}

    def prune(self, before: float) -> int:
        """What ended before ``before``, removed oldest first in runs of PRUNE_RUN rows at most, each in a transaction
        of its own (as the administration log's prune and E4's do), until a run takes fewer; then, of each identity
        that keeps more than ENDED_KEPT ended items, the oldest past them, in runs alike (L3): how many. A prune stopped
        half way keeps what it removed, and the next one goes on from there. What waits is never removed."""
        removed = 0
        while True:
            with self.db.transaction() as conn:
                done = conn.execute(
                    "DELETE FROM ee_approvals WHERE id IN (SELECT id FROM ee_approvals WHERE state IS NOT NULL "
                    "AND decided < %s ORDER BY decided LIMIT %s)",
                    (before, PRUNE_RUN),
                ).rowcount
            removed += done
            if done < PRUNE_RUN:
                break
        with self.db.connection() as conn:  # who keeps too many: one read of the index of what ended, by identity
            over = conn.execute(
                "SELECT identity_id FROM ee_approvals WHERE state IS NOT NULL GROUP BY identity_id "
                "HAVING count(*) > %s",
                (ENDED_KEPT,),
            ).fetchall()
        for (identity_id,) in over:
            while True:
                with self.db.transaction() as conn:
                    done = conn.execute(
                        "DELETE FROM ee_approvals WHERE id IN (SELECT id FROM ee_approvals WHERE identity_id = %s "
                        "AND state IS NOT NULL ORDER BY decided DESC, id DESC OFFSET %s LIMIT %s)",
                        (identity_id, ENDED_KEPT, PRUNE_RUN),
                    ).rowcount
                removed += done
                if done < PRUNE_RUN:
                    break
        return removed

    def unapplied(self, identity_id: str, item_id: str) -> bool:
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE ee_approvals SET state = 'approved-unapplied' WHERE id = %s AND identity_id = %s "
                "AND state = 'approved'",
                (item_id, identity_id),
            )
            return done.rowcount == 1

    def withheld(self, identity_id: str, item_id: str, decided: float) -> bool:
        """``approved-changed`` over ``approved``, or over a row not ended yet: the instance that sends a text may hear
        its decision before the deciding one writes its end, which then finds it ended and keeps it (``end``)."""
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE ee_approvals SET state = %s, decided = COALESCE(decided, %s) "
                "WHERE id = %s AND identity_id = %s AND (state IS NULL OR state = 'approved')",
                (APPROVED_CHANGED, decided, item_id, identity_id),
            )
            return done.rowcount == 1


class Directory(Protocol):
    """Who is who on the server, as central approvals ask it: E0b's identities, E3's groups, roles and the control of
    API keys. Only enabled identities count. Given ``conn``, as the decision's transaction finds them, past every copy;
    else E3's copies (each read checked against its generation in Valkey)."""

    def identity(self, identity_id: str) -> IdentityRef | None: ...
    def holding(self, role: Role, *, conn: Connection[Any] | None = None) -> list[IdentityRef]: ...
    def roles(self, identity_id: str, *, conn: Connection[Any] | None = None) -> frozenset[Role] | None: ...
    def controllers(self, key_id: str, *, conn: Connection[Any] | None = None) -> list[IdentityRef]: ...
    def controlled(self, identity_id: str) -> list[IdentityRef]: ...
    def group_of(self, identity_id: str) -> str | None: ...


def own(row: Row, identity_id: str) -> bool:
    """Whether ``identity_id`` asked for it: the identity whose space it waits in, or who made it in a key's space
    (``Row.asked_by``, a column of its own: the summary is sealed)."""
    return row.identity_id == identity_id or row.asked_by == identity_id


def _uncounted(kind: str, state: str) -> None:
    return None


def _cut(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _middle_cut(text: Any, limit: int) -> str:
    """``text`` cut in its middle to ``limit`` characters: a reason starts with what it is about (folders, a
    destination's name) and ends with what it lowers, both kept (the review of Task 6, M4)."""
    text = str(text)
    if len(text) <= limit:
        return text
    tail = (limit - 1) // 2
    return text[: limit - 1 - tail] + "…" + text[len(text) - tail :]


def _reasons(lines: list[str]) -> list[str]:
    """A change's reasons as the record keeps them: MOST_REASONS at most, each cut in its middle to REASON_CHARS, then
    how many more (Latet's own lines, which name a project or a destination the person chose: bounded, never dropped
    unsaid)."""
    kept = [_middle_cut(line, REASON_CHARS) for line in lines[:MOST_REASONS]]
    if len(lines) > MOST_REASONS:
        kept.append(f"{len(lines) - MOST_REASONS} more")
    return kept


def _codes(values: list[Any]) -> list[str]:
    return [_cut(value, CODE_CHARS) for value in values[:MOST_CODES]]


def _code(value: Any) -> str | None:
    return _cut(value, CODE_CHARS) if value is not None else None


class CentralApprovals:
    """The company's side of approvals on a Latet server (``Central`` for every space): where each item goes, who
    decides, the record, and the deciders' queue and decisions. Its methods that read PostgreSQL or People are
    synchronous (in the dashboard's threads, or a space's worker thread); those that reach a space are async."""

    def __init__(
        self,
        records: Records,
        directory: Directory,
        *,
        settings: Callable[[], ApprovalsSettings],
        licensed: Callable[[], bool],
        space: Callable[[str, Runner, bool], Awaitable[Runtime | None]],
        waiting_batches: Callable[[list[tuple[str, str]]], set[str]],
        ended_changes: Callable[[list[tuple[str, str]]], dict[str, str]],
        clock: Callable[[], float] = time.time,
        keep_days: int = 365,
    ) -> None:
        self.records = records
        self.directory = directory
        self.settings = settings
        self.licensed = licensed
        self.space = space  # an identity's runtime on this instance, by its id; None when it is gone or disabled
        self.waiting_batches = waiting_batches
        self.ended_changes = ended_changes
        self.clock = clock
        self.keep_days = keep_days  # the server's audit_retention_days (R18)
        self.counted: Callable[[str, str], None] = _uncounted  # what ended through this instance (Task 8's metrics)
        self._names: dict[str, tuple[float, list[IdentityRef]]] = {}
        # Each row's last verdict of its queue on this instance, and when (R39): True waiting, else how it ended; and
        # since when this instance has found it not waiting (None while it waits): its end is written after a while.
        self._checked: dict[str, tuple[float, bool | str, float | None]] = {}
        self._lock = threading.Lock()
        self._pruned = float("-inf")

    # --- where an item goes, and who decides on it ----------------------------------------------------------------

    def changes_go_to(self, identity: IdentityRef | None) -> Decides:
        """Where a change that lowers protection in this identity's space goes: to the security team when the company
        says so (an API key's too); else an API key's to the people who control it [D2], a person's to the person. Only
        with a license that lists approvals (expired, paused or after a trial too: approvals keep their route)."""
        if identity is None or not self.licensed():
            return "self"
        if self.settings().changes == "security":
            return "security"
        return "controllers" if identity.kind == "key" else "self"

    def live_review_goes_to(self, identity: IdentityRef | None) -> Literal["self", "security"]:
        """Where the Live Reviewer's texts of this identity's space go: the server masks, so they are on it already."""
        if identity is None or not self.licensed():
            return "self"
        return self.settings().live_review

    def deciders(self, identity: IdentityRef | None, decides: Decides, asked_by: str | None = None) -> list[str]:
        """The names of who decides on an item of this identity's, as whoever waits is told them: never whoever asked
        (the identity, or ``asked_by``, who made a change in an API key's space); MOST_NAMED at most, the first in
        their order."""
        return self._named(identity, decides, asked_by)[0]

    def _named(self, identity: IdentityRef | None, decides: Decides, asked_by: str | None) -> tuple[list[str], int]:
        """``deciders``, and how many more there are past MOST_NAMED (the review of Task 7, m7)."""
        asking = {identity.id if identity is not None else None, asked_by}
        names = sorted({person.name for person in self._who(identity, decides, asked_by) if person.id not in asking})
        return names[:MOST_NAMED], max(0, len(names) - MOST_NAMED)

    def _who(self, identity: IdentityRef | None, decides: Decides, asked_by: str | None) -> list[IdentityRef]:
        if decides == "security":
            return self._kept("security", self._deciding)
        if decides == "controllers" and identity is not None:
            others = [person for person in self.directory.controllers(identity.id) if person.id != asked_by]
            return others or self._kept("admins", lambda: self.directory.holding(Role.ADMIN))
        return []

    def _deciding(self, conn: Connection[Any] | None = None) -> list[IdentityRef]:
        """Who holds a role that allows ``approvals.decide`` (E3's ACTIONS: the security role)."""
        found = {person.id: person for role in ACTIONS[SECURITY] for person in self.directory.holding(role, conn=conn)}
        return list(found.values())

    def _kept(self, key: str, read: Callable[[], list[IdentityRef]]) -> list[IdentityRef]:
        """Names asked again after NAMES_FOR seconds: they are for telling, never for deciding who may decide."""
        now = self.clock()
        with self._lock:
            found = self._names.get(key)
        if found is not None and now - found[0] < NAMES_FOR:
            return found[1]
        people = read()
        with self._lock:
            self._names[key] = (now, people)
        return people

    def me(self, identity: IdentityRef | None, acting: str | None = None) -> dict[str, Any]:
        """What ``/me`` says of central approvals in this identity's space: where its changes and texts go, and who
        decides on each. ``acting``: in an API key's space, the person working there, whose changes there wait for
        someone else (``asked_by``): never named among who decides on them (the review of Task 6). A text is the key's
        agent's, never theirs: its deciders are all of the security team. Each list of names with how many more there
        are past MOST_NAMED (``deciders_more``, ``text_deciders_more``: the review of Task 7, m7)."""
        changes, texts = self.changes_go_to(identity), self.live_review_goes_to(identity)
        deciders, more = self._named(identity, changes, acting) if changes != "self" else ([], 0)
        text_deciders, text_more = self._named(identity, texts, None) if texts != "self" else ([], 0)
        return {
            "changes": changes,
            "live_review": texts,
            "deciders": deciders,
            "deciders_more": more,
            "text_deciders": text_deciders,
            "text_deciders_more": text_more,
        }

    def decides_anything(self, identity: IdentityRef | None) -> bool:
        """Whether the Company approvals page is this person's: the security team, a key's controller, an admin."""
        if identity is None or identity.kind != "person" or not self.licensed():
            return False
        roles = self.directory.roles(identity.id)
        if roles is None:
            return False
        return can(roles, SECURITY) or Role.ADMIN in roles or bool(self.directory.controlled(identity.id))

    def decider(self, identity_id: str) -> IdentityRef | None:
        """The person ``identity_id`` when the Company approvals page is theirs; None otherwise."""
        identity = self.directory.identity(identity_id)
        return identity if identity is not None and self.decides_anything(identity) else None

    # --- the spaces' side (``Central``) ---------------------------------------------------------------------------

    def opened(self, identity: IdentityRef | None, item: Change | Batch) -> None:
        """An item that waits for someone else opened in this identity's space: into the record, before anyone sees it
        (raising, it does not open: protection never drops). Its summary is sealed by the record (R40), and holds a
        change's reasons alone (never its guidance, its destinations, its id or who decides) and a text's agent, kinds
        and number, each bounded (Task 5's review)."""
        if identity is None or item.decides == "self":
            return
        asked_by = None
        if isinstance(item, Change):
            summary = item.summary()
            kind, kept = "change", {key: summary[key] for key in _CHANGE_KEYS}
            kept.update(agent=_code(kept["agent"]), loosened=_reasons(kept["loosened"]), keys=_codes(kept["keys"]))
            if item.asked_by is not None:  # made in an API key's space by one of the people working for it (E3)
                asker = self.directory.identity(item.asked_by)
                kept["asked_by"] = {"id": item.asked_by, "name": asker.name if asker is not None else None}
                asked_by = item.asked_by
        else:
            kinds = sorted({str(entry.get("kind")) for entry in item.items})
            kind, kept = "batch", {"agent": _code(item.agent), "kinds": _codes(kinds), "items": len(item.items)}
        route: Route = "controllers" if item.decides == "controllers" else "security"
        row = Row(
            item.id, identity.id, identity.name, identity.kind, kind, route, kept, self.clock(), asked_by=asked_by
        )
        self.records.add(row)  # no pruning here, under the asking runtime's lock: ``history`` prunes (R52)

    def ended(
        self, identity: IdentityRef | None, item_id: str, kind: Literal["change", "batch"], state: str,
        by: IdentityRef | None, stopped_by: str | None = None,
    ) -> None:  # fmt: skip
        """How it ended, with who decided: ``withdrawn`` when whoever asked stopped it where it waits. Stopped in an
        API key's space by someone working there who did not ask for it (``stopped_by``, whom the runtime names only
        then), it is ``rejected`` by them (the whole-branch review's m1), as the administration log's
        ``key.space.changed`` says it."""
        if identity is None:
            return
        if by is None and state == "rejected":
            if stopped_by is None:
                state = "withdrawn"
            else:  # read by id: a person disabled or removed since leaves the rejection with nobody named
                by = self.directory.identity(stopped_by)
        if self.records.end(identity.id, item_id, state, self.clock(), by):
            self.counted(kind, state)

    def dropped(self, identity: IdentityRef | None, item_id: str) -> None:
        """An item recorded that never waited (an equivalent change waited already; the store refused a text)."""
        if identity is not None:
            self.records.forget(identity.id, item_id)

    def withheld(self, identity: IdentityRef | None, item_id: str) -> None:
        """A text a decider approved, withheld where it was sent (masked then, it was not what they approved: the review
        of Task 10, fix round 2): ``approved-changed`` in the record, counted so (``approved`` counted it already when
        it ended, as ``approved-unapplied`` is counted among the approved). Who approved it stays named."""
        if identity is not None and self.records.withheld(identity.id, item_id, self.clock()):
            self.counted("batch", APPROVED_CHANGED)

    def _end(self, row: Row, state: str, by: IdentityRef | None = None) -> None:
        if self.records.end(row.identity_id, row.id, state, self.clock(), by):
            self.counted(row.kind, state)

    def _unapplied(self, row: Row) -> None:
        """R60 (the plan re-review's RR-L1): an approval its space did not take ends the record as what it is
        (``approved-unapplied``), and is counted so; ``approved`` counted it already, when it ended (the documentation
        says the second counts those among the first). Its ``change`` event says ``approved``: the decision taken."""
        if self.records.unapplied(row.identity_id, row.id):
            self.counted(row.kind, APPROVED_UNAPPLIED)

    def prune_now(self) -> int:
        """What ended more than ``keep_days`` ago removed from the record, now, and each identity's ended items past
        its ENDED_KEPT latest (L3): how many (R73: E4's jobs' thread, once an hour on one instance at a time)."""
        return self.records.prune(self.clock() - self.keep_days * 86400.0)

    def _prune(self) -> None:
        """``prune_now`` once PRUNE_EVERY has passed since this instance's last (the history's and the metrics' way to
        the retention, R52, R63)."""
        now = self.clock()
        with self._lock:
            if now - self._pruned < PRUNE_EVERY:
                return
            self._pruned = now
        self.prune_now()

    # --- who may decide: read past every copy at each decision ----------------------------------------------------

    def _capacity(self, approver_id: str, row: Row, *, conn: Connection[Any] | None) -> str | None:
        """What ``approver_id`` decides on ``row`` as: ``security`` (the security team's items), ``controller`` (an
        API key's, one of the people who control it but whoever made it), ``admin`` (an API key's nobody else
        controls); None when not theirs to decide. Read from the row and who is who now, never from the names the
        item was told (its ``deciders``: for telling)."""
        roles = self.directory.roles(approver_id, conn=conn)
        if roles is None:
            return None  # disabled, removed, or an API key: deciders are people
        if row.route == "security":
            return "security" if can(roles, SECURITY) else None
        others = [p.id for p in self.directory.controllers(row.identity_id, conn=conn) if p.id != row.asked_by]
        if approver_id in others:
            return "controller"
        return "admin" if not others and Role.ADMIN in roles else None

    def admitted(
        self, conn: Connection[Any] | None, approver_id: str, item_id: str, kind: str, approve: bool | None
    ) -> tuple[Row | None, str]:
        """The row a decision (``approve`` True or False) or a read (None) is about, with the capacity it is taken in
        (``own``: whoever asked, who only withdraws; ``security``, ``controller``, ``admin``); or None and why not:
        ``gone``, ``not-yours``, ``yours`` (nobody approves their own). ``conn``: the decision's transaction."""
        row = self.records.get(item_id, conn=conn)
        if row is None or row.kind != kind or row.state is not None:
            return None, "gone"
        if own(row, approver_id):
            return (None, "yours") if approve else (row, "own")
        capacity = self._capacity(approver_id, row, conn=conn)
        return (row, capacity) if capacity is not None else (None, "not-yours")

    # --- the deciders' side ---------------------------------------------------------------------------------------

    def asked(self, row: Row, mine: bool, groups: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Who asked for ``row``, as a decider's list says it; ``groups``: each identity's group, read once for a whole
        list (the review of Task 6, M6), else read here."""
        group = groups[row.identity_id] if groups is not None else self.directory.group_of(row.identity_id)
        return {
            "id": row.id,
            "identity": {"id": row.identity_id, "name": row.identity_name, "kind": row.identity_kind},
            "group": group,
            "created": row.created,
            "mine": mine,
            "route": row.route,
        }

    def view(self, approver: IdentityRef) -> View:
        """What ``approver`` may see of the record, from E3's copies (a decision reads past them): R39."""
        roles = self.directory.roles(approver.id) or frozenset()
        keys = frozenset(key.id for key in self.directory.controlled(approver.id))
        return View(approver.id, can(roles, SECURITY), keys, Role.ADMIN in roles)

    def _seen(self, view: View, rows: list[Row]) -> list[Row]:
        """Of ``rows`` (filtered in the database), an admin's controllers' items kept only where nobody else controls
        the key than whoever made the item: one read of each key's controllers, never one a row, weighed for each row
        by who made it (two items of one key, made by two people, may be an admin's and a controller's). What the admin
        decided stays theirs to see, the key's controllers since then aside (the review of Task 6, I2)."""
        if not view.admin:
            return rows
        controllers: dict[str, list[str]] = {}
        out = []
        for row in rows:
            mine = own(row, view.person) or row.by_id == view.person
            if row.route == "controllers" and not mine and row.identity_id not in view.keys:
                if row.identity_id not in controllers:
                    controllers[row.identity_id] = [p.id for p in self.directory.controllers(row.identity_id)]
                if [person for person in controllers[row.identity_id] if person != row.asked_by]:
                    continue
            out.append(row)
        return out

    def _reconciled(self, rows: list[Row]) -> list[Row]:
        """What is confirmed waiting of ``rows`` (R34, R35, R39). Each row's queue is asked once every RECONCILE_EVERY
        seconds an instance, all of them in one query (the changes) and one round trip (the texts), across identities.
        A removed or disabled identity's ends ``gone`` at once. Any other item its queue no longer holds is hidden at
        once and ended only once its queue has said so for RECONCILE_EVERY seconds, at two of its checks (the review
        of Task 6, I1): its own end, with who decided and how it ended (``withdrawn``, ``expired``), reaches the record
        a moment after its queue lets it go, and comes first (``Records.end`` keeps the first). Then a change whose row
        ended ends as it ended; a text whose record is not in Valkey, or a change with no row yet, ends ``abandoned`` or
        ``gone`` once older than LOST_AFTER as well: the record is written before the item is kept, so a young one may
        not be there yet."""
        now, unasked = self.clock(), (float("-inf"), True, None)
        with self._lock:
            due = [row for row in rows if now - self._checked.get(row.id, unasked)[0] >= RECONCILE_EVERY]
        if due:
            identities = {row.identity_id for row in due}
            gone_identities = {found for found in identities if self.directory.identity(found) is None}
            changes = [(row.identity_id, row.id) for row in due if row.kind == "change"]
            texts = [(row.identity_id, row.id) for row in due if row.kind == "batch"]
            ended = self.ended_changes(changes) if changes else {}
            alive = self.waiting_batches(texts) if texts else set()
            verdicts: dict[str, bool | str] = {}
            for row in due:
                if row.identity_id in gone_identities:
                    verdict: bool | str = "identity-gone"
                elif row.kind == "change":
                    verdict = ended.get(row.id, True)
                else:
                    verdict = row.id in alive or "abandoned"
                verdicts[row.id] = verdict
            with self._lock:
                for row_id, verdict in verdicts.items():
                    before = self._checked.get(row_id)
                    # Since when it has been found so: a verdict that changed (missing, then ended) starts again, so
                    # that its own end comes first again (the re-review of Task 6, m1).
                    since = now
                    if before is not None and before[2] is not None and before[1] == verdict:
                        since = before[2]
                    self._checked[row_id] = (now, verdict, None if verdict is True else since)
                for key in [key for key, (when, _, _) in self._checked.items() if now - when > 10 * LOST_AFTER]:
                    del self._checked[key]  # what no queue asked of for long: forgotten
        kept: list[Row] = []
        for row in rows:
            with self._lock:
                _, verdict, since = self._checked.get(row.id, (now, True, None))
            if verdict is True:
                kept.append(row)
            elif verdict == "identity-gone":  # removed or disabled: nobody carries a decision out
                self._end(row, "gone")
            elif since is None or now - since < RECONCILE_EVERY:
                continue  # hidden: its own end, with who ended it, is on its way
            elif verdict in ("gone", "abandoned"):
                if now - row.created > LOST_AFTER:  # never there, or lost (Valkey restarted, its end not written)
                    self._end(row, str(verdict))
            else:
                self._end(row, str(verdict))  # the change ended in its queue while the record did not hear it
        return kept

    def queue(self, approver: IdentityRef) -> dict[str, Any]:
        """What waits for ``approver`` to decide, oldest first, and their own requests that wait for someone else
        (``mine``): the changes (what they lower, never a value) and the texts (their agent, kinds and number: a text
        itself is read when opened, ``batch``). Filtered in the database by what they see, each item confirmed waiting
        in its queue (``_reconciled``). In the dashboard's threads; no space is built; asked only while the Company
        approvals page is open (the menu's count is ``count``).

        ``more``: what waits for them to decide (their own left out) beyond what is listed (the review of Task 6, M5).
        Past one read, the database counts it: what a queue no longer holds is among it until a read ends it, and, for
        an admin, every key's controllers' items (R68: the lone-key filter is not built in SQL)."""
        view = self.view(approver)
        read = self.records.waiting(view, WAITING_READ)
        rows = self._reconciled(self._seen(view, read))
        listed = rows[:MOST_LISTED]
        groups = {identity_id: self.directory.group_of(identity_id) for identity_id in {r.identity_id for r in listed}}
        changes: list[dict[str, Any]] = []
        batches: list[dict[str, Any]] = []
        for row in listed:
            target = changes if row.kind == "change" else batches
            target.append({**row.summary, **self.asked(row, own(row, approver.id), groups)})
        theirs = sum(1 for row in listed if not own(row, approver.id))  # what is listed that they decide on
        if len(read) < WAITING_READ:
            more = sum(1 for row in rows[MOST_LISTED:] if not own(row, approver.id))
        else:  # past one read, counted in the database (never starved: the read is the approver's own)
            more = max(0, self.records.count(view) - theirs)
        return {"changes": changes, "batches": batches, "more": more}

    def count(self, approver: IdentityRef) -> int:
        """What waits for ``approver`` to decide (their own left out): the menu's count, one query, no queue asked
        (what a queue lost is counted until the page's read ends it). An admin's reads the rows once, for the
        lone-key filter: the admins are few, and the database's count would name keys others control. That read takes
        at most WAITING_READ controllers' rows before the filter, so past 2000 of them waiting an admin's list and
        count stop there (R68: accepted for the few admins; a filter of lone keys in SQL is not built)."""
        view = self.view(approver)
        if not view.admin:
            return self.records.count(view)
        return sum(1 for row in self._seen(view, self.records.waiting(view, WAITING_READ)) if not own(row, view.person))

    def history(self, approver: IdentityRef, limit: int = 50) -> list[dict[str, Any]]:
        """What ended lately that ``approver`` decides on, or asked for, newest first: who asked, who decided, when;
        never a text. It also prunes what ended past the retention, once an hour on this instance (R52). An admin's is
        read WAITING_READ rows deep, then filtered (``_seen``), then cut to ``limit``, as ``count`` reads: the keys
        other people control, which the database's filter lets through, never fill it (the review of Task 6, I2)."""
        self._prune()
        view = self.view(approver)
        limit = max(1, min(limit, 500))
        out: list[dict[str, Any]] = []
        for row in self._seen(view, self.records.history(view, WAITING_READ if view.admin else limit))[:limit]:
            by = {"id": row.by_id, "name": row.by_name} if row.by_id else None
            asker = {"id": row.identity_id, "name": row.identity_name, "kind": row.identity_kind}
            out.append({
                **row.summary, "id": row.id, "kind": row.kind, "route": row.route, "identity": asker,
                "created": row.created, "decided": row.decided, "state": row.state, "by": by,
            })  # fmt: skip
        return out

    async def _runtime(self, row: Row, run: Runner, *, fresh: bool = False) -> Runtime | None:
        """The asking identity's runtime on this instance (built there if it is not; with ``fresh``, its settings read
        again first, R41), or None when it is gone: then the record ends it ``gone``."""
        runtime = await self.space(row.identity_id, run, fresh)
        if runtime is None:
            await run(self._end, row, "gone")
        return runtime

    async def _on_its_loop(self, runtime: Runtime, work: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """``work`` on the event loop the space's queue lives on: this one on a server (one loop an instance); another
        in a test whose two clients share a server."""
        loop = runtime.mediator.loop
        if loop is None or loop is asyncio.get_running_loop():
            return await work()
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work(), loop))

    async def batch(self, row: Row, run: Runner) -> Batch | None:
        """The text of ``row`` (admitted), as its author sees it, read through the asking identity's queue; None when
        it no longer waits."""
        runtime = await self._runtime(row, run)
        if runtime is None:
            return None
        backend = runtime.mediator.approvals.backend
        return await self._on_its_loop(runtime, lambda: backend.fetch(row.id))

    async def check(
        self, row: Row, masks: list[dict[str, Any]], images: list[dict[str, Any]], run: Runner,
        version: str | None = None,
    ) -> None:  # fmt: skip
        """The masks and images a decider sends, checked as the asking identity's queue checks them (``MaskError``),
        and the text as they read it (``version``: ``Changed`` once it was masked again since), before the decision is
        recorded."""
        runtime = await self._runtime(row, run)
        if runtime is None:
            return
        approvals = runtime.mediator.approvals
        found = await self._on_its_loop(runtime, lambda: approvals.backend.fetch(row.id))
        if found is not None:
            if version is not None and batch_version(found.items) != version:
                raise Changed("it was masked again since it was read: read it again, then decide")
            approvals._masks(found, masks, in_text=True)  # a decider's masks: values the text holds (R45)
            approvals._images(found, images)

    async def image(self, row: Row, item: int, sent: bool, run: Runner) -> tuple[str, str] | None:
        runtime = await self._runtime(row, run)
        if runtime is None:
            return None
        approvals = runtime.mediator.approvals
        return await self._on_its_loop(runtime, lambda: approvals.image(row.id, item, sent))

    async def carry_change(self, row: Row, approve: bool, by: IdentityRef | None, *, run: Runner) -> str:
        """A decision on a change, carried out in the asking identity's space on this instance, its settings read again
        first (R41): ``approved``, ``rejected``, ``stale``, ``gone`` (the runtime's answers), or ``approved-unapplied``
        when it ended approved and the save was then refused (another instance wrote the settings since: they ask
        again). Raises what ``Runtime.decide_change`` raises otherwise (``BelowFloor`` for an approval below the floor
        of now, then rejected; ``FloorUnavailable``). ``by``: who decided, given only by whoever admitted the decision
        from the record (``admitted``), None for whoever asked (Task 1's review, C5). A change that ended meanwhile
        otherwise (another decider's decision on another instance, a newer change) is ``gone`` to this one, never its
        outcome told as theirs (the review of Task 6's carry: ``decide_change`` answers how it ended)."""
        runtime = await self._runtime(row, run, fresh=True)
        if runtime is None:
            return "gone"
        try:
            state = await run(runtime.decide_change, row.id, approve, by)
        except (OSError, SharedItemsError):  # the save refused (the store, or what it holds: R70): nothing applied
            settled = runtime.mediator.approvals.settled  # PostgreSQL on a server: read in the threads (M1)
            ended = await run(settled.get, row.id)
            if approve and ended == "approved":
                await run(self._unapplied, row)  # R60: the record and the metrics say what it is
                return APPROVED_UNAPPLIED
            # The settings in use written back after a rejection, the decider's or the floor's of an approval (the
            # review of Task 7, m2): the rejection stands, and is said as it stands. Only this decision's own end comes
            # before that save: another decider's would have answered without it.
            if ended == "rejected":
                return "rejected"
            raise
        if state == "gone":  # it ended where its end could not be written
            await run(self._end, row, "gone")
        elif state not in (("approved", "stale") if approve else ("rejected",)) and state != "not-yours":
            return "gone"  # how someone else's end ended it: theirs, in the record and the log
        return state

    async def carry_batch(
        self, row: Row, approve: bool, masks: list[dict[str, Any]], images: list[dict[str, Any]],
        by: IdentityRef | None, *, run: Runner, version: str | None = None,
    ) -> str:  # fmt: skip
        """A decision on a text: ``approved``, ``rejected`` or ``gone``. Carried out through the asking identity's queue
        on this instance (E0b's backend wakes the request waiting on another); the record is ended here and then, so
        that a second decider is never admitted to it (R54: the runtime's end comes later, and finds it ended). An
        approval of the text as it was read (``version``) raises ``Changed`` once it was masked again since, nothing
        decided."""
        runtime = await self._runtime(row, run)
        if runtime is None:
            return "gone"
        approvals = runtime.mediator.approvals
        found = await self._on_its_loop(
            runtime, lambda: approvals.decide(row.id, approve, masks, images, by=by, version=version)
        )
        if not found:
            return "gone"
        state = "approved" if approve else "rejected"
        await run(self._end, row, "withdrawn" if by is None and not approve else state, by)
        return state


KINDS: Final = ("change", "batch")
ROUTES: Final = ("security", "controllers")
STATES: Final = (
    "approved", "rejected", "withdrawn", "stale", "superseded", "expired", "abandoned", "gone",
    "approved-unapplied",  # among the approved: their space did not take it (R60)
    "approved-changed",  # among the approved: withheld where it was sent, masked otherwise (fix round 2)
)  # fmt: skip
WAITING_EVERY: Final = 30.0  # seconds between two reads of what waits (one GROUP BY), in the metrics' own threads
_UNPRUNED = Paced()  # a prune that failed in the metrics' thread: said once a minute


class ApprovalMetrics:
    """E5's metrics in E0c's registry (``Server.metrics``), as E4's ``ServerMetrics`` registers its own: once a
    registry (``of``), bound to the central approvals of the edition made last on this server. What waits is read from
    the record (every instance says the same: take the largest), what ended is counted where the record ended it (add
    them up). Kinds, routes and states only: never a name, an id or a reason."""

    _made: ClassVar[weakref.WeakKeyDictionary[Metrics, ApprovalMetrics]] = weakref.WeakKeyDictionary()
    _making: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def of(cls, registry: Metrics) -> ApprovalMetrics:
        with cls._making:
            found = cls._made.get(registry)
            if found is None:
                found = cls._made[registry] = cls(registry)
            return found

    def __init__(self, registry: Metrics) -> None:
        self._bound: CentralApprovals | None = None
        registry.gauge(
            "latet_approvals_company_waiting",
            "Changes that lower protection and Live Reviewer texts waiting for the security team or an API key's "
            "controllers.",
            self._waiting,
            ("kind", "route"),
            allowed={"kind": KINDS, "route": ROUTES},
            every=WAITING_EVERY,
        )
        self._ended = registry.counter(
            "latet_approvals_company_decided_total",
            "Changes and texts that stopped waiting for someone else, ended through this instance, by how they ended.",
            ("kind", "state"),
            allowed={"kind": KINDS, "state": STATES},
        )

    def bind(self, central: CentralApprovals) -> None:
        """The central approvals whose record the metrics read, and whose ends they count, now (the last edition made
        on this server)."""
        self._bound = central
        central.counted = self.ended

    def unbind(self, central: CentralApprovals) -> None:
        """An edition closed: its ends count nothing any more, and its record is read no more unless another was bound
        since (an edition made again before the one it replaces closed)."""
        if self._bound is central:
            self._bound = None
        central.counted = _uncounted

    def ended(self, kind: str, state: str) -> None:
        self._ended.inc(kind, state)  # a value outside its set is counted as other: never raised

    def _waiting(self) -> dict[tuple[str, ...], float] | None:
        """In the metrics' reading threads: what waits now, by kind and route; None (left out of the scrape) while no
        edition is bound."""
        central = self._bound
        if central is None:
            return None
        try:  # R63: one more way to the retention; E4's jobs' thread is the one that needs nothing (R73)
            central._prune()
        except Exception as exc:  # its words can name the database's address
            if count := _UNPRUNED.due():
                log.warning(
                    "the record of approvals could not be pruned (%s)%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )
        counts = central.records.counts()
        return {(kind, route): float(counts.get((kind, route), 0)) for kind in KINDS for route in ROUTES}


class ServerDirectory:
    """``Directory`` on a server: E0b's identities, E3's groups, roles and the control of API keys, each answered as a
    list of identities. A read that fails raises, as theirs do: never None, which a queue would take for an identity
    removed, and end what it asked for (the review of Task 6). Without ``conn``, E3's copies, each checked against its
    generation in Valkey: a group's name is one such read."""

    def __init__(self, identities: Identities, people: People) -> None:
        self.identities = identities
        self.membership = people  # E3: groups, roles, the control of API keys

    def identity(self, identity_id: str) -> IdentityRef | None:
        found = self.identities.get(identity_id)
        return found.ref() if found is not None and not found.disabled else None

    def _refs(self, ids: set[str], conn: Connection[Any] | None = None) -> list[IdentityRef]:
        """The enabled identities of ``ids``, by id. Given ``conn`` (the decision's transaction), read there with their
        names, never through this instance's copies: no second connection while the transaction holds one, and no copy
        a few seconds old (the review of Task 7, m4)."""
        if conn is None:
            return [ref for identity_id in sorted(ids) if (ref := self.identity(identity_id)) is not None]
        rows = conn.execute(
            "SELECT id, name, kind FROM identities WHERE id = ANY(%s) AND NOT disabled ORDER BY id", (sorted(ids),)
        ).fetchall()
        return [IdentityRef(identity_id, name, kind) for identity_id, name, kind in rows]

    def holding(self, role: Role, *, conn: Connection[Any] | None = None) -> list[IdentityRef]:
        return self._refs(self.membership.holding(role, conn=conn), conn)

    def roles(self, identity_id: str, *, conn: Connection[Any] | None = None) -> frozenset[Role] | None:
        if conn is not None:
            return self.membership.roles_in(conn, identity_id)  # None for no one, an API key or a person disabled
        found = self.identity(identity_id)
        return self.membership.roles_of(identity_id) if found is not None and found.kind == "person" else None

    def controllers(self, key_id: str, *, conn: Connection[Any] | None = None) -> list[IdentityRef]:
        return self._refs(self.membership.controlling(key_id, conn=conn), conn)

    def controlled(self, identity_id: str) -> list[IdentityRef]:
        """The enabled keys the person controls (a disabled key's changes wait for nobody: R57)."""
        return [key for key in self.membership.controlled_by(identity_id) if self.identity(key.id) is not None]

    def group_of(self, identity_id: str) -> str | None:
        groups = self.membership.groups_of(identity_id)
        return groups[0].name if groups else None


def make_approvals(edition: EnterpriseEdition, server: Server, people: People) -> CentralApprovals:
    """Central approvals on this server: its record in PostgreSQL, who is who from E0b and E3, the settings and the
    license from E1, each identity's space on this instance from E0b's spaces."""
    from latet.server import queue as server_queue
    from latet.server.spaces import IdentityUnavailable

    async def space(identity_id: str, run: Runner, fresh: bool) -> Runtime | None:
        identity = await run(server.identities.current, identity_id)  # past this instance's copy
        if identity is None or identity.disabled:
            return None
        try:
            # ``fresh``: its settings read again from PostgreSQL first, in its worker (R41: a decision never weighs a
            # copy that missed a save made on another instance).
            return await (server.spaces.reapplied(identity) if fresh else server.spaces.runtime(identity))
        except IdentityUnavailable:  # disabled or removed meanwhile
            return None

    return CentralApprovals(
        PostgresRecords(server.db, server.master.sealer(b"approvals")),  # each row's summary sealed (R40)
        ServerDirectory(server.identities, people),
        settings=lambda: edition.settings().approvals,
        licensed=lambda: edition.licensing.licensed("approvals"),
        space=space,
        waiting_batches=lambda items: server_queue.waiting_batches(server.bus, items),
        ended_changes=lambda items: server_queue.ended_changes(server.db, items),
        clock=edition.context.clock,
        keep_days=server.settings.audit_retention_days,
    )
