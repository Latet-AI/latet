"""Where the Enterprise Edition keeps a server's events: counts by the hour (the event log), and each event waiting for
each SIEM output (the SIEM queue). The protocols, the rows an event adds to, the bound of the projects a client names,
the queue's rows and its gap markers, and the implementation in this process's memory (the tests', and what two
instances of a test share when they are given the same object); PostgreSQL's is `pg.py`."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Final, NamedTuple, Protocol

from latet.ee.events.schema import checked, epoch, iso
from latet.events import FIELDS

DIMENSIONS = ("hour", "type", "identity", "kind", "group", "agent", "project", "code", "decider")
DAY = 86_400.0
BATCH_DAYS = 1  # a batch's id is kept a day: one written again after an error is counted once
# A project is the one name of an event a client gives (the folder it says it works in, on a server): one that names a
# new folder at each request would add a row an hour for each, kept for the retention. The log keeps by name the first
# MOST_PROJECTS of an identity in an hour, whichever instance wrote them, and counts the others, with their values,
# under OTHER (M6: 50, as each instance's pipeline does before). A row that keeps no identity (a person, with `people:
# group-only`) is bounded by its group's, which holds many people's projects: MOST_GROUP_PROJECTS.
MOST_PROJECTS = 50
MOST_GROUP_PROJECTS = 1_000
OTHER = "other"
# The requests: a gateway's exchange, a stopped one, and an applications-API call (R7). What a report counts as
# requests, and the events whose person has the requests' pseudonym (M1 of the final security review): every other
# type has a pseudonym of its own (``Pseudonyms.acts_of``).
REQUESTS: Final = ("exchange", "stopped", "apps")
_HOUR, _IDENTITY, _GROUP, _PROJECT = (DIMENSIONS.index(name) for name in ("hour", "identity", "group", "project"))

# Whose projects a row counts among: (hour, identity, '') for a row that keeps an identity, (hour, '', group) otherwise.
Holder = tuple[int, str, str]


class EventStore(Protocol):
    def write(
        self, batch: str, events: Sequence[Mapping[str, Any]], outputs: Sequence[str] = (), *, at: float
    ) -> bool: ...
    def each(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> Iterator[dict[str, Any]]: ...
    def query(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> list[dict[str, Any]]: ...
    def sums(
        self,
        since: float,
        until: float,
        by: Sequence[str] = (),
        where: Mapping[str, Collection[str]] | None = None,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]: ...
    def prune(self, days: int, *, now: float) -> int: ...
    def kept(self, name: str, value: bytes) -> bytes: ...
    def job(self, name: str) -> tuple[float | None, str | None]: ...
    def set_job(self, name: str, at: float, note: str | None = None) -> None: ...
    def swap_job(self, name: str, at: float, note: str, *, expected: str | None) -> bool: ...


@dataclass(frozen=True)
class QueueState:
    """How one SIEM output's queue is doing, as every instance sees it: events waiting, dropped so far, the last
    delivery, the failure going on, when to try again, and the failures in a row."""

    queued: int
    dropped: int = 0
    last_ok: float | None = None
    last_error: str | None = None
    failing_since: float | None = None
    retry_at: float | None = None
    tries: int = 0
    blocked: bool = False  # an entry of the administration log the receiver refuses holds the queue (I5)
    admin: int = 0  # the administration log's events waiting
    collapsed: int = 0  # the entries the bound collapsed into a gap marker, until the output delivers again
    # The place of the last of the events at the queue's head the receiver refused, each alone, taking none of them
    # (Task 7's fix round 2): what holds the queue until it takes them, or the administrator skips them (`skip`).
    held_to: int | None = None


# What a queue kept short drops (I5, I13): the applications API's events first, then any other but the administration
# log's, which is never trimmed (the SIEM's copy of the log is the record that shows a deletion).
FIRST_DROPPED: Final = "apps"
NEVER_DROPPED: Final = "admin"
ADMIN_DROPPED: Final = "siem-admin-dropped"  # the job's note that counts what outputs removed took of the log


ADMIN_KEPT: Final = 1_000_000  # an output's events of the administration log kept at most (about 400 MB of rows)
# How far below the bound a collapse leaves the queue (Task 7: `collapse(…, ADMIN_KEPT, keep=ADMIN_KEPT - SLACK)`):
# each collapse's own `siem.gap` entry is queued in its turn, and a queue left at the bound would be past it again at
# the next round, a collapse and an entry of the log each round.
SLACK: Final = 10_000
GAP: Final = "siem.gap"  # a gap marker's outcome: entries of the log Latet did not send, or collapsed (R2-2)


class Skipped(NamedTuple):
    """What a skip passed (R2-12, Task 7's fix round 2): the entry of the administration log now behind a gap marker
    of its own (``first`` to ``last``, ``entries`` of them: a marker refused in its turn stands for many; None, None, 0
    when none), and the events other than the log's at the head that the receiver refused, each alone, which it dropped
    and counted (``dropped``). The route records both as `siem.skipped`."""

    first: int | None
    last: int | None
    entries: int
    dropped: int


# What writes the entry of the administration log that names a gap marker's range, in the marker's own transaction
# (R3-1): (that transaction's connection, None in memory; the action, `siem.gap`; its details {output, from, to,
# count}). On a server, Task 11's: E0b's `AuditLog.record` by `server`, given the connection.
Noting = Callable[[Any, str, Mapping[str, Any]], None]


class SiemQueue(Protocol):
    def put(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        at: float,
        refs: Sequence[int | None] | None = None,
    ) -> None: ...
    def put_audit(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        refs: Sequence[int | None],
        job: str,
        note: str,
        expected: str | None,
        at: float,
        noted: Callable[[Any], None] | None = None,
    ) -> bool: ...
    def collapse(
        self,
        output: str,
        most: int,
        *,
        origin: str,
        at: float,
        keep: int | None = None,
        noted: Noting | None = None,
    ) -> tuple[int, int, int] | None: ...
    def skip(
        self, output: str, *, origin: str, at: float, conn: Any = None, held_only: bool = False
    ) -> Skipped | None: ...
    def markers(self) -> list[tuple[str, dict[str, Any]]]: ...
    def peek(self, output: str, n: int) -> list[tuple[int, dict[str, Any]]]: ...
    def peek_rows(self, output: str, n: int) -> list[tuple[int, dict[str, Any], str]]: ...
    def done(self, output: str, seqs: Sequence[int], *, at: float, texts: Sequence[str] | None = None) -> None: ...
    def drop(self, output: str, seqs: Sequence[int], why: str) -> int: ...
    def failed(
        self, output: str, why: str, *, at: float, retry_at: float, blocked: bool = False, held_to: int | None = None
    ) -> None: ...
    def schedule(self, output: str) -> tuple[float | None, int]: ...
    def trim(self, output: str, limit: int) -> int: ...
    def state(self, output: str) -> QueueState: ...
    def forget(self, outputs: Collection[str]) -> int: ...
    def forget_unknown(self, keep: Collection[str], *, before: float) -> int: ...
    def admin_waiting(self, output: str) -> int: ...
    def admin_dropped(self) -> int: ...
    def counts(self) -> dict[str, int]: ...


class UnreadableQueue(ValueError):
    """A row of a SIEM queue that is not JSON, which no code of Latet writes: `markers()` raises it, so that `latet
    server log verify` fails rather than pass over what it cannot read (R4-1)."""


_GAP_REASON: Final = re.compile(r"audit:([0-9]+)-([0-9]+)")  # ASCII digits alone: the form Latet writes (R4-1)
_HASH: Final = re.compile(r"[0-9a-f]{64}")


def queued(
    outputs: Sequence[str],
    events: Sequence[Mapping[str, Any]],
    at: float,
    refs: Sequence[int | None] | None = None,
) -> list[tuple[str, float, str, str, int | None]]:
    """The queue's rows of ``events`` for each output, in the events' order: (output, when, the event's type, its
    JSON, its entry's id in the administration log: an `admin` event's, None for the others)."""
    ids = list(refs) if refs is not None else [None] * len(events)
    rows = [
        (str(event.get("type") or ""), json.dumps(dict(event), separators=(",", ":")), ref)
        for event, ref in zip(events, ids, strict=True)
    ]
    return [(output, at, kind, text, ref) for kind, text, ref in rows for output in outputs]


def audit_rows(
    outputs: Sequence[str], events: Sequence[Mapping[str, Any]], refs: Sequence[int | None], at: float
) -> list[tuple[str, float, str, str, int | None]]:
    """``queued``'s rows of a page of the administration log (``put_audit``): each event checked (``checked``: a
    NotAnEvent refuses the page whole), an event of the log, gap marker or entry, with the id of its first entry. An
    `admin` row with no id would read as the log's entry 0 (Task 4's ``span_of``): ValueError, in fixed words."""
    if len(refs) != len(events):
        raise ValueError("a page of the administration log names one entry for each event")
    if any(type(ref) is not int or ref < 1 for ref in refs):
        raise ValueError("an event of the administration log names its entry by its id")
    return queued(outputs, [checked(event) for event in events], at, refs) if outputs and events else []


def gap_event(
    first: int, last: int, count: int, prev: str | None, hash_: str | None, *, origin: str, at: float
) -> dict[str, Any]:
    """An `admin` event standing, in a SIEM's copy of the administration log, for the entries ``first`` to ``last``
    (``count`` of them) that Latet did not send (no output, the license producing nothing: R2-2), skipped (R2-12) or
    collapsed past ``ADMIN_KEPT``: their ids and how many, and the chain across them (``prev`` of the first, ``hash`` of
    the last), so that the receiver's chain links straight through and the gap names itself; no chain when either is
    None or not a hash (``prev`` is "" for the log's first entry alone). Never an entry's detail; the same id on every
    instance for the same range."""
    chain = None
    if isinstance(prev, str) and isinstance(hash_, str) and _HASH.fullmatch(hash_):
        chain = {"prev": prev, "hash": hash_} if prev == "" or _HASH.fullmatch(prev) else None
    event = dict.fromkeys(FIELDS) | {
        "v": 1, "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{origin}#gap:{first}-{last}").hex, "ts": iso(at),
        "type": NEVER_DROPPED, "masked": {}, "stopped": count, "reason": f"audit:{first}-{last}", "outcome": GAP,
        "chain": chain,
    }  # fmt: skip
    return checked(event)


def is_gap(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` is a gap marker in the form Latet writes one (R4-1): `outcome` `siem.gap`, a `reason`
    `audit:<first>-<last>` in ASCII digits, which no entry's actor ever is, and a `stopped` that is an int. The log's
    own `siem.gap` entry (a collapse the server recorded) is an entry like any other."""
    reason, stopped = event.get("reason"), event.get("stopped")
    return (
        event.get("outcome") == GAP
        and type(stopped) is int
        and isinstance(reason, str)
        and _GAP_REASON.fullmatch(reason) is not None
    )


Span = tuple[int, int, int, str | None, str | None]


def span_of(event: Mapping[str, Any], ref: int | None) -> Span:
    """What an `admin` row of a queue stands for: (its first entry's id, its last's, how many, the chain's prev, its
    hash): a gap marker its range, an entry itself (``ref``). Prev and hash are None, both, for a row without a chain:
    a missing chain is never read as the log's first prev ("")."""
    chain = event.get("chain")
    prev = chain.get("prev") if isinstance(chain, Mapping) else None
    found = chain.get("hash") if isinstance(chain, Mapping) else None
    if not (isinstance(prev, str) and isinstance(found, str)):
        prev = found = None
    gap = _GAP_REASON.fullmatch(event["reason"]) if is_gap(event) else None
    if gap is not None:
        return int(gap[1]), int(gap[2]), int(event["stopped"]), prev, found
    return int(ref or 0), int(ref or 0), 1, prev, found


def _marker(spans: Sequence[Span], *, origin: str, at: float) -> str:
    """The gap marker's JSON for consecutive ``spans`` of a queue: their first id, their last, their entries, and the
    chain from the first's prev to the last's hash when every span has one and each hash is the next one's prev; no
    chain otherwise (a break, or a row without a chain), never one that claims a link the entries do not make."""
    first, last = spans[0][0], spans[-1][1]
    linked = all(span[3] is not None and span[4] is not None for span in spans) and all(
        before[4] == after[3] for before, after in pairwise(spans)
    )
    prev, found = (spans[0][3], spans[-1][4]) if linked else (None, None)
    event = gap_event(first, last, sum(span[2] for span in spans), prev, found, origin=origin, at=at)
    return json.dumps(event, separators=(",", ":"))


def skipped_head(rows: Iterable[tuple[int, str]], held_to: int | None) -> tuple[list[int], int | None]:
    """What a skip passes of a queue's head, ``rows`` its rows' (place, type) oldest first: the places of the events
    other than the log's that the receiver refused, each alone, taking none (up to ``held_to``, the place of the last
    of them, and the first entry of the log), dropped; and the place of the entry of the log the queue waits at, if
    any, put behind a gap marker of its own, never dropped: the head itself when no run is held (``held_to`` None),
    else the first entry within the run. An entry past the run was never sent: it stays as it is."""
    run: list[int] = []
    for seq, kind in rows:
        if held_to is not None and seq > held_to:
            break
        if kind == NEVER_DROPPED:
            return run, seq
        if held_to is None:
            break
        run.append(seq)
    return run, None


def to_collapse(waiting: int, most: int, keep: int | None = None) -> int:
    """How many of an output's oldest `admin` rows a collapse takes into one marker, when ``waiting`` wait: none
    within ``most``; past it, enough to leave ``keep`` (``most`` when not given; one at least, since the marker is one
    of them, and ``most`` at most), the marker among them."""
    most = max(most, 1)
    kept = most if keep is None else min(max(keep, 1), most)
    return waiting - kept + 1 if waiting > most else 0


def count_rows(events: Iterable[Mapping[str, Any]]) -> dict[tuple[Any, ...], list[int]]:
    """The rows of the log events add to, ``[events, values]`` each: one for the event itself (category ''), one for
    each kind of value it masked. Keys are DIMENSIONS' values, then the category; rows of one key are summed, in the
    order the events came. Each event is checked first (``checked``): NotAnEvent, in its words, for one that is not an
    event of the schema, and nothing is counted."""
    return rows_of([checked(e) for e in events])


def rows_of(events: Iterable[Mapping[str, Any]]) -> dict[tuple[Any, ...], list[int]]:
    """``count_rows``' rows of events ``checked`` gave already: a write checks each event once, then counts and queues
    the same events."""
    rows: dict[tuple[Any, ...], list[int]] = {}
    for e in events:
        key = (
            int(epoch(e["ts"]) // 3600), e["type"], e["identity"] or "", e["kind"] or "", e["group"] or "",
            e["agent"] or "", e["project"] or "", e["outcome"] or e["reason"] or "", e["by"] or "",
        )  # fmt: skip
        for category, values in (("", 0), *e["masked"].items()):
            row = rows.setdefault((*key, category), [0, 0])
            row[0] += 1
            row[1] += values
    return rows


def holder(key: Sequence[Any]) -> Holder:
    """Whose projects a row of the log (its key) is counted among: its identity's in its hour, or, for a row that keeps
    no identity, its group's."""
    hour, identity, group = key[_HOUR], key[_IDENTITY], key[_GROUP]
    return (hour, identity, "") if identity else (hour, "", group)


def holders(rows: Iterable[Sequence[Any]]) -> set[Holder]:
    """The holders of the rows that name a project: those whose projects a write must know first."""
    return {holder(key) for key in rows if key[_PROJECT]}


def bounded(
    rows: Mapping[tuple[Any, ...], Sequence[int]], known: Mapping[Holder, Collection[str]]
) -> dict[tuple[Any, ...], list[int]]:
    """``rows`` (``count_rows``'s, in the order the events came) with each project past its holder's bound counted as
    ``OTHER``: a holder keeps by name the projects the log ``known`` holds of it in that hour, and new ones while they
    are fewer than its bound (``MOST_PROJECTS`` an identity, ``MOST_GROUP_PROJECTS`` a group)."""
    names: dict[Holder, set[str]] = {}
    out: dict[tuple[Any, ...], list[int]] = {}
    for key, (count, values) in rows.items():
        project = key[_PROJECT]
        if project:
            who = holder(key)
            held = names.get(who)
            if held is None:
                held = names[who] = set(known.get(who, ()))
            if project not in held:
                if len(held) < (MOST_PROJECTS if who[1] else MOST_GROUP_PROJECTS):
                    held.add(project)
                else:
                    key = (*key[:_PROJECT], OTHER, *key[_PROJECT + 1 :])
        row = out.setdefault(key, [0, 0])
        row[0] += count
        row[1] += values
    return out


def hours(since: float, until: float) -> tuple[int, int]:
    """The hours ``[first, last)`` that ``[since, until)`` touches: an hour is counted whole."""
    return int(since // 3600), int(-(-until // 3600))


def retention(days: object) -> int:
    """``days`` as a retention: a whole number of days, 1 at least. ValueError otherwise: what prune forgets is gone,
    and a retention read wrong forgets nothing."""
    if type(days) is not int or days < 1:
        raise ValueError("a retention is a whole number of days, 1 at least")
    return days


def dimensions(by: Sequence[str], where: Mapping[str, Collection[str]] | None) -> list[str]:
    """The dimensions asked, each once, in the order given. ValueError: one the log does not have."""
    names = list(dict.fromkeys(by))
    unknown = (set(names) | set(where or {})) - set(DIMENSIONS)
    if unknown:
        raise ValueError(f"no such dimension: {', '.join(sorted(map(str, unknown)))}")
    return names


# What ``sums`` adds up by: the log's dimensions, and the kind of value of each row ('' for the events themselves).
COUNTED = (*DIMENSIONS, "category")
SUMS = ("exchanges", "stopped", "masked")


def counted(by: Sequence[str], where: Mapping[str, Collection[str]] | None, limit: int | None) -> list[str]:
    """What ``sums`` adds up by, each once, in the order given. ValueError: a dimension the log does not have (``where``
    takes the log's dimensions alone: the kind of value is ``category``), or a limit that is not a number of rows, one
    at least (a limit of none would leave no row to say how many keys there were)."""
    names = list(dict.fromkeys(by))
    unknown = (set(names) - set(COUNTED)) | (set(where or {}) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"no such dimension: {', '.join(sorted(map(str, unknown)))}")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("a limit is a number of rows, one at least")
    return names


def ranked(
    added: Mapping[tuple[Any, ...], Sequence[int]], names: Sequence[str], limit: int | None
) -> tuple[list[dict[str, Any]], int]:
    """``sums``' answer from what it added up by key: the rows ranked by requests, then values, then key (in its
    characters' order, as PostgreSQL's ``C`` collation), the first ``limit``, and how many keys there were: the empty
    key (every dimension '': no group, no project, no one) is a row but no key, and an hour is never empty. Totals (no
    ``names``) are one row, of zeros when nothing was counted, and one key."""
    if not names and not added:
        added = {(): (0, 0, 0)}
    order = sorted(added.items(), key=lambda item: (-item[1][0], -item[1][2], item[0]))
    shown = order if limit is None else order[:limit]
    rows = [dict(zip(names, key, strict=True)) | dict(zip(SUMS, values, strict=True)) for key, values in shown]
    return rows, sum(1 for key, _ in order if not names or not all(value == "" for value in key))


# A row of the queue in memory: (place, output, when, the event's JSON, its type, its entry's id in the log).
_Row = tuple[int, str, float, str, str, int | None]


class MemoryEvents:
    """The event log and the SIEM queue in this process's memory."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: dict[tuple[Any, ...], list[int]] = {}
        self._projects: dict[Holder, set[str]] = {}  # the projects each holder's rows name (the bound's)
        self._batches: dict[str, float] = {}
        self._keys: dict[str, bytes] = {}
        self._jobs: dict[str, tuple[float | None, str | None]] = {}
        # (place, output, when, the event's JSON, its type, its entry's id in the administration log)
        self._queue: list[_Row] = []
        self._seq = 0
        self._state: dict[str, dict[str, Any]] = {}
        self._admin_dropped = 0

    def write(self, batch: str, events: Sequence[Mapping[str, Any]], outputs: Sequence[str] = (), *, at: float) -> bool:
        """Count ``events`` and queue them for each of ``outputs``, together, once a batch: False, and nothing written,
        for a batch written already. Each event is checked once, before anything is written (NotAnEvent refuses the
        batch whole), and the outputs get the events as checked, never as given."""
        events = [checked(e) for e in events]
        rows = rows_of(events)
        waiting = queued(outputs, events, at)  # made before anything is written: written with the counts, or neither
        with self._lock:
            if batch in self._batches:
                return False
            self._batches[batch] = at
            self._add(rows)
            self._append(waiting)
        return True

    def _add(self, rows: Mapping[tuple[Any, ...], Sequence[int]]) -> None:
        """Add ``rows`` to the counts, each project past its bound as ``OTHER``. The lock held."""
        known = {who: self._projects.get(who, set()) for who in holders(rows)}
        for key, (count, values) in bounded(rows, known).items():
            row = self._counts.setdefault(key, [0, 0])
            row[0] += count
            row[1] += values
            if key[_PROJECT]:
                self._projects.setdefault(holder(key), set()).add(key[_PROJECT])

    def query(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> list[dict[str, Any]]:
        """The counts of ``[since, until)`` (taken by the hour), summed over every dimension but those in ``by`` and
        the kind of value. ``events``: the events counted (in a kind's row, those that masked a value of it);
        ``masked``: values, in a kind's rows. ``where`` keeps the rows whose dimension is one of its values."""
        names = dimensions(by, where)
        index = {name: i for i, name in enumerate(DIMENSIONS)}
        chosen = {name: set(values) for name, values in (where or {}).items()}
        first, last = hours(since, until)
        with self._lock:
            items = [(key, list(row)) for key, row in self._counts.items()]
        out: dict[tuple[Any, ...], list[int]] = {}
        for key, (count, values) in items:
            if not first <= key[_HOUR] < last or any(key[index[name]] not in found for name, found in chosen.items()):
                continue
            row = out.setdefault((*(key[index[name]] for name in names), key[-1]), [0, 0])
            row[0] += count
            row[1] += values
        keys = [*names, "category"]
        return [dict(zip(keys, group, strict=True)) | {"events": n, "masked": v} for group, (n, v) in out.items()]

    def each(
        self, since: float, until: float, by: Sequence[str] = (), where: Mapping[str, Collection[str]] | None = None
    ) -> Iterator[dict[str, Any]]:
        yield from self.query(since, until, by, where)

    def sums(
        self,
        since: float,
        until: float,
        by: Sequence[str] = (),
        where: Mapping[str, Collection[str]] | None = None,
        *,
        category: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """A report's arithmetic over ``[since, until)`` by ``by`` (the log's dimensions, and ``category``), within
        ``where``: ``exchanges`` the events (their own rows), ``stopped`` those of type `stopped`, ``masked`` the
        values (the kinds' rows); with a kind of value chosen (``category``), the events that masked one of it and its
        values alone. Ranked by requests, then values, then key, the first ``limit`` of them (all with None); and how
        many keys there were. PostgreSQL's does it in SQL: a report never reads every row."""
        names = counted(by, where, limit)
        index = {name: i for i, name in enumerate(COUNTED)}
        chosen = {name: set(values) for name, values in (where or {}).items()}
        first, last = hours(since, until)
        kind_of, type_of = index["category"], index["type"]
        with self._lock:
            items = [(key, list(row)) for key, row in self._counts.items()]
        added: dict[tuple[Any, ...], list[int]] = {}
        for key, (count, values) in items:
            if not first <= key[_HOUR] < last or any(key[index[name]] not in found for name, found in chosen.items()):
                continue
            kind = key[kind_of]
            if category is not None and kind != category:
                continue
            row = added.setdefault(tuple(key[index[name]] for name in names), [0, 0, 0])
            if category is not None or not kind:  # the events: their own rows, or the chosen kind's
                row[0] += count
                row[1] += count if key[type_of] == "stopped" else 0
            if category is not None or kind:  # the values: the kinds' rows
                row[2] += values
        return ranked(added, names, limit)

    def prune(self, days: int, *, now: float) -> int:
        """Forget the counts older than ``days`` days and the batches older than a day: the rows of counts removed.
        ValueError, and nothing forgotten, for ``days`` that are not a retention (``retention``)."""
        oldest = int((now - retention(days) * DAY) // 3600)
        with self._lock:
            old = [key for key in self._counts if key[_HOUR] < oldest]
            for key in old:
                del self._counts[key]
            for who in [who for who in self._projects if who[0] < oldest]:
                del self._projects[who]
            for batch in [b for b, at in self._batches.items() if at < now - BATCH_DAYS * DAY]:
                del self._batches[batch]
        return len(old)

    def kept(self, name: str, value: bytes) -> bytes:
        with self._lock:
            return self._keys.setdefault(name, value)

    def job(self, name: str) -> tuple[float | None, str | None]:
        with self._lock:
            return self._jobs.get(name, (None, None))

    def set_job(self, name: str, at: float, note: str | None = None) -> None:
        with self._lock:
            self._jobs[name] = (at, note)

    def swap_job(self, name: str, at: float, note: str, *, expected: str | None) -> bool:
        """The job's note written only while it is still ``expected``, the one read (None: no note yet): False, and
        nothing written, when another instance moved it meanwhile. A job whose lock may run out while it looks (the
        license's) records what it found only when its write won."""
        with self._lock:
            if self._jobs.get(name, (None, None))[1] != expected:
                return False
            self._jobs[name] = (at, note)
            return True

    # --- the SIEM queue -----------------------------------------------------------------------------------------------

    def _append(self, rows: Iterable[tuple[str, float, str, str, int | None]]) -> None:
        """``queued``'s rows at the end of the queue, each with its place. The lock held."""
        for output, when, kind, text, ref in rows:
            self._seq += 1
            self._queue.append((self._seq, output, when, text, kind, ref))

    def put(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        at: float,
        refs: Sequence[int | None] | None = None,
    ) -> None:
        rows = queued(outputs, events, at, refs)
        with self._lock:
            self._append(rows)

    def put_audit(
        self,
        outputs: Sequence[str],
        events: Sequence[Mapping[str, Any]],
        *,
        refs: Sequence[int | None],
        job: str,
        note: str,
        expected: str | None,
        at: float,
        noted: Callable[[Any], None] | None = None,
    ) -> bool:
        """Queue the `admin` events of a page of the administration log, and note the last entry read, at once; only
        while the note is still ``expected``, the one the round read (R2-10): False, and nothing written, otherwise.
        Each event is checked, and names its entry (``audit_rows``). ``noted`` writes the entry of the log naming a gap
        whose marker the page begins with, first: when it raises, nothing is written (R3-1)."""
        with self._lock:
            if self._jobs.get(job, (None, None))[1] != expected:
                return False
            rows = audit_rows(outputs, events, refs, at)
            if noted is not None:
                noted(None)
            self._append(rows)
            self._jobs[job] = (at, note)
            return True

    def collapse(
        self,
        output: str,
        most: int,
        *,
        origin: str,
        at: float,
        keep: int | None = None,
        noted: Noting | None = None,
    ) -> tuple[int, int, int] | None:
        """Past ``most`` events of the administration log waiting for ``output``, the oldest collapse in place into one
        gap marker, which keeps the place of the oldest, until ``keep`` wait (``most`` when not given: see ``SLACK``);
        a marker there already is extended, never a second made: (first id, last id, the entries it stands for); None
        within the bound. A marker is itself one of the log's rows: a bound below one keeps it alone. The marker is
        made, then ``noted`` writes the log's entry naming the range: when either raises, nothing is collapsed and no
        entry written (R3-1)."""
        with self._lock:
            admin = [row for row in self._queue if row[1] == output and row[4] == NEVER_DROPPED]
            many = to_collapse(len(admin), most, keep)
            if not many:
                return None
            chosen = admin[:many]
            spans = [span_of(json.loads(row[3]), row[5]) for row in chosen]
            total = sum(span[2] for span in spans)
            marker = _marker(spans, origin=origin, at=at)
            if noted is not None:
                noted(None, GAP, {"output": output, "from": spans[0][0], "to": spans[-1][1], "count": total})
            head, gone = chosen[0], {row[0] for row in chosen[1:]}
            self._queue = [
                (row[0], row[1], row[2], marker, row[4], spans[0][0]) if row[0] == head[0] else row
                for row in self._queue
                if row[0] not in gone
            ]
            self._row(output)["collapsed"] = total  # FAIL in the status until the output delivers again
            return spans[0][0], spans[-1][1], total

    def skip(self, output: str, *, origin: str, at: float, conn: Any = None, held_only: bool = False) -> Skipped | None:
        """What holds the queue's head, passed (R2-12; Task 7's fix round 2: ``skipped_head``): the events other than
        the log's that the receiver refused, each alone, dropped and counted; and the entry of the administration log
        the queue waits at, sent no more: it stands behind a gap marker of its own, and a marker refused in its turn is
        taken off. The queue is no longer held. What it passed, which the caller records as `siem.skipped` (R3-1: in
        ``conn``'s transaction, on a server); None when nothing holds the head. ``held_only`` (the route's, Task 10):
        None, and nothing passed, unless the receiver refused the head (``blocked``, or a run refused: ``held_to``), so
        that an entry merely waiting its turn is never put behind a marker."""
        with self._lock:
            mine = [row for row in self._queue if row[1] == output]
            state = self._state.get(output) or {}
            held_to = state.get("held_to")
            if held_only and not state.get("blocked") and held_to is None:
                return None
            run, entry = skipped_head(((row[0], row[4]) for row in mine), held_to)
            if not run and entry is None:
                return None
            dropped = self._take(output, run)
            first: int | None = None
            last: int | None = None
            count = 0
            if entry is not None:
                head = next(row for row in mine if row[0] == entry)
                event = json.loads(head[3])
                span = span_of(event, head[5])
                first, last, count = span[0], span[1], span[2]
                if is_gap(event):
                    self._take(output, [entry])
                else:
                    marker = _marker([span], origin=origin, at=at)
                    self._queue = [(*row[:3], marker, *row[4:]) if row[0] == entry else row for row in self._queue]
            row = self._row(output)
            row.update(retry_at=None, tries=0, blocked=False, held_to=None)
            row["dropped"] += dropped
            return Skipped(first, last, count, dropped)

    def markers(self) -> list[tuple[str, dict[str, Any]]]:
        """Every event waiting in every queue whose `outcome` is `siem.gap`, by output, oldest first, whatever its
        `type` or form: what `latet server log verify` accounts for (R3-1, R4-1). ``UnreadableQueue`` for a row that
        is not JSON."""
        with self._lock:
            rows = [(row[1], row[3]) for row in self._queue]
        found: list[tuple[str, dict[str, Any]]] = []
        for output, text in rows:
            try:
                event = json.loads(text)
            except ValueError as exc:
                raise UnreadableQueue("a row of a SIEM queue is not JSON") from exc
            if isinstance(event, dict) and event.get("outcome") == GAP:
                found.append((output, event))
        return found

    def peek(self, output: str, n: int) -> list[tuple[int, dict[str, Any]]]:
        return [(seq, event) for seq, event, _ in self.peek_rows(output, n)]

    def peek_rows(self, output: str, n: int) -> list[tuple[int, dict[str, Any], str]]:
        """``peek``'s rows, each with its text as the queue keeps it: what ``done`` takes back to know them."""
        with self._lock:
            mine = [(row[0], row[3]) for row in self._queue if row[1] == output][:n]
        return [(seq, json.loads(text), text) for seq, text in mine]

    def _take(self, output: str, seqs: Collection[int]) -> int:
        wanted = set(seqs)
        before = len(self._queue)
        self._queue = [row for row in self._queue if not (row[1] == output and row[0] in wanted)]
        return before - len(self._queue)

    def _row(self, output: str) -> dict[str, Any]:
        empty = {"dropped": 0, "last_ok": None, "last_error": None, "failing_since": None, "retry_at": None, "tries": 0,
                 "blocked": False, "collapsed": 0, "held_to": None}  # fmt: skip
        return self._state.setdefault(output, empty)

    def done(self, output: str, seqs: Sequence[int], *, at: float, texts: Sequence[str] | None = None) -> None:
        """Delivered: these rows leave the queue, and the failure, if any, is over. With ``texts`` (each row's text as
        ``peek_rows`` read it, in the same order) a row leaves only while it holds that text still: one a collapse or a
        skip made a gap marker of meanwhile stays, and goes out in its turn (Task 7's fix rounds 1 and 2), whatever its
        event holds (an id of any kind, a number PostgreSQL's JSON cannot hold)."""
        with self._lock:
            if texts is None:
                self._take(output, seqs)
            else:
                sent = dict(zip(seqs, texts, strict=True))
                self._queue = [
                    row for row in self._queue if not (row[1] == output and row[0] in sent and row[3] == sent[row[0]])
                ]
            self._row(output).update(
                last_ok=at, last_error=None, failing_since=None, retry_at=None, tries=0, blocked=False, collapsed=0,
                held_to=None,
            )  # fmt: skip

    def drop(self, output: str, seqs: Sequence[int], why: str) -> int:
        """A batch the receiver refused for good taken off and counted as dropped, but never an event of the
        administration log in it: one refused holds the queue until the administrator skips it (I5)."""
        with self._lock:
            admin = {row[0] for row in self._queue if row[1] == output and row[4] == NEVER_DROPPED}
            gone = self._take(output, [seq for seq in seqs if seq not in admin])
            row = self._row(output)
            row["dropped"] += gone
            row["last_error"] = why
        return gone

    def failed(
        self, output: str, why: str, *, at: float, retry_at: float, blocked: bool = False, held_to: int | None = None
    ) -> None:
        """A try that failed: tried again at ``retry_at``. ``blocked``: an entry of the log the receiver refuses holds
        the queue (I5); ``held_to``: the place of the last of the events at the head it refused, each alone, taking
        none. Each failure says both anew: the last try's is what holds."""
        with self._lock:
            row = self._row(output)
            row.update(last_error=why, retry_at=retry_at, tries=row["tries"] + 1, blocked=blocked, held_to=held_to)
            if row["failing_since"] is None:
                row["failing_since"] = at

    def schedule(self, output: str) -> tuple[float | None, int]:
        with self._lock:
            row = self._state.get(output)
            return (row["retry_at"], row["tries"]) if row is not None else (None, 0)

    def trim(self, output: str, limit: int) -> int:
        """The queue kept to ``limit`` events: the oldest of the applications API's go first, then the oldest of the
        others but the administration log's, which are never trimmed (I5, I13); how many went, counted as dropped."""
        with self._lock:
            mine = [row for row in self._queue if row[1] == output]
            order = [r for r in mine if r[4] == FIRST_DROPPED] + [
                r for r in mine if r[4] not in (FIRST_DROPPED, NEVER_DROPPED)
            ]
            taken = self._take(output, [row[0] for row in order[: max(len(mine) - limit, 0)]])
            if taken:
                self._row(output)["dropped"] += taken
        return taken

    def state(self, output: str) -> QueueState:
        with self._lock:
            mine = [row for row in self._queue if row[1] == output]
            admin = [row for row in mine if row[4] == NEVER_DROPPED]
            row = dict(self._state.get(output) or {})
        return QueueState(len(mine), **row, admin=len(admin))

    def _gone(self, keep: Callable[[_Row], bool]) -> int:
        """Under the lock: the rows ``keep`` refuses taken off, the administration log's among them counted apart."""
        old = len(self._queue)
        self._admin_dropped += sum(1 for row in self._queue if not keep(row) and row[4] == NEVER_DROPPED)
        self._queue = [row for row in self._queue if keep(row)]
        return old - len(self._queue)

    def forget(self, outputs: Collection[str]) -> int:
        gone = set(outputs)
        with self._lock:
            taken = self._gone(lambda row: row[1] not in gone)
            for name in gone:
                self._state.pop(name, None)
            return taken

    def forget_unknown(self, keep: Collection[str], *, before: float) -> int:
        kept = set(keep)
        with self._lock:
            taken = self._gone(lambda row: row[1] in kept or row[2] >= before)
            waiting = {row[1] for row in self._queue}
            for name in [n for n in self._state if n not in kept and n not in waiting]:
                del self._state[name]
            return taken

    def admin_waiting(self, output: str) -> int:
        with self._lock:
            return sum(1 for row in self._queue if row[1] == output and row[4] == NEVER_DROPPED)

    def admin_dropped(self) -> int:
        with self._lock:
            return self._admin_dropped

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(row[1] for row in self._queue))
