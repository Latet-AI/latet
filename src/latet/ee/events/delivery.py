"""Each SIEM output's queue, sent by one instance at a time: ``round`` takes, for each output that is due, its lease
(`Bus.lease("siem:<output>")` on a server) and sends one batch, in the queue's order. A batch leaves the queue only once
the receiver took it, and only while the lease is still this instance's (C1); one it cannot take now is tried again
(5 s, 10 s, 30 s, 1 min, then every 5 min, the same for every instance); one it refuses for good is sent again in
halves until the event it refuses is alone, which is dropped and counted, but an entry of the administration log, which
is never dropped: the queue waits at it (I5). Whatever one output raises is that output's failure, never a stop of the
others (I8). Nothing is sent while ``producing`` says no (a paused license): what waits goes out after.

Each output connects only where `siem.py` lets it (R10: no proxy, the address checked by egress at each connection, each
output's lookups counted for it alone). A send runs an event loop of its own (``asyncio.run``), so ``round`` and
``test`` run in a worker thread (on a server, the jobs' thread), never on an event loop: called on one, they refuse."""

from __future__ import annotations

import asyncio
import logging
import ssl
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from latet.ee.events.locks import Lease, LeaseFn
from latet.ee.events.metrics import NO_METRICS, EventMetrics
from latet.ee.events.siem import Output, SecretElsewhere, SendError, default_context, make_output, send_test
from latet.ee.events.store import ADMIN_KEPT, NEVER_DROPPED, SLACK, Noting, QueueState, SiemQueue
from latet.ee.settings import SiemOutput

log = logging.getLogger(__name__)

# Events a send at most: large, since syslog waits up to siem.CLOSE_SECONDS for the receiver's close at each send.
BATCH = 500
LIMIT = 200_000  # events each output's queue keeps: past this the oldest are dropped, `apps` first, `admin` never
LOCK_SECONDS = 60.0  # a lock outlives one send whole (siem.SEND_SECONDS): one that dies holding it frees it in a minute
# Seconds a round's turns go on: past them the round ends after the turn under way, and says there is more to do, so
# that one slow receiver (a send may last siem.SEND_SECONDS) holds neither the other outputs nor the instance's other
# jobs (the administration log's, the license's) behind it; each round begins one output past where the last began.
ROUND_SECONDS = 10.0

RETRY_DELAYS = (5.0, 10.0, 30.0, 60.0, 300.0)
REBUILD_EVERY = 60.0  # seconds: the outputs are made again, their secrets read again (one set on another instance)
TRIM_EVERY = 60.0  # seconds between two bounds of a queue on an instance: its limit, and the administration log's
SAID_EVERY = 60.0  # seconds between two warnings of the same kind (Valkey or the database that does not answer)
# Seconds of its lease a send, or the batch's leaving the queue, needs left (C1): within them no other instance can take
# the lease, collapse the batch's rows into a gap marker or send them, before what this one does lands.
MARGIN = 5.0
ELSEWHERE = "its secret was set for another address: set it again (`latet server siem secret {name}`)"
UNREAD = "its secret could not be read now ({kind})"
LATE = "its sends took longer than its lock allows: tried again later"
EVERY_REFUSED = (
    "it took none of the events it was sent, each refused alone: none dropped, tried again later (is it set to take"
    " them?)"
)


Batch = list[tuple[int, dict[str, Any], str]]  # a queue's rows as `peek_rows` gives them: (place, event, its text)


class _LockLost(Exception):
    """The lease ran out while a batch was out: the next holder sends it (at least once)."""


class _Held(SendError):
    """The receiver refused events at the queue's head, each alone, and took none before them: the queue waits at them
    until it takes them or the administrator skips them (``held_to``: the last one's place). ``blocked``: the head is
    an entry of the administration log it refuses, alone (I5); else a run of other events, ending at an entry or not
    (Task 7's fix round 2: never a head `skip` cannot pass)."""

    def __init__(self, message: str, held_to: int, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.held_to, self.blocked = held_to, blocked


def _off_loop() -> None:
    """RuntimeError on a thread that runs an event loop: a send runs one of its own, and would hold the server's."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("the SIEM's delivery runs in a worker thread, never on an event loop")


class SiemDelivery:
    """The SIEM's delivery on one instance. ``outputs`` reads the outputs as the settings say now; ``secret`` an
    output's secret as it was sealed for where it sends now (``siem.secret_of``: ``SecretElsewhere`` for one set for
    another address, I1); ``lease`` takes a lock that says whether it is still this instance's (`Bus.lease`).
    ``producing`` says whether the license lets events go to the SIEM now (R2). ``origin`` names the gap markers;
    past ``admin_kept`` events of the administration log in a queue the oldest collapse into one marker, down to
    ``admin_kept - slack``, and ``noted`` writes the collapse's entry of the log in its transaction (R3-1). The
    metrics are told each output by its name (E0c's registry labels it by its kind: R13). A round's turns stop once
    ``budget`` seconds went (``ROUND_SECONDS``), the next round taking up the outputs after the turn it ended at."""

    def __init__(
        self,
        queue: SiemQueue,
        outputs: Callable[[], Sequence[SiemOutput]],
        secret: Callable[[SiemOutput], str | None],
        lease: LeaseFn,
        *,
        producing: Callable[[], bool] | None = None,
        context: ssl.SSLContext | None = None,
        host: str | None = None,
        clock: Callable[[], float] = time.time,
        delays: Sequence[float] = RETRY_DELAYS,
        lock_seconds: float = LOCK_SECONDS,
        limit: int = LIMIT,
        metrics: EventMetrics = NO_METRICS,
        origin: str = "",
        admin_kept: int = ADMIN_KEPT,
        slack: int = SLACK,
        noted: Noting | None = None,
        budget: float = ROUND_SECONDS,
    ) -> None:
        if not delays:
            raise ValueError("a delivery waits one delay at least before it tries again")
        self.queue, self.outputs, self.secret, self.lease = queue, outputs, secret, lease
        self.producing: Callable[[], bool] = producing or (lambda: True)
        self.context, self.host, self.clock = context or default_context(), host, clock
        self.delays = tuple(delays)
        self.lock_seconds, self.limit, self.metrics = lock_seconds, limit, metrics
        self.origin, self.admin_kept, self.slack, self.noted = origin, admin_kept, slack, noted
        self._built: dict[str, tuple[SiemOutput, Output]] = {}  # each output's object, made with its secret
        self._built_at = float("-inf")
        self._known: set[str] | None = None  # the outputs the last round saw: one gone since was removed (R2-1)
        self._trimmed: dict[str, float] = {}
        self._said: dict[str, float] = {}
        self.budget = budget
        self._first = 0  # where among the outputs the next round begins (each round one past the last's beginning)
        self._closed = False
        self._mutex = threading.Lock()

    def round(self) -> bool:
        """One batch for each output that is due and whose lease this instance takes: True when one was delivered (the
        next may follow at once). Never raises for an output, nor for settings or a license that cannot be read now:
        those send nothing, and forget no queue."""
        _off_loop()
        with self._mutex:
            if self._closed:
                return False
            try:
                wanted = self._sync()
                if not wanted or not self.producing():
                    return False
            except Exception as exc:  # by its type (R14): nothing is sent, and no output is taken as removed
                self._say(
                    "round",
                    "the SIEM's delivery waits: its outputs or the license cannot be read (%s)",
                    type(exc).__name__,
                )
                return False
            sent = False
            names = list(wanted)
            start = self._first % len(names)
            self._first = start + 1  # the next round begins one past this one's beginning, or where this one ends
            deadline = time.monotonic() + self.budget
            for index, name in enumerate(names[start:] + names[:start]):
                if self._closed:
                    break
                if index and time.monotonic() >= deadline:  # one slow receiver holds no other behind it
                    self._first = start + index
                    return True
                try:
                    sent = self._turn(name, wanted[name]) or sent
                except Exception as exc:  # I8: an output's lease or queue that fails stops none of the others
                    self._say(f"turn:{name}", "SIEM output %s: its turn failed (%s)", name, type(exc).__name__)
            return sent

    def refresh(self) -> None:
        """The outputs or a secret changed: they are made again before the next batch."""
        self._built_at = float("-inf")

    def test(self, name: str) -> tuple[bool, str]:
        """Send a test event to one output now, in a worker thread: whether it arrived, and why not."""
        _off_loop()
        settings = next((output for output in self.outputs() if output.name == name), None)
        if settings is None:
            return False, "there is no output of that name"
        try:
            secret = self.secret(settings)
        except SecretElsewhere:
            return False, ELSEWHERE.format(name=settings.name)
        except Exception as exc:  # by its type: a store's words can name where it is
            return False, UNREAD.format(kind=type(exc).__name__)
        return send_test(settings, secret, self.context, host=self.host)

    def state(self) -> dict[str, QueueState]:
        return {output.name: self.queue.state(output.name) for output in self.outputs()}

    def close(self) -> None:
        """No batch more: a round under way ends after the output it sends, and every output's object is closed, with
        the secret it held."""
        self._closed = True
        with self._mutex:
            for name, (_, output) in list(self._built.items()):
                self._closing(name, output)
            self._built.clear()

    def _turn(self, name: str, settings: SiemOutput) -> bool:
        """One batch of ``name``'s queue when it is due and this instance takes its lease: whether one was delivered."""
        retry_at, _ = self.queue.schedule(name)
        if retry_at is not None and retry_at > self.clock():
            return False  # failing: every instance waits the same delay
        with self.lease(f"siem:{name}", self.lock_seconds) as lease:
            if not lease:
                return False  # another instance sends this queue now
            try:
                return self._deliver(name, settings, lease)
            except _LockLost:  # a failure while the lease is still its own, with too little of it left; else nothing
                self._failed(name, LATE, lease)
            except Exception as exc:  # I8: by its type, whatever it is (httpx raised UnicodeEncodeError); tried again
                self._failed(name, f"it could not be sent ({type(exc).__name__})", lease)
        return False

    def _deliver(self, name: str, settings: SiemOutput, lease: Lease) -> bool:
        """Under the output's lease: its queue kept within its bounds, then one batch sent."""
        now = self.clock()
        retry_at, _ = self.queue.schedule(name)  # again, under the lease: another instance may have failed meanwhile
        if retry_at is not None and retry_at > now:
            return False
        if now - self._trimmed.get(name, float("-inf")) >= TRIM_EVERY:
            self._trimmed[name] = now
            self._bound(name, now)
        batch = self.queue.peek_rows(name, BATCH)
        if not batch:
            return False
        try:
            self._send(name, self._object(name, settings), batch, lease)
        except _Held as held:  # what it refuses holds the queue: an entry of the log (I5), or a run the skip passes
            self._failed(name, str(held), lease, blocked=held.blocked, held_to=held.held_to)
            return False
        except SendError as exc:  # it may take it later
            self._failed(name, str(exc), lease)
            return False
        return True

    def _bound(self, name: str, now: float) -> None:
        """The queue kept to its newest ``limit`` events (`apps` first, `admin` never: I5, I13); and past ``admin_kept``
        of the administration log's events, the oldest collapsed into one gap marker down to ``admin_kept - slack``,
        so that a long outage makes one collapse, not one a round (each collapse's own `siem.gap` entry is queued in its
        turn), the entry written in the collapse's transaction (R3-1). Under the lease, before the batch is read.
        A bound that cannot be kept now (the database, the log's entry) is said by its type and tried again in a
        minute: the batch goes all the same, and nothing is written as the receiver's failure."""
        try:
            dropped = self.queue.trim(name, self.limit)
        except Exception as exc:  # by its type (R14)
            self._say(f"trim:{name}", "SIEM output %s: its queue could not be kept short now (%s)", name,
                      type(exc).__name__)  # fmt: skip
        else:
            if dropped:
                self._count(self.metrics.dropped, name, dropped)
                log.warning("SIEM output %s: %d events dropped, past the newest %d", name, dropped, self.limit)
        try:
            collapsed = self._collapsed(name, now)
        except Exception as exc:  # by its type (R14): a collapse whose entry fails leaves no marker (R3-1)
            self._say(f"collapse:{name}", "SIEM output %s: its administration log's bound could not be kept now (%s)",
                      name, type(exc).__name__)  # fmt: skip
            return
        if collapsed is not None:  # FAIL in the status until the output delivers again
            first, last, count = collapsed
            log.warning(
                "SIEM output %s: entries %d to %d of the administration log (%d) collapsed into one gap marker",
                name, first, last, count,
            )  # fmt: skip

    def _collapsed(self, name: str, now: float) -> tuple[int, int, int] | None:
        """Past ``admin_kept`` events of the administration log waiting, the oldest collapsed down to ``admin_kept -
        slack`` with the collapse's entry of the log (R3-1): its range, or None."""
        if self.queue.admin_waiting(name) <= self.admin_kept:
            return None
        if self.noted is None:  # a marker and its entry of the log, or neither (R3-1)
            self._say(f"bound:{name}", "SIEM output %s: past its bound of the administration log, nothing collapsed: "
                      "no entry of the log could record it", name)  # fmt: skip
            return None
        return self.queue.collapse(
            name, self.admin_kept, origin=self.origin, at=now, keep=self.admin_kept - self.slack, noted=self.noted
        )

    def _send(self, name: str, output: Output, batch: Batch, lease: Lease) -> None:
        """``batch`` sent and taken off the queue, in order, each send and each leaving with ``MARGIN`` seconds of the
        lease left (C1). A batch the receiver refuses for good (a 4xx but those it may take later) is sent again in
        halves, in order, until each event it refuses is alone. Those are dropped and counted once it took another
        event of the batch (I5), but an entry of the administration log, which holds the queue (``_Held``, blocked).
        When it took none, the receiver refuses everything (it is set wrong): nothing is dropped, and the run it refused
        holds the queue (``_Held``, not blocked), a failure tried again later that the administrator may skip, since
        dropping would empty the queue in minutes. An entry it refuses after other events it refused, taking none, is
        so held too: blocked only when it is the head once this try's drops are done, so that `skip` always passes what
        holds the queue (fix round 2). A failure to retry raises: what was taken off before stays taken, and nothing
        after it is sent."""
        refused: list[tuple[int, SendError]] = []  # each event refused alone, and why
        took: list[int] = []  # the sizes of the parts the receiver took
        try:
            self._halves(name, output, batch, lease, refused, took)
        except _Held as held:
            if refused and not took:  # events it refused before the entry are the head: a run, not the entry
                raise _Held(f"{refused[-1][1]}: {EVERY_REFUSED}", held.held_to) from None
            if refused:  # it took others: those it refused before the entry are dropped, and the entry is the head
                self._drop(name, refused, lease)
            raise
        except Exception:
            if refused and took:
                self._drop(name, refused, lease)
            raise
        if refused and not took:
            raise _Held(f"{refused[-1][1]}: {EVERY_REFUSED}", refused[-1][0])
        if refused:
            self._drop(name, refused, lease)

    def _halves(
        self,
        name: str,
        output: Output,
        batch: Batch,
        lease: Lease,
        refused: list[tuple[int, SendError]],
        took: list[int],
    ) -> None:
        """One part of a batch sent, and taken off the queue when the receiver takes it; refused for good, its halves,
        each in its turn: see ``_send``."""
        if not lease.held(MARGIN):  # C1: no send that may end after the lease, nor once it is another's
            raise _LockLost
        try:
            output.send([event for _, event, _ in batch])
        except SendError as exc:
            if exc.retry:
                raise
            if len(batch) > 1:
                half = len(batch) // 2
                self._halves(name, output, batch[:half], lease, refused, took)
                self._halves(name, output, batch[half:], lease, refused, took)
                return
            seq, event, _ = batch[0]
            if event.get("type") == NEVER_DROPPED:
                raise _Held(
                    f"{exc}: an entry of the administration log, kept and tried again", seq, blocked=True
                ) from None
            refused.append((seq, exc))
            return
        if not lease.held(MARGIN):  # C1: a send that outlived the lock, or nearly, takes nothing off the queue
            raise _LockLost
        # Only the rows that still hold the text sent: one a collapse or a skip made a marker of meanwhile stays.
        self.queue.done(name, [seq for seq, _, _ in batch], at=self.clock(), texts=[text for _, _, text in batch])
        took.append(len(batch))
        self._count(self.metrics.sent, name, len(batch))

    def _drop(self, name: str, refused: list[tuple[int, SendError]], lease: Lease) -> None:
        """The events the receiver refused, each alone, taken off and counted while the lease is still this
        instance's (C1); said once a minute at most, in the receiver's words as `siem.py` gives them."""
        if not lease.held():
            raise _LockLost
        dropped = self.queue.drop(name, [seq for seq, _ in refused], str(refused[-1][1]))
        if dropped:
            self._count(self.metrics.dropped, name, dropped)
            self._say(f"refused:{name}", "SIEM output %s refused events, each alone (%s): %d dropped", name,
                      refused[-1][1], dropped)  # fmt: skip

    def _failed(self, name: str, why: str, lease: Lease, *, blocked: bool = False, held_to: int | None = None) -> None:
        """A failure of the output, tried again after its delay, the same for every instance. One that outlived the
        lease is not written: the next holder tries, and says how it went (C1)."""
        if not lease.held():
            log.info("SIEM output %s: its lock ran out while it sent: the next holder tries again", name)
            return
        now = self.clock()
        _, tries = self.queue.schedule(name)
        wait = self.delays[min(tries, len(self.delays) - 1)]
        self.queue.failed(name, why, at=now, retry_at=now + wait, blocked=blocked, held_to=held_to)
        self._count(self.metrics.failed, name)

    def _object(self, name: str, settings: SiemOutput) -> Output:
        """The output's object, made with its secret when first needed (again when its settings change, at ``refresh``
        and every ``REBUILD_EVERY``). SendError, held, when its secret was set for another address (I1) or cannot be
        read now: nothing is sent, and it is tried again."""
        found = self._built.get(name)
        if found is not None:
            return found[1]
        try:
            secret = self.secret(settings)
        except SecretElsewhere:  # I1: sealed for where it sent before; never sent here
            raise SendError(ELSEWHERE.format(name=name)) from None
        except Exception as exc:  # by its type (R14): a store's words can name where it is
            raise SendError(UNREAD.format(kind=type(exc).__name__)) from None
        output = make_output(settings, secret, self.context, host=self.host)
        self._built[name] = (settings, output)
        return output

    def _sync(self) -> dict[str, SiemOutput]:
        """The outputs as the settings say now, by name. An object whose output changed is closed, and every one each
        ``REBUILD_EVERY``. An output this instance sees removed sends nothing more (R2-1: it may have been removed for
        where it points): its object is closed and dropped, with the secret it held, and its queue goes with it, what it
        held of the administration log counted apart (`admin_dropped`, said by the status and the documentation's
        gaps). One it never saw is not judged here: the retention clears it."""
        wanted = {output.name: output for output in self.outputs()}
        stale = time.monotonic() - self._built_at >= REBUILD_EVERY
        for name, (settings, output) in list(self._built.items()):
            if stale or wanted.get(name) != settings:
                del self._built[name]
                self._closing(name, output)
        if stale:
            self._built_at = time.monotonic()
        removed = (self._known or set()) - set(wanted)
        if removed:
            taken = self.queue.forget(removed)
            for name in removed:
                self._trimmed.pop(name, None)
                self._count(self.metrics.queued, name, 0)
            log.info("SIEM outputs removed: %d events waiting for them dropped", taken)
        self._known = set(wanted)
        return wanted

    def _closing(self, name: str, output: Output) -> None:
        try:
            output.close()
        except Exception as exc:  # by its type (R14)
            self._say(f"close:{name}", "SIEM output %s could not be closed (%s)", name, type(exc).__name__)

    def _count(self, metric: Callable[..., None], *args: Any) -> None:
        """A metric counted, its failure said by its type and never raised: a batch delivered is never written as a
        failure, nor sent again, for a metric."""
        try:
            metric(*args)
        except Exception as exc:  # by its type (R14)
            self._say("metrics", "a SIEM metric could not be counted (%s)", type(exc).__name__)

    def _say(self, what: str, message: str, *args: object) -> None:
        """A warning said once a minute at most for each ``what`` (an exception by its type alone: R14): a store that
        does not answer is said, never a line a round."""
        now = time.monotonic()
        if now - self._said.get(what, float("-inf")) >= SAID_EVERY:
            self._said[what] = now
            log.warning(message, *args)
