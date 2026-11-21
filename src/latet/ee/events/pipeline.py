"""Where a server's events go on each instance. ``put`` takes one at once, from any thread, and never waits; a thread
of its own gathers them into batches (500 at most, or what came within a second of the first), fills in what the
server knows (an identity's kind and first group), keeps of the person what the company chose (``reports.people``) and
writes each batch in one transaction: its counts, and each event in the queue of each SIEM output while the license
lets events go there. Once a minute it weighs the license again."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import queue
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from latet.ee.events.metrics import NO_METRICS, EventMetrics
from latet.ee.events.schema import NotAnEvent, checked, epoch
from latet.ee.events.store import MOST_PROJECTS, NEVER_DROPPED, OTHER, REQUESTS, EventStore
from latet.ee.settings import EnterpriseSettings

log = logging.getLogger(__name__)

__all__ = [
    "MOST_PROJECTS",
    "WAITING",
    "EventPipeline",
    "Licensed",
    "Pseudonyms",
    "SettingsCache",
    "SettingsUnread",
]

WAITING = 10_000  # events waiting in an instance's memory: past this a new one is lost, and counted
# The applications API's events take half of it at most (I13): a program calling in a loop while the database is slow
# never crowds out a person's stopped request, a sign-in or the license's events.
APPS = "apps"
# MOST_PROJECTS (the store's, never a second bound): projects of one identity in one hour on an instance, weighed
# before the person is kept as `reports.people` says; past it, OTHER (M6: a client names them).
SAID_EVERY = 60.0  # seconds between two warnings of the same kind (a batch held, a lookup that failed)
BATCH = 500
EVERY = 1.0  # seconds a batch waits for more events after its first
TICK = 60.0  # seconds between weighings of the license
CACHED = 60.0  # seconds an identity's kind and group are kept
MOST_CACHED = 100_000  # identities whose kind and group an instance keeps: past them, every one is asked again
RETRY = (0.5, 1.0, 2.0, 5.0, 10.0)  # seconds between tries of a batch the database did not take
CLOSING_TRIES = 3  # tries of a batch while the instance stops
CLOSING_WAIT = 0.5  # seconds at most between two of those tries
STALE = 300.0  # seconds the last good settings answer while they cannot be read: past them, a batch waits (M11)


class Licensed(Protocol):
    def licensed(self, feature: str) -> bool: ...
    def producing(self, feature: str) -> bool: ...


class Pseudonyms:
    """The same pseudonym for a person on every instance and in every report: ``p_`` and 10 characters of an HMAC of
    their id, with the server's key (``key`` gives it: made by the first instance that needs it, sealed with the master
    key). Without the key nobody tells whose a pseudonym is.

    The administration log's events to the SIEM name a person by a pseudonym of a domain of their own (``admin_of``):
    the Audit page names whom each entry acted on, with the entry's id and time, which its event carries too, so an
    event that shared the exchange events' pseudonym would name every exchange and report row of that person (M1 of
    the whole-branch review). The events that are not requests (a sign-in, a decision, a license, a sync, a change of
    the locked rules) take a third (``acts_of``): each is written in the same second as an entry of the log that names
    the person, so one that shared the requests' pseudonym would name their requests all the same (M1 of the final
    security review).

    A project whose name holds a term its space masks is named with the same key, under a label of its own
    (``project_of``): one name for every person and folder of that name, which nobody confirms without the key (R3 of
    the final security review's re-review)."""

    _ADMIN: Final = b"admin\0"  # no identity's id holds a NUL: the domains never meet
    _ACTS: Final = b"acts\0"
    _PROJECT: Final = b"project\0"  # nor does a project's name as an event carries one (printable alone)

    def __init__(self, key: Callable[[], bytes]) -> None:
        self._find = key
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def of(self, identity: str) -> str:
        """A person's pseudonym in the requests' events (exchange, stopped, apps) and in the reports' people."""
        return self._made(identity.encode())

    def admin_of(self, identity: str) -> str:
        """A person's pseudonym in the administration log's `admin` events: never one ``of`` gives."""
        return self._made(self._ADMIN + identity.encode())

    def acts_of(self, identity: str) -> str:
        """A person's pseudonym in the events that are not requests, and so in the reports' approvers: never one ``of``
        or ``admin_of`` gives."""
        return self._made(self._ACTS + identity.encode())

    def project_of(self, name: str) -> str:
        """What an event names a project whose ``name`` (as the event would carry it) holds a term its space masks:
        `masked-` and 8 characters of an HMAC of the name, the same for everyone; never a person's pseudonym."""
        return "masked-" + self._digest(self._PROJECT + name.encode())[:8]

    def _made(self, message: bytes) -> str:
        return "p_" + self._digest(message)[:10]

    def _digest(self, message: bytes) -> str:
        with self._lock:
            if self._key is None:
                self._key = self._find()
            key = self._key
        return base64.b32encode(hmac.new(key, message, hashlib.sha256).digest()).decode().lower()


class SettingsUnread(Exception):
    """No Enterprise settings were read yet on this instance: nothing is decided on their defaults (M11)."""


class SettingsCache:
    """The Enterprise settings, read again at most every ``every`` seconds (each batch asks how to keep its people) and
    at once after this instance saves them (``clear``). Settings that cannot be read keep the last good ones, for
    ``STALE`` seconds at most; while none were ever read, or the last good ones are older than that,
    ``SettingsUnread``: a batch waits for them rather than be kept by group only and sent nowhere, the defaults, or as
    settings the company may have changed since (M11)."""

    def __init__(
        self, load: Callable[[], EnterpriseSettings], every: float = 5.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.load, self.every, self.clock = load, every, clock
        self._value: EnterpriseSettings | None = None
        self._at = 0.0  # when they were last asked for
        self._read_at = 0.0  # when they were last read
        self._said_at = float("-inf")
        self._lock = threading.Lock()

    def __call__(self) -> EnterpriseSettings:
        with self._lock:
            now = self.clock()
            value = self._value
            stale = now - self._read_at > STALE  # read too long ago: never answered without a read that succeeds
            if value is None or stale or now - self._at >= self.every:
                try:
                    value = self._value = self.load()
                    self._read_at = now
                except Exception as exc:  # by its type: a driver's words can name the database's address
                    if value is None or stale:
                        raise SettingsUnread(type(exc).__name__) from None
                    if now - self._said_at >= SAID_EVERY:
                        self._said_at = now
                        log.warning(
                            "the Enterprise settings cannot be read (%s): the last good ones stay", type(exc).__name__
                        )
                self._at = now
            return value

    def clear(self) -> None:
        with self._lock:
            self._value = None


def _is_apps(stamped: tuple[Any, bool]) -> bool:
    item = stamped[0]
    return isinstance(item, Mapping) and item.get("type") == APPS


@dataclass(frozen=True)
class _Written:
    """What a batch the store answered leaves to count, once (R2-6): its events kept by type, those refused and why
    (``checked``'s fixed words, or none to say), whether the SIEM's queue took them, and how many were counted under
    ``OTHER`` because their identity's projects of the hour were full (M6)."""

    kept: Counter[str]
    refused: int = 0
    why: str = ""
    queued: bool = False
    others: int = 0


class EventPipeline:
    def __init__(
        self,
        store: EventStore,
        *,
        settings: Callable[[], EnterpriseSettings],
        licensing: Licensed,
        pseudonyms: Pseudonyms,
        group_of: Callable[[str], str | None] | None = None,
        kind_of: Callable[[str], str | None] | None = None,
        wake: Callable[[], None] | None = None,
        metrics: EventMetrics = NO_METRICS,
        clock: Callable[[], float] = time.time,
        every: float = EVERY,
        tick: float = TICK,
        retry: Sequence[float] = RETRY,
        started: bool = True,
    ) -> None:
        self.store, self.licensing, self.pseudonyms = store, licensing, pseudonyms
        self.settings = settings if isinstance(settings, SettingsCache) else SettingsCache(settings)
        self.group_of, self.kind_of, self.wake, self.metrics = group_of, kind_of, wake, metrics
        self.clock, self.every, self.tick, self.retry = clock, every, tick, tuple(retry) or RETRY
        self.lost = 0  # events lost: too many waiting, the database gone while the instance stopped, or put once closed
        self.refused = 0  # events that were not of the schema
        # What waits: each event with whether it could go to the SIEM when it was put (R2), and the one ``_gather``
        # took that starts the next batch (a batch keeps one answer, and one id).
        self._queue: queue.Queue[tuple[Any, bool]] = queue.Queue(WAITING)
        self._carried: tuple[Any, bool] | None = None
        self._door = threading.Lock()  # a put and close()'s last look at what waits, one after the other
        self._closed = False  # close() took what waits once the thread was gone: a later put is lost
        self._apps, self._apps_most = 0, max(1, WAITING // 2)  # the applications API's events waiting, and their room
        self._counting = threading.Lock()
        self._keeps = self._siem_on = False
        self._next_look = 0.0
        self._kinds: dict[str, tuple[str | None, float]] = {}
        self._groups: dict[str, tuple[str | None, float]] = {}
        self._projects: dict[tuple[str, int], set[str]] = {}  # each identity's projects of each hour (M6)
        self._projects_hour = 0  # the latest hour weighed: when it moves on, the hours before the one it left go
        self._overflowed = 0  # events of the batch being shaped counted under OTHER: counted once it is written
        self._said: dict[str, float] = {}  # when each kind of warning was said last
        self._stop = threading.Event()
        self.look()
        self._thread = threading.Thread(target=self._run, name="latet-events", daemon=True)
        if started:
            self._thread.start()

    def start(self) -> None:
        """Its thread, for a pipeline made with ``started=False``: a server's edition starts it last, once it is whole
        (I7); what was put meanwhile waits for it."""
        if self._thread.ident is None:
            self._thread.start()

    # --- any thread -------------------------------------------------------------------------------------------------

    def keeps(self) -> bool:
        """Whether events are kept now: the license lists reports or the SIEM. Cheap, in memory: weighed once a
        minute, on the pipeline's thread."""
        return self._keeps

    def siem_on(self) -> bool:
        """Whether events go to the SIEM now: the license lists it and is not paused."""
        return self._siem_on

    def put(self, event: Mapping[str, Any]) -> None:
        """Take an event to keep, at once, in memory: never a read of the license or of the database (the event loop
        calls it). While no license keeps events, every one is dropped here, whatever its type. Each is stamped with
        whether events go to the SIEM now: one put while nothing is produced never goes there afterwards (R2). When too
        many wait, or once close() found the thread gone, it is lost and counted; the applications API's take half the
        room at most (I13): one past it is the one lost, never an event of another type. It never raises."""
        if not self._keeps:
            return
        item = dict(event) if isinstance(event, Mapping) else event  # what is not a mapping is refused by the writer
        stamped = (item, self._siem_on)
        with self._door:  # never let in once close() took what waits: nothing would ever write it
            taken = not self._closed and self._let_in(stamped)
        if not taken:
            self._lose(1)

    def _let_in(self, stamped: tuple[Any, bool]) -> bool:
        """``stamped`` into what waits, if there is room for it (I13: the applications API's in half of it at most)."""
        if _is_apps(stamped):
            with self._counting:
                if self._apps >= self._apps_most:
                    return False
                self._apps += 1
        try:
            self._queue.put_nowait(stamped)
        except queue.Full:
            self._took(stamped)
            return False
        return True

    def _lose(self, n: int) -> None:
        with self._counting:
            self.lost += n
        self._count(self.metrics.lost, n)

    def _count(self, metric: Callable[[Any], None], value: Any) -> None:
        """A metric counted, its failure said by its type and never raised to whoever put or wrote the events: a batch
        written is never held, nor written again, for a metric (R2-6)."""
        try:
            metric(value)
        except Exception as exc:  # by its type (R14)
            self._say("metrics", "an event metric could not be counted (%s)", type(exc).__name__)

    def _took(self, stamped: tuple[Any, bool]) -> None:
        """An event leaves what waits (taken by the writer, or never let in): its room is free again."""
        if _is_apps(stamped):
            with self._counting:
                self._apps -= 1

    def _say(self, what: str, message: str, *args: object) -> None:
        """A warning said once a minute at most for each ``what``: a database that does not answer is said, never a
        line an event."""
        now = time.monotonic()
        if now - self._said.get(what, float("-inf")) >= SAID_EVERY:
            self._said[what] = now
            log.warning(message, *args)

    def look(self) -> None:
        """Weigh the license now: whether events are kept (what protects' answer, which a trial's end, a pause and a
        file that cannot be used leave on: the log keeps counting), and whether they go to the SIEM (only while the
        license produces: R2)."""
        self._next_look = time.monotonic() + self.tick
        try:
            self._keeps = self.licensing.licensed("reports") or self.licensing.licensed("siem")
            self._siem_on = self.licensing.producing("siem")
        except Exception as exc:  # by its type (R14): a driver's words can name the database's address
            self._say("license", "could not weigh the license (%s): events are kept as before", type(exc).__name__)

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until every event taken so far is written (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        """Write what waits (a few tries while the database does not answer), then stop. Once the thread is gone (or
        never started), what still waits (put as it ended) is lost and counted, and so is whatever is put after."""
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=30)
        if self._thread.is_alive():
            return  # still writing: what waits is its own, and it writes it before it ends
        with self._door:
            self._closed = True
            left = [] if self._carried is None else [self._carried]  # taken from the queue already
            self._carried = None
            while True:
                try:
                    stamped = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._took(stamped)
                left.append(stamped)
        if left:
            self._lose(len(left))
            log.error("%d events were not kept: the pipeline closed before they were written", len(left))
            for _ in left:
                self._queue.task_done()

    # --- the writer's thread ----------------------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                batch, siem = self._gather()
                if batch:
                    self._held(batch, siem)
                elif self._stop.is_set():
                    return
                self._look_if_due()
            except Exception as exc:  # the thread never ends on what failed (I6): said by its type
                self._say("thread", "the events' thread met an error (%s): it goes on", type(exc).__name__)
                self._stop.wait(0.1)

    def _look_if_due(self) -> None:
        """The license weighed again when it is due, never while the instance stops: each closing try would add a
        connection's timeout while the database does not answer, and the last answer stays."""
        if not self._stop.is_set() and time.monotonic() >= self._next_look:
            self.look()

    def _gather(self) -> tuple[list[Any], bool]:
        """The events of the next batch, and whether they could go to the SIEM when they were put: the first that
        comes, and those that follow within ``every`` seconds with the same answer. One with the other answer ends the
        batch and starts the next: a batch keeps one answer, and one id (R2, R2-6)."""
        first, self._carried = self._carried, None
        if first is None:
            try:
                first = self._queue.get(timeout=0.2)
            except queue.Empty:
                return [], False
            self._took(first)
        siem = first[1]
        batch = [first[0]]
        deadline = time.monotonic() + self.every
        while len(batch) < BATCH:
            wait = 0.0 if self._stop.is_set() else deadline - time.monotonic()
            try:
                item = self._queue.get(timeout=wait) if wait > 0 else self._queue.get_nowait()
            except queue.Empty:
                break
            self._took(item)
            if item[1] != siem:
                self._carried = item  # taken from the queue, and done with the batch it starts
                break
            batch.append(item[0])
        return batch, siem

    def _held(self, raw: list[Any], siem: bool) -> None:
        """A batch kept whatever fails while it is shaped or written (I6): a pseudonyms' key or an identity's group the
        server did not give, settings never read yet or too long ago (M11), any error but the schema's, which ``_keep``
        refuses event by event and drops for a batch the log refuses whole. Tried again with ``retry``, the license
        weighed again first but while the instance stops (a pause that began meanwhile keeps it from the SIEM: R2),
        each try shaping the events
        again from what was put; said by its type once a minute; while the instance stops, a few tries, then lost and
        counted. The batch's id is drawn once, here (R2-6): a write whose answer was lost is tried again under the same
        id, which the store counts once. Once the store answered, the batch is written: what is counted after (its
        metrics, its refusals, the SIEM's wake) never fails it, holds it nor writes it again. The thread never ends on
        it."""
        tries, batch = 0, uuid.uuid4().hex
        try:
            while True:
                try:
                    self._look_if_due()
                    written = self._keep(raw, batch, siem)  # the store's answer is its last step
                except Exception as exc:  # by its type (R14): a driver's words can name an address
                    tries += 1
                    self._next_look = 0.0  # the license weighed again before the next try (R2)
                    if self._stop.is_set() and tries >= CLOSING_TRIES:
                        self._lose(len(raw))
                        log.error("%d events were not kept: the instance stops (%s)", len(raw), type(exc).__name__)
                        return
                    self._say("held", "%d events wait to be kept (%s): trying again", len(raw), type(exc).__name__)
                    self._pause(tries)
                    continue
                if written is not None:
                    self._counted(written)
                return
        finally:
            for _ in raw:
                self._queue.task_done()

    def _pause(self, tries: int) -> None:
        """The wait before a batch's next try: ``retry``'s, cut short when the instance stops, which then gives each
        batch a few short tries."""
        delay = self.retry[min(tries - 1, len(self.retry) - 1)]
        if self._stop.is_set():
            time.sleep(min(delay, CLOSING_WAIT))
        else:
            self._stop.wait(delay)

    def _keep(self, raw: list[Any], batch: str, siem: bool) -> _Written | None:
        """One batch shaped from what was put and written, or raised for ``_held`` to try again whole; the store's
        answer is its last step, and what it leaves to count is counted after, once (R2-6). An event the schema refuses
        is refused alone; a batch the log refuses whole (``NotAnEvent``: it would refuse it again) is dropped, never
        tried again. The events go to the SIEM's queue only when they could go there both when they were put and now
        (R2). None: nothing to count (no license for reports or the SIEM)."""
        if not self._keeps:
            return None
        self._overflowed = 0  # this try's: a batch shaped again counts its overflow once
        settings = self.settings()  # SettingsUnread while none were read, or too long ago (M11): the batch waits
        events: list[dict[str, Any]] = []
        refused, why = 0, ""
        for item in raw:
            try:
                events.append(self._shaped(item, settings.reports.people))
            except NotAnEvent as exc:  # `checked`'s fixed words: a field's name or a count, never a value
                refused += 1
                why = why or str(exc)
        if not events:
            return _Written(Counter(), refused, why)
        outputs = [output.name for output in settings.siem.outputs] if siem and self._siem_on else []
        kept = Counter(str(event["type"]) for event in events)
        try:
            self.store.write(batch, events, outputs, at=self.clock())  # False: written before, counted once
        except NotAnEvent:  # the log refuses the batch whole, and would again: dropped, said by its type
            log.error("%d events were not kept: the event log refused their batch (NotAnEvent)", len(raw))
            return _Written(Counter(), len(raw))
        return _Written(kept, refused, why, bool(outputs), self._overflowed)

    def _counted(self, written: _Written) -> None:
        """What a batch written leaves to count, counted once (R2-6), never raised: a metric that fails is said by its
        type, and a SIEM's wake that fails is said and never tried again (the delivery's own tick sends them)."""
        if written.kept:
            self._count(self.metrics.kept, written.kept)
        if written.others:
            self._count(self.metrics.projects_full, written.others)
        if written.refused:
            self._refuse(written.refused)
            if written.why:
                log.warning("%d events that are not of the schema were not kept: %s", written.refused, written.why)
        if written.queued and self.wake is not None:
            try:
                self.wake()
            except Exception as exc:  # by its type (R14)
                log.warning("the SIEM's delivery was not woken (%s)", type(exc).__name__)

    def _refuse(self, n: int) -> None:
        """Events not kept because they are not of the schema: counted, among the metrics' events lost."""
        self.refused += n
        self._count(self.metrics.lost, n)

    def _shaped(self, raw: Any, people: str) -> dict[str, Any]:
        """``raw`` checked, its kind and group filled in from the server, its person kept as the company chose, and
        checked again: what the server filled in is the schema's too, or the event is refused alone. An event of the
        administration log is refused: it goes to the SIEM's queue with its entry's id (Task 8), never through here.
        Under `pseudonymous`, a request names its person by the requests' pseudonym, any other event by the one of the
        events that are not requests (M1 of the final security review)."""
        event = checked(raw)
        if event["type"] == NEVER_DROPPED:
            raise NotAnEvent("an event of the administration log goes to the SIEM with its entry, never through here")
        identity = event["identity"]
        event["project"] = self._project(identity, event)  # weighed for the person, before they are kept as chosen
        if identity is not None and identity.startswith("u_"):
            if event["kind"] is None and self.kind_of is not None:
                event["kind"] = self._cached(self._kinds, identity, self.kind_of)
            if event["group"] is None and self.group_of is not None:
                event["group"] = self._cached(self._groups, identity, self.group_of)
        pseudonym = self.pseudonyms.of if event["type"] in REQUESTS else self.pseudonyms.acts_of
        event["identity"] = self._person(identity, event["kind"], people, pseudonym)
        event["by"] = self._person(event["by"], "person", people, pseudonym)  # who decided is a person, kept as one
        return checked(event)

    def _project(self, identity: str | None, event: Mapping[str, Any]) -> str | None:
        """The event's project, or ``OTHER`` past ``MOST_PROJECTS`` names of one identity in one hour (M6): a client
        that names a new folder at each request adds a row an hour for each, kept for the retention, else. Weighed for
        the identity the event names, before its person is kept as ``reports.people`` says: with ``group-only`` the
        rows keep no person, and each person's projects are bounded all the same."""
        project = event["project"]
        if project is None:
            return None
        hour = int(epoch(event["ts"]) // 3600)
        if hour > self._projects_hour:  # only the writer's thread reads it: the hours gone are forgotten
            self._projects = {key: names for key, names in self._projects.items() if key[1] >= hour - 1}
            self._projects_hour = hour
        names = self._projects.setdefault((identity or "", hour), set())
        if project in names or len(names) < MOST_PROJECTS:
            names.add(project)
            return project
        self._overflowed += 1
        self._say("projects", "an identity named more than %d projects in an hour: the others count as %s",
                  MOST_PROJECTS, OTHER)  # fmt: skip
        return OTHER

    @staticmethod
    def _person(identity: str | None, kind: str | None, people: str, pseudonym: Callable[[str], str]) -> str | None:
        """A person as ``reports.people`` keeps them: never, by ``pseudonym`` (the domain of the event's type), or by
        their id. An API key is a program: kept by its id. A pseudonym stays one, and ``group-only`` keeps none either.
        A choice it does not know keeps no person."""
        if identity is None or kind == "key":
            return identity
        if people == "named":
            return identity
        if people == "pseudonymous":
            return identity if identity.startswith("p_") else pseudonym(identity)
        return None

    def _cached(
        self, cache: dict[str, tuple[str | None, float]], identity: str, find: Callable[[str], str | None]
    ) -> str | None:
        """What ``find`` says of an identity, asked again after ``CACHED`` seconds. When it cannot say now, its last
        answer, out of date, said by its type once a minute; with none, it raises, and the batch waits for the server's
        answer (I6), never kept without it."""
        now = time.monotonic()
        found = cache.get(identity)
        if found is None or found[1] < now:
            try:
                answer = find(identity)
            except Exception as exc:  # by its type (R14)
                if found is None:
                    raise
                self._say(
                    "lookup", "could not look an identity up for its events (%s): its last answer", type(exc).__name__
                )
                return found[0]
            if len(cache) >= MOST_CACHED:
                cache.clear()
            found = cache[identity] = (answer, now + CACHED)
        return found[0]
