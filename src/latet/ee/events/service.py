"""A server's events (E4) on one instance: the store in the server's PostgreSQL, the pipeline that writes what
happens on this instance, and the thread of jobs (the SIEM's delivery, the administration log to the SIEM, the
license's and the policy's changes, the retention), each that must run once taking its Valkey lock. Made by the
Enterprise Edition as it starts on a server (R17) with nothing running, started last once the edition is whole (I7),
closed first when it closes."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from latet.ee.events.admin import JOB as FORWARD_JOB
from latet.ee.events.admin import AdminForwarder, AuditReader
from latet.ee.events.delivery import SiemDelivery
from latet.ee.events.gaps import accounted, check
from latet.ee.events.jobs import Jobs, LicenseWatch, Retention
from latet.ee.events.metrics import ServerMetrics
from latet.ee.events.pg import PgEvents
from latet.ee.events.pipeline import EventPipeline, Pseudonyms, SettingsCache
from latet.ee.events.siem import secret_of
from latet.ee.policy import FEATURE as LOCKED_RULES
from latet.ee.roles import Role

if TYPE_CHECKING:
    from latet.ee.edition import EnterpriseEdition
    from latet.server.app import Server
    from latet.server.master import MasterKey

PSEUDONYMS = "reports-pseudonyms"
# Seconds between two rounds of each job when none did any work (one that did runs again at once). The delivery reads
# every output's schedule and asks Valkey for each due output's lease at each round (Task 7): a tick of a few seconds,
# the pipeline waking it at once when it queued events for the SIEM.
DELIVERY_EVERY = 5.0
FORWARD_EVERY = 5.0
WATCH_EVERY = 60.0
RETENTION_EVERY = 3600.0  # the retention runs once a day: each look asks the store whether its day is due
_POWER = frozenset({Role.ADMIN, Role.SECURITY})  # Decision 1 (d): who holds one of them administers


@dataclass
class EventService:
    store: PgEvents
    settings: SettingsCache
    pipeline: EventPipeline
    delivery: SiemDelivery
    jobs: Jobs
    watch: LicenseWatch  # the license's and the policy's job, which the jobs' thread runs every minute
    audit: AuditReader | None = None  # the administration log, where `gaps` reads the entries that name markers
    metrics: ServerMetrics | None = None  # E0c's registry's, bound to this service while it runs
    forward: Callable[[], bool] | None = None  # the administration log's job, as the jobs' thread runs it

    def start(self, *, jobs: bool = True) -> None:
        """The pipeline's thread and the jobs': the edition starts them last, once it is whole (I7). Without ``jobs``
        (a one-shot command's server, `latet server init`), the pipeline alone: an event put meanwhile is still written
        (a pipeline never started counts what waits as lost when it closes), and no job runs a round, nor takes a lock
        that would outlive the command."""
        self.pipeline.start()
        if jobs:
            self.jobs.start()

    def refreshed(self) -> None:
        """The Enterprise settings changed (here, or heard from another instance): read them again at the next look,
        and make the outputs again (a secret set meanwhile among them)."""
        self.settings.clear()
        self.delivery.refresh()

    def gaps(self) -> dict[str, Any]:
        """R3-1, R4-1, R4-2, for `latet server log verify`: Task 8's ``check``, the markers waiting in this server's
        queues read first, then the log's entries that name markers (those whose hash holds), then the first id the log
        keeps. Raises when the log or a queue cannot be read (a row that is not JSON among them): the route says so."""
        if self.audit is None:
            return {"recorded": [], "first_kept": 1} | accounted(self.store.markers(), set(), 1)
        return check(self.audit, self.store)

    def close(self) -> None:
        """What waits is written first (and may still reach the SIEM), then the jobs stop, then the outputs close with
        the secrets they held; the metrics read this service no more. Each step is taken whatever the one before
        raised (no thread is left running for it), and the first error is raised once all were taken."""
        steps = [self.pipeline.close, self.jobs.close, self.delivery.close]
        if self.metrics is not None:
            metrics, counts = self.metrics, self.store.counts
            steps.append(lambda: metrics.unbind(counts))
        failed: BaseException | None = None
        for step in steps:
            try:
                step()
            except BaseException as exc:  # the next steps are taken all the same
                failed = failed or exc
        if failed is not None:
            raise failed


def pseudonym_key(store: PgEvents, master: MasterKey) -> bytes:
    """The server's key for pseudonyms: made by the first instance that needs it, kept sealed with the master key (its
    version in the frame: a rotation of the master key opens it still), the same on every instance."""
    sealed = store.kept(PSEUDONYMS, master.seal(secrets.token_bytes(32), PSEUDONYMS))
    return master.open(sealed, PSEUDONYMS)


def make_events(edition: EnterpriseEdition, server: Server) -> EventService:
    """This instance's events: what the edition records goes to its pipeline, which weighs the license now. Nothing
    runs before ``EventService.start``, which the edition calls last (I7): what is recorded meanwhile waits.

    What the jobs ask of the server (Task 6's and Task 8's carries): an identity's kind and first group are None for
    none and for an id the server does not know, and raise only when the server fails (a batch then waits, never kept
    without them); the outputs are the settings' own, never filtered by the license (a pause is ``producing``'s), and
    raise when the settings cannot be read, never ``[]`` (which would forget every queue)."""
    from latet.ee.policy_server import ServerPolicy
    from latet.ee.server import settings_in

    store = PgEvents(server.db)
    settings = SettingsCache(edition.settings)
    people = edition.people
    lock = server.bus.lock

    def kind_of(identity: str) -> str | None:
        found = server.identities.get(identity)
        return found.kind if found is not None else None

    def group_of(identity: str) -> str | None:
        groups = people.groups_of(identity) if people is not None else []
        return groups[0].id if groups else None

    def outputs() -> list[str]:
        return [output.name for output in settings().siem.outputs]

    def output_kind(name: str) -> str | None:
        return next((output.kind for output in settings().siem.outputs if output.name == name), None)

    def policy_in_force() -> str | None:
        """R9: the etag of the policy in force (E2's, stored while the license lists locked rules), or None. Raises
        while the edition's policy is not made yet: the license's job then notes nothing and records nothing (I7)."""
        policy = getattr(edition, "policy", None)
        if policy is None:
            raise LookupError("the edition's policy is not made yet")
        if not isinstance(policy, ServerPolicy) or not edition.licensing.licensed(LOCKED_RULES):
            return None
        return policy.stored_etag()

    def administers(identity: str) -> bool:
        """Decision 1 (d): who holds the admin or the security role now is named in the SIEM's admin events."""
        return people is not None and bool(_POWER & people.roles_of(identity))

    def empowers(group_id: str) -> bool:
        """R3-2: whether a group holds the admin or the security role now (E3's ``People.given``): who joins or leaves
        it is named in the SIEM's admin events, as a direct grant names them."""
        return people is not None and bool(_POWER & people.given(group_id))

    def gap_noted(conn: Any, action: str, details: Mapping[str, Any]) -> None:
        """R3-1 (Task 4's `Noting`): the entry of the administration log that names a gap marker's range, by the
        server, in the marker's own transaction (``conn``): the bound's collapse (`siem.gap {output, ...}`), a pause's
        or no output's marker (`siem.gap {output: null, ...}`), and where the forwarding begins (`siem.forwarding`)."""
        output = details.get("output")
        server.audit.record("server", action, str(output) if output else None, dict(details), conn=conn)

    def retention_days() -> int:
        """M11: how long to keep, read in its own transaction where it is asked, never a copy's defaults: raising
        when it cannot be read, so that nothing is pruned."""
        with server.db.connection() as conn:
            return settings_in(conn).reports.retention_days

    metrics = ServerMetrics.of(server.metrics)  # once a registry: an edition made again on this server rebinds it (I10)
    origin = server.settings.url
    # E0b's log as the jobs read it: ``holds`` is asked of the entries it gave (its AuditEntry), never of another's.
    audit = cast("AuditReader", server.audit)
    pipeline = EventPipeline(
        store, settings=settings, licensing=edition.licensing,
        pseudonyms=Pseudonyms(lambda: pseudonym_key(store, server.master)), group_of=group_of, kind_of=kind_of,
        metrics=metrics, clock=server.clock, started=False,
    )  # fmt: skip
    delivery = SiemDelivery(
        store, lambda: settings().siem.outputs, lambda output: secret_of(edition.secrets, output), server.bus.lease,
        producing=pipeline.siem_on, host=urlsplit(origin).hostname, clock=server.clock, metrics=metrics,
        origin=origin, noted=gap_noted,  # its markers named by the server, each with its entry (R3-1)
    )  # fmt: skip
    # Under a lease it checks before each page (R2-10). Its events name a person by a pseudonym of a domain of their
    # own, never the exchange events' and the reports' (M1 of the whole-branch review: the Audit page names targets).
    forwarder = AdminForwarder(
        audit, store, store, server.bus.lease, outputs=outputs, producing=pipeline.siem_on, origin=origin,
        kind_of=kind_of, group_of=group_of, administers=administers, people=lambda: settings().reports.people,
        pseudonym=pipeline.pseudonyms.admin_of, empowers=empowers, noted=gap_noted, clock=server.clock,
    )  # fmt: skip

    began: list[bool | float] = [False, float("-inf")]  # whether the forwarding began, and when that was last read

    def forward() -> bool:
        """The administration log's forwarding begins once the license lists the SIEM: its first round records where
        it begins (`siem.forwarding`, by the server), so that a server that never had the SIEM writes nothing of it in
        its log. Once begun (here, or on another instance: its note, read once a minute while the license does not
        list the SIEM) it goes on in every standing of the license, what it does not send noted as one gap and marked
        when it sends again (R2-2): never entries of a time nothing was produced sent afterwards (R2)."""
        if not began[0] and not edition.licensing.licensed("siem"):
            now = time.monotonic()
            if now - float(began[1]) < WATCH_EVERY:
                return False
            began[1] = now
            if store.job(FORWARD_JOB)[1] is None:
                return False
        began[0] = True
        return forwarder.round()

    def deliver() -> bool:
        """The SIEM's delivery, while the license keeps events (lists reports or the SIEM): a server whose license keeps
        none has nothing to send, and asks its database nothing for it every few seconds."""
        return pipeline.keeps() and delivery.round()

    forward.__qualname__ = "AdminForwarder.round"  # as the jobs' thread names a job that failed
    deliver.__qualname__ = "SiemDelivery.round"
    watch = LicenseWatch(edition.licensing, store, lock, pipeline.put, clock=server.clock, policy=policy_in_force)
    retention = Retention(store, store, lock, days=retention_days, outputs=outputs, clock=server.clock)
    jobs = Jobs(
        [
            (DELIVERY_EVERY, deliver),  # first: the one ``wake`` runs at once
            (FORWARD_EVERY, forward),
            (WATCH_EVERY, watch.round),
            (RETENTION_EVERY, retention.round),
        ],
        started=False,
    )
    pipeline.wake = jobs.wake
    events = EventService(store, settings, pipeline, delivery, jobs, watch, audit, metrics, forward)
    metrics.bind(store.counts, output_kind)  # only a complete service may be retained by the registry's callbacks
    return events
