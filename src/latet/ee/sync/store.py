"""What term sync keeps: the synced terms, each source's last run, and the syncs asked for. On a server in PostgreSQL
(pgstore.py, every term sealed with the master key's `terms` sealer); in memory for the tests, with the same meaning. A
term its source no longer gives keeps its row, marked gone: it stays masked until someone removes it."""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class StoredTerm:
    id: str  # a hash keyed with a secret: it says nothing of the value
    source: str
    type: str
    value: str = field(repr=False)  # a synced term is sensitive in itself: never in a log line or a traceback
    first_seen: float
    gone: float | None  # when its source stopped giving it; None: it still does


@dataclass(frozen=True)
class Run:
    """A source's last sync: what it gave, or why it failed. Never a term."""

    source: str
    kind: str  # file, url, graph or ldap: the metrics' label
    started: float
    finished: float
    outcome: Literal["ok", "failed"]
    code: str | None = None  # why it failed (records.CODES), for the events and the metrics
    reason: str | None = None  # why it failed, for people: Latet's words and codes, never a record's
    found: int = 0  # terms the source gave
    added: int = 0  # of them, new
    gone: int = 0  # terms it no longer gave
    skipped: dict[str, int] = field(default_factory=dict)  # values left out, by why


@dataclass(frozen=True)
class Asked:
    """A sync asked for by hand, not run yet: when, and by whom (a person's id; None for the administration)."""

    source: str
    at: float
    by: str | None


class SealedElsewhere(Exception):
    """The synced terms were sealed with another master key: none can be read, nor a new one matched to them."""


class NewerKey(Exception):
    """A term is sealed with a master key version newer than this server's newest: another instance may open it, so
    nothing is removed as unreadable (B-R5)."""


class TooMany(Exception):
    """A run would keep more terms than ``most``, gone ones included: nothing is written."""

    def __init__(self, kept: int) -> None:
        super().__init__(f"{kept} terms would be kept")
        self.kept = kept


def uncounted(run: Run) -> None:
    """A run counted nowhere: a term sync's count of runs while no metrics are bound to it (``TermMetrics.unbind``)."""
    return None


def term_id(key: bytes, source: str, etype: str, value: str) -> str:
    """A term's id: a hash keyed with a secret, over its source, its type and its value composed and case-folded (a
    change of case at the source is the same term), so that neither the id nor a guessed value tells the other."""
    folded = unicodedata.normalize("NFC", value).casefold()
    return hmac.new(key, f"{source}\x00{etype}\x00{folded}".encode(), hashlib.sha256).hexdigest()[:24]


def tally(rows: Iterable[tuple[str, str, bool, int]]) -> dict[str, dict[str, Any]]:
    """(source, type, gone, count) rows, as `counts()` answers them: per source, its terms still in it by type, and
    how many are no longer."""
    out: dict[str, dict[str, Any]] = {}
    for source, etype, gone, n in rows:
        entry = out.setdefault(source, {"terms": 0, "by_type": {}, "gone": 0})
        if gone:
            entry["gone"] += n
        else:
            entry["terms"] += n
            entry["by_type"][etype] = entry["by_type"].get(etype, 0) + n
    return out


class TermStore(Protocol):
    """What term sync keeps, and how it changes. Every change of the terms moves `version()`. A change given ``conn``
    is written in the caller's transaction (a route writes it with its entry in the administration log)."""

    unreadable: int  # terms the last `terms()` could not open (sealed with another master key)

    def version(self) -> int: ...
    def apply(
        self, source: str, terms: Sequence[tuple[str, str]], now: float, *, most: int | None = None, conn: Any = None
    ) -> tuple[int, int]: ...
    def retire(self, configured: Iterable[str], now: float, *, conn: Any = None) -> int: ...
    def terms(self) -> list[StoredTerm]: ...
    def kept(self) -> int: ...
    def gone(self, limit: int = 1000, source: str | None = None) -> tuple[list[StoredTerm], int]: ...
    def remove(
        self, ids: Iterable[str] | None = None, *, source: str | None = None, conn: Any = None
    ) -> dict[str, int]: ...
    def record_run(self, run: Run) -> None: ...
    def forget_runs(self, names: Iterable[str], *, conn: Any = None) -> None: ...
    def runs(self) -> dict[str, Run]: ...
    def ask(self, names: Iterable[str], by: str | None, now: float, *, conn: Any = None) -> None: ...
    def remove_unreadable(self, *, conn: Any = None) -> dict[str, int]: ...
    def asks(self) -> dict[str, Asked]: ...
    def counts(self) -> dict[str, dict[str, Any]]: ...
    def close(self) -> None: ...


class MemoryTermStore:
    """The store in memory, for the tests: what PgTermStore keeps, with the same meaning. It has no transaction, so a
    change given ``conn`` ignores it, and it seals nothing, so no term is ever unreadable."""

    def __init__(self, ids_key: bytes | None = None) -> None:
        self._key = ids_key if ids_key is not None else os.urandom(32)
        self._lock = threading.RLock()
        self._terms: dict[str, StoredTerm] = {}
        self._runs: dict[str, Run] = {}
        self._asks: dict[str, Asked] = {}
        self._version = 0
        self.unreadable = 0

    def term_id(self, source: str, etype: str, value: str) -> str:
        return term_id(self._key, source, etype, value)

    def version(self) -> int:
        with self._lock:
            return self._version

    def apply(
        self, source: str, terms: Sequence[tuple[str, str]], now: float, *, most: int | None = None, conn: Any = None
    ) -> tuple[int, int]:
        """A successful run of ``source``, which gave ``terms`` (value, type): new ones are added, those back in it
        are no longer gone, those it no longer gives are marked gone. A term already kept keeps its first spelling,
        whatever its case now. With ``most``, `TooMany` and nothing changed when the rows kept after it (every
        source's, gone ones included, and its new ones) would be more. How many were added, and newly gone."""
        given: dict[str, tuple[str, str]] = {}
        for value, etype in terms:
            given.setdefault(self.term_id(source, etype, value), (value, etype))  # the first spelling a run gives
        with self._lock:
            known = {i: t for i, t in self._terms.items() if t.source == source}
            new = [i for i in given if i not in known]
            if most is not None and len(self._terms) + len(new) > most:
                raise TooMany(len(self._terms) + len(new))
            back = [i for i, t in known.items() if i in given and t.gone is not None]
            left = [i for i, t in known.items() if i not in given and t.gone is None]
            for i in new:
                value, etype = given[i]
                self._terms[i] = StoredTerm(i, source, etype, value, now, None)
            for i in back:
                self._terms[i] = replace(self._terms[i], gone=None)
            for i in left:
                self._terms[i] = replace(self._terms[i], gone=now)
            if new or back or left:
                self._version += 1
        return len(new), len(left)

    def retire(self, configured: Iterable[str], now: float, *, conn: Any = None) -> int:
        """The terms of the sources no longer set up become gone. How many did."""
        keep = set(configured)
        with self._lock:
            ids = [i for i, t in self._terms.items() if t.gone is None and t.source not in keep]
            for i in ids:
                self._terms[i] = replace(self._terms[i], gone=now)
            if ids:
                self._version += 1
        return len(ids)

    def terms(self) -> list[StoredTerm]:
        with self._lock:
            return list(self._terms.values())

    def kept(self) -> int:
        """Every row kept, gone ones included: what `most` counts."""
        with self._lock:
            return len(self._terms)

    def gone(self, limit: int = 1000, source: str | None = None) -> tuple[list[StoredTerm], int]:
        """The terms no longer in their source (``source``'s, or every one's), the latest gone first: at most
        ``limit`` of them, and how many there are."""
        with self._lock:
            found = [t for t in self._terms.values() if t.gone is not None and (source is None or t.source == source)]
        found.sort(key=lambda t: (-(t.gone or 0.0), t.id))
        return found[:limit], len(found)

    def remove(
        self, ids: Iterable[str] | None = None, *, source: str | None = None, conn: Any = None
    ) -> dict[str, int]:
        """Forget terms no longer in their source: these, or every one of ``source``, or every one. A term still in
        its source is left alone. How many of each type went."""
        wanted = set(ids) if ids is not None else None
        removed: Counter[str] = Counter()
        with self._lock:
            for i, t in list(self._terms.items()):
                if t.gone is None or (wanted is not None and i not in wanted):
                    continue
                if wanted is None and source is not None and t.source != source:
                    continue
                del self._terms[i]
                removed[t.type] += 1
            if removed:
                self._version += 1
        return dict(removed)

    def remove_unreadable(self, *, conn: Any = None) -> dict[str, int]:
        """Forget the gone terms no key of this server opens (B-R5): memory seals nothing, so there are none."""
        return {}

    def record_run(self, run: Run) -> None:
        """``run`` is its source's last; a sync asked for before it began is done (one asked while it ran stays for
        the next run)."""
        with self._lock:
            self._runs[run.source] = run
            asked = self._asks.get(run.source)
            if asked is not None and asked.at <= run.started:
                del self._asks[run.source]

    def forget_runs(self, names: Iterable[str], *, conn: Any = None) -> None:
        """The last runs of ``names``, and a sync asked for of them, forgotten: each runs at the next look (a source
        taken out, changed or set up again under its name: the final review's M4)."""
        with self._lock:
            for name in names:
                self._runs.pop(name, None)
                self._asks.pop(name, None)

    def runs(self) -> dict[str, Run]:
        with self._lock:
            return dict(self._runs)

    def ask(self, names: Iterable[str], by: str | None, now: float, *, conn: Any = None) -> None:
        """A sync of each of ``names`` asked for at ``now`` by ``by``: the next run of each takes it. A source whose
        last run began at ``now`` or after has it already, as `record_run` would have it: that run, kept before the
        ask, is the one a save woke as the sync was asked for, and no run is due after it."""
        with self._lock:
            for name in names:
                run = self._runs.get(name)
                if run is None or run.started < now:
                    self._asks[name] = Asked(name, now, by)

    def asks(self) -> dict[str, Asked]:
        with self._lock:
            return dict(self._asks)

    def counts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            found = Counter((t.source, t.type, t.gone is not None) for t in self._terms.values())
        return tally((source, etype, gone, n) for (source, etype, gone), n in found.items())

    def close(self) -> None:
        return None
