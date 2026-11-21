"""The term sync on a Latet server: each source read on its schedule, or when asked, by one instance at a time (a
Valkey lease), its terms kept sealed in PostgreSQL, and the terms in use in one live detector on every instance
(LiveTerms, the Community engine's), which every space masks with as a company's locked terms. The instance that changed
the terms says so on `ee:terms` (never a term); E1's listener wakes the others, which read PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

from latet.ee.events.locks import Lease, LeaseFn
from latet.ee.settings import TermSource, TermSyncSettings
from latet.ee.sync.records import MAX_BYTES, SourceError, bounded, terms_of
from latet.ee.sync.sources import TIMEOUT, Reader
from latet.ee.sync.store import Asked, Run, SealedElsewhere, StoredTerm, TermStore, TooMany, uncounted
from latet.engine.detectors.terms import LiveTerms
from latet.events import make_event
from latet.floor import TERMS_UNAVAILABLE, TermsUnavailable
from latet.logline import Paced
from latet.server.egress import RESOLVE_SECONDS

if TYPE_CHECKING:
    from latet.ee.edition import EnterpriseEdition
    from latet.server.app import Server

log = logging.getLogger(__name__)

FEATURE = "term-sync"
CHANNEL = "ee:terms"  # {"version": n} once the terms changed, {"asked": n} once a sync was asked for: never a term
LEASE = "terms-sync"  # one instance syncs at a time
LEASE_SECONDS = 900.0  # an instance that dies holding the lease frees it after this
APPLY_MARGIN = 60.0  # a run applies only with this much of its lease left: the terms read again (B-R5), then applied
APPLY_SECONDS = 30.0  # the apply alone: the lease is asked again right before it, with this much of it left
TICK = 30.0  # seconds between two looks at what is due, unless woken
CLOSE_WAIT = 5.0  # seconds close() waits for each thread: one still in a read is left, its lease given back
RETRY_MINUTES = 15  # a failed source is tried again after this, or after its own interval when that is shorter
# Ticks an instance that cannot read a source's kind (no `ldap` extra) leaves a due source's turn to the instances that
# can, before it runs it itself and the source fails `not-installed` (the final review's M5).
LEAVE_TICKS = 3
MOST_KEPT = 50_000  # terms kept in all, gone ones included: the count the matcher's budget was measured at (R14)
# How long a read may still run past its deadline, which a reader checks between two reads (B-I1) and its watchdog
# enforces by cutting the connection (records.cut_at_deadline): one request (its name looked up by egress, then its
# answer within the request's timeout), and the parsing of what it read, which nothing checks: the largest document, of
# YAML, the slowest format (about 0.4 MB a second), in about 2 minutes. A run's deadline is its lease's turn less this
# and APPLY_MARGIN, so that a read that ends at its deadline is still applied within the lease, before another instance
# may take it.
PARSE_SECONDS = MAX_BYTES / 400_000
READ_MARGIN = RESOLVE_SECONDS + TIMEOUT + PARSE_SECONDS
_UNLOADED, _UNTICKED = Paced(), Paced()

Publish = Callable[[str, Mapping[str, Any]], object]  # E1's latet.ee.server.publish, bound to the server's bus
Record = Callable[[Mapping[str, Any]], None]  # the edition's record: E4's event log


class Standing(Protocol):
    """What term sync asks of the license: E1's Licensing."""

    def licensed(self, feature: str) -> bool: ...
    def producing(self, feature: str) -> bool: ...
    def knows(self) -> bool: ...


class SyncPaused(Exception):
    """The license does not let term sync read its sources now (a pause, a trial's end, a file that cannot be used):
    the terms synced so far stay masked."""


def trailing(behind: object) -> TypeGuard[Mapping[str, Any]]:
    """Whether ``behind`` (``TermSync.behind``, or a status's answer of it) says the terms in use trail the store for
    longer than two ticks: a reload under way (a new version seen, the terms not swapped yet) is no failure to read
    them. What `latet server status` and `latet server terms status` say alike (the final review's M1)."""
    seconds = behind.get("seconds") if isinstance(behind, Mapping) else None
    return isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds > 2 * TICK


def terms_tag(terms: Iterable[tuple[str, str]]) -> str:
    """The tag of the terms masked (value, type), as ``LiveTerms`` holds them (each type upper-cased): a digest of
    those terms alone, sorted, so that every instance names the same terms alike, whatever version of the store it read
    them at, and a run that only marks terms gone or back, a source taken out whose terms stay masked, or a row sealed
    again, leaves it as it was (the final review's I1); "" for none, so that a server that masks no synced term keeps
    0.11's keys and stamps (A-M4). Nothing else changes how they are masked: the license is not in it (without term
    sync no term is masked). Held in memory and in keyed digests (``Masker.tag``), never said."""
    held = sorted({(value, etype.upper()) for value, etype in terms})
    if not held:
        return ""
    shown = json.dumps(held, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(shown.encode("utf-8")).hexdigest()[:32]


def reads(kind: str) -> bool:
    """Whether this instance's install reads a source of ``kind``: every kind, an LDAP directory with Latet's `ldap`
    extra alone (ldap3)."""
    if kind != "ldap":
        return True
    from latet.ee.sync.ldap import installed

    return installed()


def _outlived() -> SourceError:
    """A run whose lease may be another instance's now: what it read is not applied."""
    turn = f"{LEASE_SECONDS / 60:g} minutes"
    return SourceError("timeout", f"the source took longer than the sync's turn ({turn}): narrow it")


class TermSync:
    """Term sync on one instance of a server: the unit tests give it stand-ins, ``make_term_sync`` the server's own."""

    def __init__(
        self,
        store: TermStore,
        *,
        settings: Callable[[], TermSyncSettings],
        secret: Callable[[TermSource], str | None],
        licensing: Standing,
        lease: LeaseFn,
        publish: Publish,
        record: Record,
        readers: Mapping[str, Reader],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.live = LiveTerms()
        self.licensing = licensing
        self.readers: dict[str, Reader] = dict(readers)
        self.clock = clock
        self.counted: Callable[[Run], None] = uncounted  # the metrics' count of runs (Task 10)
        self.unreadable = 0  # terms the last read of them could not open
        self._settings, self._secret, self._lease = settings, secret, lease
        self._publish, self._record = publish, record
        self._reloading = threading.Lock()  # one reload at a time, and one read of the terms (B-R5)
        self._state = threading.Lock()  # what `behind` reads, changed and read together
        self._loaded: tuple[int, bool] | None = None  # the store's version and the license the terms in use came with
        self._seen: int | None = None  # the store's version the last reload read
        self._read: int | None = None  # the store's version its last read of the terms was of (B-R5)
        self._behind_since: float | None = None  # since when the terms in use trail the store (B-I1)
        self._wake, self._reload_wake, self._stop = threading.Event(), threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._reloader: threading.Thread | None = None
        self._leasing = threading.Lock()  # a turn's lease taken, or given back by close(), one at a time
        self._holding: Lease | None = None  # the lease of the turn under way, which close() gives back
        self.installed: Callable[[str], bool] = reads  # whether this instance reads a kind of source (M5)
        # The due sources of a kind this instance cannot read, each left to the others since when, at which last run
        # and ask (one that changed since: left again from now).
        self._left: dict[str, tuple[float, tuple[float | None, float | None]]] = {}

    # --- the terms in use -----------------------------------------------------------------------------------------

    def reload(self) -> bool:
        """Use what the store holds now (another instance may have changed it), none while the license does not list
        term sync. Raises while the license was never read on this instance (``TermsUnavailable``), while the store does
        not answer, or while it holds terms this instance's master key cannot open (``TermsUnavailable``: never fewer
        terms, under any tag, the design security review's B-I2): the terms in use stay as they were. The store's
        version is read again after the terms, and the swap waits while it moves (B-M3). The terms are swapped only
        when their tag (``terms_tag``) changes: a new version of the store that masks the same terms touches nothing.
        Whether they changed."""
        with self._reloading:
            licensed = self.licensing.licensed(FEATURE)
            if not licensed and not self.licensing.knows():  # never read: "none" would build spaces without them
                raise TermsUnavailable(TERMS_UNAVAILABLE)
            for _ in range(3):
                version = self.store.version()
                with self._state:
                    self._seen = version
                    if (version, licensed) == self._loaded:
                        self._behind_since = None
                        return False
                    if self._behind_since is None:
                        self._behind_since = self.clock()  # until the swap below
                kept = self.store.terms() if licensed else []
                if licensed:
                    self._read = version
                if licensed and self.store.unreadable:  # another master key's terms: keep those in use (B-I2)
                    self.unreadable = self.store.unreadable
                    raise TermsUnavailable(TERMS_UNAVAILABLE)
                if self.store.version() == version:  # the terms read are the version's (B-M3)
                    break
            else:
                return False  # still moving: the next reload takes it, and `behind` counts meanwhile
            one_each: dict[tuple[str, str], tuple[str, str]] = {}
            for t in sorted(kept, key=lambda t: t.id):  # one spelling of each, the same on every instance (ids are)
                one_each.setdefault((t.value.casefold(), t.type), (t.value, t.type))
            tag = terms_tag(one_each.values())
            swapped = tag != self.live.tag
            if swapped:  # the same tag: the same terms, masked as they are (the final review's I1)
                self.live.use(one_each.values(), tag)
            self.unreadable = 0
            with self._state:
                self._loaded, self._behind_since = (version, licensed), None
            return swapped

    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    def ready(self) -> LiveTerms:
        """The live terms, for a space being built: loaded first when they never were, so that no space masks without
        them; ``TermsUnavailable`` while they cannot be (the space is refused, and tried again)."""
        if self._loaded is None:
            try:
                self.reload()
            except TermsUnavailable:
                raise
            except Exception as exc:  # its words can name the database's address
                if count := _UNLOADED.due():
                    log.warning(
                        "the synced terms could not be loaded (%s)%s",
                        type(exc).__name__,
                        f" ({count} times since the line before)" if count > 1 else "",
                    )
                raise TermsUnavailable(TERMS_UNAVAILABLE) from None
            if self._loaded is None:  # the store moved under every read (a burst of runs): no terms to hand out yet
                raise TermsUnavailable(TERMS_UNAVAILABLE)
        return self.live

    def wake(self) -> None:
        """Another instance changed the terms or asked for a sync (`ee:terms`, through E1's listener): this instance's
        threads look now, the reload's apart from any read (B-I1). Nothing is read here, on the listener's thread."""
        self._reload_wake.set()
        self._wake.set()

    def changed(self) -> None:
        """After a change of the terms committed (a removal, a source taken out): the others are told, and this
        instance's own thread is woken and reloads (nothing is read in a route's after-commit callbacks, which
        ``People.committed`` calls unguarded: fix round 1's M2)."""
        try:
            self._publish(CHANNEL, {"version": self.store.version()})
        except Exception as exc:  # each tick reads the version too
            log.warning("the synced terms' change could not be told (%s)", type(exc).__name__)
        self.wake()

    def asked(self) -> None:
        """After a sync asked for committed: every instance looks, and whichever holds the lease runs it. (Every
        publish here names ``CHANNEL`` and a literal message: the Valkey test reads each one's fields, S7. E1's publish
        logs a failure by its type: each tick reads the version too.)"""
        self._publish(CHANNEL, {"asked": 1})
        self.wake()

    # --- syncing -------------------------------------------------------------------------------------------------

    def _due(self, source: TermSource, last: Run | None, asked: Asked | None) -> bool:
        if asked is not None and (last is None or asked.at > last.started):
            return True
        if last is None:
            return True
        minutes = source.every_minutes if last.outcome == "ok" else min(source.every_minutes, RETRY_MINUTES)
        return self.clock() >= last.finished + minutes * 60

    def _next(self, source: TermSource, last: Run | None) -> float:
        if last is None:
            return self.clock()
        minutes = source.every_minutes if last.outcome == "ok" else min(source.every_minutes, RETRY_MINUTES)
        return last.finished + minutes * 60

    def tick(self) -> list[Run]:
        """What the thread does every TICK seconds, or when woken: first the terms the store holds are used, whoever
        changed them (a message missed, or another instance's change that woke it); then, while the license lets term
        sync produce and this instance takes the lease, each source that is due or asked for syncs, every instance told
        and this one masking a run's terms as soon as it is applied, before the next source is read (fix round 1's
        I5), and before the run's bookkeeping. Each source that is due is read only once the lease is known to be still
        this instance's, with a whole read's time left, and the license to let term sync produce still: after a
        failover or a pause the tick stops there. A close stops it between two sources too."""
        runs: list[Run] = []
        self._reload_quietly()
        if self.licensing.producing(FEATURE):
            with self._turn() as lease:
                if lease is not None:
                    taken = self.clock()
                    last, asks = self.store.runs(), self.store.asks()
                    for source in self._settings().sources:
                        if self._stop.is_set() or self.clock() - taken > LEASE_SECONDS / 2:
                            break  # closing, or the others wait for the next turn: the lease must outlive the work
                        asked = asks.get(source.name)
                        if not self._due(source, last.get(source.name), asked):
                            self._left.pop(source.name, None)
                        elif self._takes(source, last.get(source.name), asked):
                            if not self._may_go_on(lease):
                                break  # the lease lost, or term sync paused by the license, since the tick began
                            before = self.store.version()
                            until = taken + LEASE_SECONDS - APPLY_MARGIN - READ_MARGIN  # its deadline (B-I1)
                            run = self._run(source, lease, until)
                            self._told(before)  # first: no failure of the bookkeeping keeps the terms from any instance
                            if run is not None:
                                self._book(run, asked.by if asked is not None else None, lease)
                                runs.append(run)
        self._reload_quietly()
        return runs

    def _takes(self, source: TermSource, last: Run | None, asked: Asked | None) -> bool:
        """Whether this instance takes a due source's turn: always when it reads the source's kind; else (an LDAP source
        on an instance without Latet's `ldap` extra) once it has left the turn for LEAVE_TICKS ticks to the instances
        that read it and none took it (no run since, nor a new ask): the source then fails `not-installed`, said where
        it is shown. A fleet where some instances have the extra never flaps between a run and `not-installed` (the
        final review's M5); one where none has it says so within LEAVE_TICKS ticks."""
        if self.installed(source.kind):
            self._left.pop(source.name, None)
            return True
        seen = (last.started if last is not None else None, asked.at if asked is not None else None)
        since, then = self._left.get(source.name, (self.clock(), seen))
        if then != seen:  # another instance ran it, or a sync was asked for again: left again from now
            since = self.clock()
        if self.clock() - since < LEAVE_TICKS * TICK:
            self._left[source.name] = (since, seen)
            return False
        self._left.pop(source.name, None)
        return True

    @contextmanager
    def _turn(self) -> Iterator[Lease | None]:
        """The lease for a tick's turn, taken only while the term sync is not closing (None: not taken, or another
        instance's), and kept where close() finds it: a close that cannot wait for a read gives it back at once, so that
        an instance that stops mid-read never keeps the others from syncing until the lease runs out (LEASE_SECONDS)."""
        with ExitStack() as stack:
            with self._leasing:
                taken = None if self._stop.is_set() else stack.enter_context(self._lease(LEASE, LEASE_SECONDS))
                held = self._holding = taken if taken else None
            try:
                yield held
            finally:
                with self._leasing:
                    self._holding = None

    def _may_go_on(self, lease: Lease) -> bool:
        """Whether the next source may be read: the lease still this instance's with a whole read's time left, and term
        sync still produced under the license. No when either cannot be asked (Valkey or the database not answering)."""
        try:
            return lease.held(APPLY_MARGIN + READ_MARGIN) and self.licensing.producing(FEATURE)
        except Exception as exc:
            log.warning("term sync: whether its turn goes on could not be read (%s)", type(exc).__name__)
            return False

    def _told(self, before: int) -> None:
        """After a run: when the store moved, every instance told (its version alone), and this one masking with what
        the store holds now."""
        try:
            version = self.store.version()
            if version != before:
                self._publish(CHANNEL, {"version": version})
        except Exception as exc:  # every instance's reload thread reads the version too, every TICK seconds
            log.warning("the synced terms' change could not be told (%s)", type(exc).__name__)
        self._reload_quietly()

    def _book(self, run: Run, by: str | None, lease: Lease) -> None:
        """A run's bookkeeping, once its terms are told and in use: recorded as its source's last only while the lease
        is still this instance's (a late run never overwrites a newer holder's, and a sync asked for stays asked), then
        its event and its count. A failure is logged by its type alone."""
        try:
            if lease.held():
                self.store.record_run(run)
        except Exception as exc:
            log.warning("term sync: the run of %s could not be recorded (%s)", run.source, type(exc).__name__)
        try:
            by_type = self.store.counts().get(run.source, {}).get("by_type", {})
            self._event(run.outcome, run.code, run.source, by_type, by)
            self.counted(run)
        except Exception as exc:
            log.warning("term sync: the run of %s could not be counted (%s)", run.source, type(exc).__name__)

    def _reload_quietly(self) -> None:
        try:
            self.reload()
        except Exception as exc:  # the terms in use stay as they were
            if count := _UNTICKED.due():
                log.warning(
                    "the synced terms could not be read again (%s)%s",
                    type(exc).__name__,
                    f" ({count} times since the line before)" if count > 1 else "",
                )

    def _run(self, source: TermSource, lease: Lease, until: float) -> Run | None:
        """One run of ``source``: what it gives read within the run's bounds, and applied while the lease is this
        instance's; a run that fails changes nothing. None when the source was taken out or changed while it was read:
        what the read gave is dropped and no run is recorded (still due, it is read again as it now stands)."""
        started = self.clock()
        try:
            reader = self.readers.get(source.kind)
            if reader is None:
                raise SourceError("not-installed", f"this server reads no {source.kind} source")
            with bounded(until, self.clock):  # checked inside the reader: a read past it fails `timeout` (B-I1, B-M5)
                mapped = terms_of(source, reader(source, lambda: self._secret(source)))
            if not lease.held(APPLY_MARGIN):  # another instance may hold it now: what was read is not applied
                raise _outlived()
            # A source taken out while it was read (its terms retired by the route) or changed is not applied: the apply
            # would mark its terms back in it, where no one could remove them. What this reads is this instance's copy
            # of the settings: a change committed on another instance is seen once that copy has it (E1's ee:settings
            # message, or its periodic read). A run applied in that lag still marks the terms back, until the next
            # save of the sources retires every source no longer set up; closing it whole takes the check inside the
            # apply's transaction, under the settings' lock.
            if not any(s == source for s in self._settings().sources):
                log.info("term sync: the source %s changed while it was read: what it gave is not applied", source.name)
                return None
            if not mapped.terms and self.store.counts().get(source.name, {}).get("terms", 0):
                raise SourceError("empty", "the source gave no term: nothing changed (an export cut short, a filter?)")
            self._read_again()
            if not lease.held(APPLY_SECONDS):  # asked again right before the apply, after the read of the terms
                raise _outlived()
            added, gone = self.store.apply(source.name, mapped.terms, self.clock(), most=MOST_KEPT)
            if self._retired_again(source):
                return None
            run = Run(
                source.name, source.kind, started, self.clock(), "ok",
                found=len(mapped.terms), added=added, gone=gone, skipped=mapped.skipped,
            )  # fmt: skip
        except SourceError as exc:
            run = Run(source.name, source.kind, started, self.clock(), "failed", exc.code, exc.message)
        except TooMany:
            reason = (
                f"the server keeps {MOST_KEPT:,} synced terms at most, gone ones included: remove some, or narrow a "
                "source"
            )
            run = Run(source.name, source.kind, started, self.clock(), "failed", "too-many", reason)
        except SealedElsewhere:
            reason = "the synced terms were sealed with another master key: term sync waits for the right one"
            run = Run(source.name, source.kind, started, self.clock(), "failed", "sealed-elsewhere", reason)
        except Exception as exc:  # a fault must not stop the other sources; its message could quote a record
            log.error("term sync: the source %s failed (%s)", source.name, type(exc).__name__)
            reason = f"an error in Latet ({type(exc).__name__})"
            run = Run(source.name, source.kind, started, self.clock(), "failed", "error", reason)
        return run

    def _retired_again(self, source: TermSource) -> bool:
        """After an apply: whether the source is no longer set up (a route took it out between the check before the
        apply and the apply, which marked its terms back in it). Its terms alone are then marked gone again, every other
        source's left as they are (one this instance's copy of the settings does not know yet included)."""
        configured = [s.name for s in self._settings().sources]
        if source.name in configured:
            return False
        self.store.retire(sorted({*configured, *self.store.counts()} - {source.name}), self.clock())
        return True

    def _read_again(self) -> None:
        """Before a run applies: the terms read again when the last read of them is older than the store (another
        change came since, or the reload failed), so that `apply` seals again what no key of this server opened in the
        rows as they are now (B-R5, as root ruled it). One store serves the reload and the runs; the read is the
        reload's own, in turn with it, and says how many terms no key opens, as a reload does."""
        with self._reloading:
            version = self.store.version()
            if version != self._read:
                self.store.terms()
                self._read, self.unreadable = version, self.store.unreadable

    def _event(
        self, outcome: str, code: str | None, source: str | None, counts: Mapping[str, int], by: str | None
    ) -> None:
        rule = f"term-sync:{source}" if source else "term-sync"
        person = by if by is not None and by.startswith("u_") else None  # the administration: its log has the token
        self._record(
            make_event(
                "sync", clock=self.clock, outcome=outcome, reason=code, rule=rule, masked=dict(counts), by=person
            )
        )

    # --- what the routes ask, in their change transaction (``conn``) -----------------------------------------------

    def ask(
        self,
        name: str | None,
        *,
        by: str | None,
        conn: Any = None,
        sources: Sequence[TermSource] | None = None,
    ) -> list[str]:
        """Record a sync asked for: every source, or the one named. SyncPaused while the license does not let term sync
        produce, KeyError for a name no source has. ``sources``: the sources as the caller's transaction reads them (a
        route's: this instance's copy of the settings may not have a change another instance has just made); this
        instance's copy when not given. ``asked()`` once it committed."""
        if not self.licensing.producing(FEATURE):
            raise SyncPaused("term sync is paused by the license: the terms synced so far stay masked")
        given = self._settings().sources if sources is None else sources
        names = [s.name for s in given if name is None or s.name == name]
        if name is not None and not names:
            raise KeyError(name)
        self.store.ask(names, by, self.clock(), conn=conn)
        return names

    def retire(self, names: Sequence[str], *, conn: Any = None) -> int:
        """The terms of the sources not in ``names`` become gone (still masked). ``changed()`` once it committed."""
        return self.store.retire(names, self.clock(), conn=conn)

    def forget(self, names: Sequence[str], *, conn: Any = None) -> None:
        """The last runs of ``names``, and a sync asked for of them, forgotten (a source taken out, changed or set up
        again under its name: the final review's M4): each that is set up runs at the next look, as the source it is
        now, never at its old run's schedule (up to a week), and none shows a run of another source than it is."""
        self.store.forget_runs(names, conn=conn)

    def remove(
        self, ids: Sequence[str] | None = None, *, source: str | None = None, conn: Any = None
    ) -> dict[str, int]:
        """Forget terms no longer in their source: these, or every one of ``source``, or every one. A term still in its
        source is left alone. ``removed(…)`` once it committed."""
        return self.store.remove(ids, source=source, conn=conn)

    def remove_unreadable(self, *, conn: Any = None) -> dict[str, int]:
        """Forget the terms no key of this server opens among those no longer in their source (B-R5: the
        administration's alone, asked first; never one a source still gives, which its next run seals again; `NewerKey`
        while one names a newer master key version than this server's). ``removed(…)`` once it committed."""
        return self.store.remove_unreadable(conn=conn)

    def removed(self, counts: Mapping[str, int], *, source: str | None, by: str | None) -> None:
        """After a removal committed: its event (counts by type), and every instance stops masking those terms."""
        if counts:
            self._event("removed", None, source, counts, by)
            self.changed()

    # --- what is shown ---------------------------------------------------------------------------------------------

    def sources(self) -> list[TermSource]:
        """The sources set up now, as the Enterprise settings hold them (the metrics count them by kind)."""
        return list(self._settings().sources)

    def status(self) -> dict[str, Any]:
        """Each source (those set up, then those the store still keeps terms of): its terms by type, those no longer in
        it, a sync asked for, its last run and when it is due. Never a term."""
        configured = {s.name: s for s in self._settings().sources}
        counts, runs, asks = self.store.counts(), self.store.runs(), self.store.asks()
        sources = []
        for name in [*configured, *sorted(set(counts) - set(configured))]:
            source, last, asked = configured.get(name), runs.get(name), asks.get(name)
            sources.append({
                "name": name,
                "kind": source.kind if source else None,
                "every_minutes": source.every_minutes if source else None,
                "configured": source is not None,
                **counts.get(name, {"terms": 0, "by_type": {}, "gone": 0}),
                "asked": asked.at if asked else None,
                "last": asdict(last) if last else None,
                "next": self._next(source, last) if source else None,
            })  # fmt: skip
        return {
            "licensed": self.licensing.licensed(FEATURE),
            "paused": not self.licensing.producing(FEATURE),
            "masked": self.live.count,
            "kept": self.store.kept(),
            "most": MOST_KEPT,
            "gone": sum(c["gone"] for c in counts.values()),
            "unreadable": self.unreadable,
            "behind": self.behind(),
            "sources": sources,
        }

    def behind(self) -> dict[str, Any] | None:
        """Whether the terms in use trail the store (a reload that keeps failing, or that keeps the terms for B-I2):
        the version masked, the one the store holds, and for how long; None when they do not (B-I1). `latet server
        status` and `latet server terms status` say it past two ticks (``trailing``), and the gauge
        `latet_terms_behind_seconds` reads it (B-R6)."""
        with self._state:  # changed together by the reload: never one reload's version with another's time
            since, loaded, seen = self._behind_since, self._loaded, self._seen
        if since is None or loaded is None:
            return None
        return {"loaded": loaded[0], "store": seen, "seconds": self.clock() - since}

    def gone(self, limit: int = 1000, source: str | None = None) -> tuple[list[StoredTerm], int]:
        return self.store.gone(limit, source)

    # --- the thread ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """The two threads: one reloads the terms when woken or every TICK seconds, apart from any read (B-I1); the
        other ticks every TICK seconds, or when woken, and syncs what is due (its first tick loads the terms too)."""
        self.start_reloading()
        self._thread = threading.Thread(target=self._schedule, name="latet-term-sync", daemon=True)
        self._thread.start()

    def start_reloading(self) -> None:
        self._reloader = threading.Thread(target=self._reloads, name="latet-term-reload", daemon=True)
        self._reloader.start()

    def _reloads(self) -> None:
        while not self._stop.is_set():
            self._reload_wake.clear()  # first: a wake that comes during the reload is heard by the next wait
            self._reload_quietly()
            self._reload_wake.wait(TICK)

    def _schedule(self) -> None:
        while not self._stop.is_set():  # tick() also stops between two sources once closing
            self._wake.clear()  # first: a wake that comes during the tick is heard by the next wait
            try:
                self.tick()
            except Exception as exc:  # one bad tick must not end the term sync
                log.error("term sync: a tick failed (%s)", type(exc).__name__)
            self._wake.wait(TICK)

    def close(self) -> None:
        """Both threads stopped: each waited for CLOSE_WAIT seconds at most. One still busy is left to end on its own (a
        daemon thread, a read cut at the run's deadline by the reader's watchdog, then stopping before the next source),
        and the lease of its turn is given back now: no other instance waits for it to run out (LEASE_SECONDS) once this
        one stops. A thread still reading then never applies (the run asks the lease before its apply); one already
        inside the apply commits it, but its run is not recorded (it is recorded only under the lease), so the next
        holder runs that source again. No turn is taken after. The store is closed after the wait all the same."""
        self._stop.set()
        self._wake.set()
        self._reload_wake.set()
        for thread in (self._thread, self._reloader):
            if thread is not None:
                thread.join(timeout=CLOSE_WAIT)
        with self._leasing:
            holding, self._holding = self._holding, None
        if holding is not None:
            try:
                holding.release()
            except Exception as exc:  # Valkey's words can name its address
                log.warning(
                    "term sync: its lease could not be given back (%s): it runs out within %g minutes",
                    type(exc).__name__,
                    LEASE_SECONDS / 60,
                )
        self.store.close()


def make_term_sync(edition: EnterpriseEdition, server: Server) -> TermSync:
    """An instance's term sync: the server's PostgreSQL (each term sealed with the master key's `terms` sealer), the
    sources as the Enterprise settings hold them now, each source's secret opened for where it goes (None when it was
    kept for another place: the source then fails `no-secret`), Valkey's lease, E1's publish, E4's event log, and each
    kind's reader, a file source's inside the server's term-sync folder. Nothing here reads a store or starts a
    thread."""
    from cryptography.exceptions import InvalidTag

    from latet.ee.server import publish
    from latet.ee.settings import TERM_FILES, needs_secret, source_secret, terms_bound
    from latet.ee.sync.pgstore import SEALER_PURPOSE, PgTermStore  # psycopg: the server extra
    from latet.ee.sync.sources import readers
    from latet.server.start_errors import MasterKeyError

    def secret(source: TermSource) -> str | None:
        if not needs_secret(source):
            return None
        try:
            return edition.secrets.load(source_secret(source), bound=terms_bound(source))
        except (InvalidTag, MasterKeyError):  # kept for another place, or with a master key this server lacks
            return None

    return TermSync(
        PgTermStore(server.db, server.master.sealer(SEALER_PURPOSE)),
        settings=lambda: edition.settings().term_sync,
        secret=secret,
        licensing=edition.licensing,
        lease=server.bus.lease,
        publish=lambda channel, message: publish(server.bus, channel, message),
        record=edition.record,
        readers=readers(server.data / TERM_FILES),
        clock=server.clock,
    )
