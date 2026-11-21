"""Stand-ins of what E4 is given on a server, for the unit tests: a license as the pipeline weighs it, a clock the
test moves, metrics kept to be read, the administration log as E0b's `AuditLog` answers, and a license's standing as
E1 weighs it."""

import hashlib
import hmac
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace


class Features:
    """A license as E4 weighs it: the features it lists, and whether it is paused."""

    def __init__(self, *features: str, paused: bool = False) -> None:
        self.features, self.paused = set(features), paused

    def licensed(self, feature: str) -> bool:
        return feature in self.features

    def producing(self, feature: str) -> bool:
        return feature in self.features and not self.paused


class Clock:
    """A clock the test moves."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingMetrics:
    """The metrics an instance gives E0c's registry, kept to be read; ``labels`` holds every label given."""

    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.labels: set[str] = set()

    def kept(self, counts):
        for kind, n in counts.items():
            self.counts["kept", kind] += n
            self.labels.add(kind)

    def lost(self, n):
        self.counts["lost"] += n

    def projects_full(self, n):
        self.counts["projects_full"] += n

    def sent(self, output, n):
        self.counts["sent", output] += n
        self.labels.add(output)

    def failed(self, output):
        self.counts["failed", output] += 1
        self.labels.add(output)

    def dropped(self, output, n):
        self.counts["dropped", output] += n
        self.labels.add(output)

    def queued(self, output, n):
        self.counts["queued", output] = n
        self.labels.add(output)


HEAD = "c" * 64  # an entry's hash, as E0b's keyed chain gives one: 64 hexadecimal digits


@dataclass(frozen=True)
class Entry:
    """An entry of the administration log, as E0b's `AuditEntry` has it, with its place in the chain."""

    id: int
    ts: float
    actor: str
    action: str
    target: str | None = None
    details: dict = field(default_factory=dict)
    address: str | None = None
    prev: str = ""
    hash: str = HEAD


class FakeAudit:
    """The administration log, as E0b's `AuditLog` answers: ``query`` the entries of a period (``until`` left out),
    newest first, at most ``limit``; ``after`` those above an id and not above the last id its sequence gave, within
    ``[since, until)``, oldest first, a page at most. A row added ``by_hand`` takes no id from the sequence."""

    KEY = bytes([9]) * 32  # the fake's key for a note's tag: E0b's is the log's own

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.broken: set[int] = set()  # ids of rows written by hand: their keyed hash does not hold (R4-4)
        self.through = 0  # the last id a prune dropped (R4-2)
        self.sequence = 0  # the last id the log's sequence gave

    def add(self, *entries: Entry, by_hand: bool = False) -> None:
        self.entries += entries
        if not by_hand:
            self.sequence = max([self.sequence, *(e.id for e in entries)])

    def query(self, *, since=None, until=None, action=None, actor=None, limit=500):
        found = [
            e for e in self.entries
            if (since is None or e.ts >= since) and (until is None or e.ts < until)
            and (action is None or e.action == action) and (actor is None or e.actor == actor)
        ]  # fmt: skip
        return sorted(found, key=lambda e: e.id, reverse=True)[: max(1, min(limit, 100_000))]

    def after(self, after_id, limit=1000, *, since=None, until=None, action=None, **filters):
        found = (
            e for e in self.entries
            if after_id < e.id <= self.sequence and (action is None or e.action == action)
            and (since is None or e.ts >= since) and (until is None or e.ts < until)
        )  # fmt: skip
        return sorted(found, key=lambda e: e.id)[: max(1, min(limit, 10_000))]

    def newest_id(self):
        return max((e.id for e in self.entries if e.id <= self.sequence), default=0)

    def record(self, actor, action, target=None, details=None, address=None, *, conn=None):
        """An entry kept as E0b's `AuditLog.record` keeps one: the next id, chained (its hashes are the fake's)."""
        last = max((e for e in self.entries if e.id <= self.sequence), key=lambda e: e.id, default=None)
        self.add(Entry(self.sequence + 1, last.ts if last else 0.0, actor, action, target, dict(details or {}),
                       address))  # fmt: skip

    def holds(self, entry):
        return entry.id not in self.broken

    def first_kept(self):
        return self.through + 1

    def tag(self, text):
        return hmac.new(self.KEY, b"audit-note:" + text.encode(), hashlib.sha256).hexdigest()

    def tagged(self, text, tag):
        return isinstance(tag, str) and hmac.compare_digest(tag.encode(), self.tag(text).encode())

    def noted(self, conn, action, details):
        """Task 4's `Noting`, as Task 11 gives it on a server: the entry naming a gap marker's range, by `server`."""
        self.record("server", action, details.get("output"), dict(details), conn=conn)


def standing(license_id, state, notices=(), reason_code=None):
    """A license's standing, with what E4 reads of E1's `Standing`."""
    return SimpleNamespace(
        license=SimpleNamespace(id=license_id) if license_id else None, state=SimpleNamespace(value=state),
        notices=tuple(notices), reason_code=reason_code,
    )  # fmt: skip
