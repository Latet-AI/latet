"""The jobs of each instance (E4), on a thread of their own: the SIEM's delivery (`delivery.py`), the administration
log to the SIEM (`admin.py`), the license's changes and the retention, and those a later part adds before they start
(E5's prune of the record of approvals). Each job that must run on one instance at a time takes its lock: on a server,
Valkey's."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from latet.ee.events.locks import LockFn
from latet.ee.events.store import DAY, EventStore, SiemQueue
from latet.events import code_of, make_event

log = logging.getLogger(__name__)

LOCK_SECONDS = 60.0
LICENSE_JOB, LICENSE_LOCK = "license", "events:license"
RETENTION_JOB, RETENTION_LOCK = "retention", "events:retention"
RETENTION_SECONDS = 600.0  # a prune goes a day of hours a transaction: a year's log within it
_QUALIFIED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.][A-Za-z_][A-Za-z0-9_]*)*")  # a function's qualified name


def _named(run: Callable[[], bool], index: int) -> str:
    """A job as a failure names it, in fixed words: its function's qualified name (``SiemDelivery.round``), a name of
    the code and never of anything a job holds, or its place among the jobs when its name has no such form."""
    name = getattr(run, "__qualname__", None)
    if isinstance(name, str) and len(name) <= 80 and _QUALIFIED.fullmatch(name):
        return name
    return str(index + 1)


class Jobs:
    """The jobs of one instance on a thread of its own: each runs every so often, again at once when it did some
    work, the first (the SIEM's delivery) at once when ``wake`` is called. A job that fails is said, named in fixed
    words (``_named``) with the type of what it raised, and tried at its next turn. Made with ``started=False``,
    nothing runs before ``start`` (I7); once closed, nothing runs again."""

    def __init__(
        self, jobs: Sequence[tuple[float, Callable[[], bool]]], *, idle: float = 1.0, started: bool = True
    ) -> None:
        self.jobs = list(jobs)
        self._names = [_named(run, index) for index, (_, run) in enumerate(self.jobs)]
        self.idle = idle
        self._next = [0.0] * len(self.jobs)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._starting = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="latet-events-jobs", daemon=True)
        if started:
            self.start()

    def start(self) -> None:
        with self._starting:
            if self._thread.ident is None and not self._stop.is_set():
                self._thread.start()

    def add(self, every: float, run: Callable[[], bool]) -> None:
        """One more job (a later part's), before the jobs start (the edition starts them last, once it is whole: I7);
        refused once they started, when the thread reads the list, and once they are closed."""
        with self._starting:
            if self._thread.ident is not None or self._stop.is_set():
                raise RuntimeError("the jobs have started: a job is added before")
            self.jobs.append((every, run))
            self._names.append(_named(run, len(self.jobs) - 1))
            self._next.append(0.0)

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        with self._starting:
            self._stop.set()
        self._wake.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.is_set():
            busy = False
            for index, (every, run) in enumerate(self.jobs):
                if self._stop.is_set() or time.monotonic() < self._next[index]:
                    continue
                try:
                    worked = bool(run())
                except Exception as exc:  # by its type (R14): a driver's words can name an address
                    log.warning(
                        "the events' job %s failed (%s): it is tried again at its next turn",
                        self._names[index],
                        type(exc).__name__,
                    )
                    worked = False
                busy = busy or worked
                self._next[index] = time.monotonic() + (0.0 if worked else every)
            if not busy and self._wake.wait(self.idle):
                self._wake.clear()
                if self._next:
                    self._next[0] = 0.0


def _note(text: str | None) -> Mapping[str, Any] | None:
    """The license job's note, or None when there is none or it is of no shape the job writes (written by hand): the
    look is then a first look again, which only notes."""
    if text is None:
        return None
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


class LicenseWatch:
    """Each change of the license's state is a `license` event, once whatever the number of instances: its new state,
    with the notices that brought it as the reason. The first look only notes the license, and so does a look that finds
    one installed or removed: E1's `committed` records that event where it happens. And each change of the policy in
    force (``policy``: its etag, or None) is one `policy` event (R9): E2's look records none on a server, where every
    instance would record its own. A look that cannot read the policy in force notes nothing and records nothing (I7):
    never a policy "removed" then "set" because it was not made yet. The note is written only over the note the look
    read (``swap_job``), and a change recorded only by the look whose write won: two looks whose lock ran out between
    them record it once (Task 8's review)."""

    def __init__(
        self,
        licensing: Any,
        store: EventStore,
        lock: LockFn,
        record: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.time,
        policy: Callable[[], str | None] | None = None,
    ) -> None:
        self.licensing, self.store, self.lock, self.record, self.clock = licensing, store, lock, record, clock
        self.policy = policy

    def round(self) -> bool:
        with self.lock(LICENSE_LOCK, LOCK_SECONDS) as mine:
            if not mine:
                return False
            try:
                in_force = self.policy() if self.policy is not None else None
            except Exception as exc:  # by its type (R14): not read now, so nothing of it is noted nor recorded
                log.warning("the policy in force could not be read for its events (%s)", type(exc).__name__)
                return False
            standing = self.licensing.standing()
            found = standing.license
            state = str(standing.state.value)
            notices = sorted(str(notice) for notice in standing.notices)
            _, before_text = self.store.job(LICENSE_JOB)
            before = _note(before_text)
            now = {
                "id": found.id if found is not None else None,
                "state": state,
                "notices": notices,
                "policy": in_force,
            }
            text = json.dumps(now, sort_keys=True)
            if before_text == text:
                return False
            if not self.store.swap_job(LICENSE_JOB, self.clock(), text, expected=before_text):
                return False  # another instance's look moved the note since this one read it: it records the change
            if before is None:
                return False  # the first look only notes
            recorded = False
            if now["policy"] != before.get("policy"):
                outcome = "set" if now["policy"] is not None else "removed"
                self.record(make_event("policy", clock=self.clock, reason="changed", outcome=outcome))
                recorded = True
            if (now["id"], state, notices) == (before.get("id"), before.get("state"), before.get("notices")):
                return recorded
            if state == "invalid":
                outcome, reason = "invalid", code_of(getattr(standing, "reason_code", None), 80)
            elif state == "none" or now["id"] != before.get("id"):
                return recorded  # removed or installed: E1's committed() recorded that event
            else:
                outcome, reason = state, code_of(":".join(n for n in notices if n not in (state, "paused")), 80)
            self.record(make_event("license", clock=self.clock, outcome=code_of(outcome, 80), reason=reason))
            return True


class Retention:
    """What is older than ``reports.retention_days`` forgotten, once a day on one instance, and with it the batches'
    ids no longer needed and what waits for an output no longer set (what it held of the administration log counted
    apart: I5). ``days`` is read where it is asked (on a server, PostgreSQL in its own transaction: M11): one that
    cannot be read now prunes nothing, never a default in its place."""

    def __init__(
        self,
        store: EventStore,
        queue: SiemQueue,
        lock: LockFn,
        *,
        days: Callable[[], int],
        outputs: Callable[[], Sequence[str]],
        clock: Callable[[], float] = time.time,
        every: float = DAY,
    ) -> None:
        self.store, self.queue, self.lock = store, queue, lock
        self.days, self.outputs, self.clock, self.every = days, outputs, clock, every

    def _due(self, now: float) -> bool:
        last, _ = self.store.job(RETENTION_JOB)
        return last is None or now - last >= self.every

    def round(self) -> bool:
        now = self.clock()
        if not self._due(now):
            return False
        with self.lock(RETENTION_LOCK, RETENTION_SECONDS) as mine:
            if not mine or not self._due(now):  # again, under the lock: another instance may have just run it
                return False
            days = self.days()  # raises when it cannot be read: nothing is pruned (the jobs' thread says it)
            removed = self.store.prune(days, now=now)
            self.queue.forget_unknown(set(self.outputs()), before=now - 3600)
            self.store.set_job(RETENTION_JOB, now)
            log.info("the event log forgot %d rows older than %d days", removed, days)
            return True
