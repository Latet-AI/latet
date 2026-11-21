"""The administration log to the SIEM (E4): each entry E0b's `AuditLog` keeps (who, what, on what, when; never a
value) becomes an `admin` event in each output's queue, once. A job reads the log under a lease, by id from the last one
it read (E0b gives ids in the order entries commit: I4), a page at a time, each page queued with its note in one
transaction. What it reads and does not queue (no output, the license producing nothing) is one gap, marked in the
chain before the next page it queues (R2-2), with its entry of the log written in the same transaction (R3-1)."""

from __future__ import annotations

import functools
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, Protocol

from latet.ee.events.locks import LeaseFn
from latet.ee.events.schema import checked, epoch, iso
from latet.ee.events.store import GAP, EventStore, Noting, SiemQueue, gap_event
from latet.ee.roles import ACTIONS, Role
from latet.events import FIELDS, code_of
from latet.logline import Paced

log = logging.getLogger(__name__)

JOB = "admin-to-siem"
LOCK = "events:admin"
LOCK_SECONDS = 60.0
FIRST = 3600.0  # the first round starts at the entries of the last hour
PAGE = 1000  # entries read at once, by id
PAGES = 10  # pages a round reads at most: the rest at the next round, at once (the jobs run again a job that worked)
LATEST: Final = 1e12  # seconds: no clock gives an entry a time past this (the year 33658), nor before 0 (1970)
_HASH: Final = re.compile(r"[0-9a-f]{64}")
# Who administers is judged by the act as well (Task 8's review): each action of the log an identity makes through an
# Enterprise route, mapped to the action of E3's role table (`latet.ee.roles.ACTIONS`) the route's rule needs
# (`latet.ee.access.RULES`); one that only the admin or the security role may take names who took it, whatever roles
# they hold when the entry is read, so that a demotion right after acting hides nothing.
PERMISSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # E3's people, groups and roles: /ee/people, /ee/groups, /ee/roles.
        "person.added": "people.manage",
        "person.changed": "people.manage",
        "person.linked": "people.manage",
        "person.disabled": "people.manage",
        "person.enabled": "people.manage",
        "person.code.made": "people.manage",
        "group.added": "people.manage",
        "group.changed": "people.manage",
        "group.removed": "people.manage",
        "member.added": "people.manage",
        "member.removed": "people.manage",
        "role.granted": "people.manage",
        "role.revoked": "people.manage",
        # E3's API keys: /ee/keys and /ee/keys/*.
        "key.added": "keys.manage",
        "key.rotated": "keys.manage",
        "key.enabled": "keys.manage",
        "key.disabled": "keys.manage",
        "key.controller.added": "keys.manage",
        "key.controller.removed": "keys.manage",
        # E3's sign-in: /ee/sign-in.
        "sign-in.changed": "sign-in.edit",
        "sign-in.secret.changed": "sign-in.edit",
        "sign-in.tested": "sign-in.edit",
        "scim.token.made": "sign-in.edit",
        "scim.token.removed": "sign-in.edit",
        # E1's license and E2's policy: /ee/license, /ee/policy.
        "license.installed": "license.manage",
        "license.removed": "license.manage",
        "policy.changed": "policy.edit",
        # E4's SIEM outputs: /ee/siem (Task 10's routes; the skip `latet server`'s alone, under the same rule).
        "siem.changed": "siem.edit",
        "siem.tested": "siem.edit",
        "siem.secret.changed": "siem.edit",
        "siem.skipped": "siem.edit",
        # E4's reports and the administration log: /ee/reports and /ee/audit (Task 10's routes). An export is a read's
        # route made as a change (R4): it needs what reading needs. The reports' settings are `latet server`'s alone,
        # under the change of the rule of /ee/reports.
        "reports.exported": "reports.group",
        "audit.exported": "audit.read",
        "reports.changed": "reports.all",
        # E5's decisions on Company approvals (/ee/approvals): the security team's are the security role's acts,
        # named (Decision 1 (d)); an API key's controllers' (`key.approval.decided`) are their own work, as E3's
        # `key.space.changed`, kept as reports.people keeps a person [D5].
        "approval.decided": "approvals.decide",
        # A decider's first opening of another person's text there, its words or an image (the final security review's
        # N2): read to decide on it, so the security role's act, named as its decisions are.
        "approval.opened": "approvals.decide",
        # Who decides (/ee/approvals/settings, `latet server approvals set`'s alone): the security role's matter, set by
        # the administration [D3].
        "approvals.changed": "approvals.decide",
        # An admin deciding a key's change nobody else controls decides as an administrator (R47): named.
        "key.approval.admin-decided": "keys.manage",
        # E6's synced terms (/ee/term-sync, `latet server terms`): the security role's matter, named (Decision 1 (d)).
        "terms.source.changed": "terms.manage",
        "terms.secret.changed": "terms.manage",
        "terms.removed": "terms.manage",
        "terms.sync.asked": "terms.manage",
    }
)
_POWER: Final = frozenset({Role.ADMIN, Role.SECURITY})


def administering(table: Mapping[str, Collection[Role]]) -> frozenset[str]:
    """The actions of the log only the admin or the security role may take, as E3's role table ``table`` says: an
    action whose permission the table does not know is nobody's, and names no one."""
    return frozenset(
        action
        for action, permission in PERMISSIONS.items()
        if (roles := table.get(permission)) and frozenset(roles) <= _POWER
    )


ADMINISTERING: Final = administering(ACTIONS)
# An identity's id as an event carries one (E1's `make_event`), and a group's: what a code of the log must be to be
# taken for one. A code that begins as an identity's and is not one (a row written by hand) names no one.
_IDENTITY: Final = re.compile(r"u_[a-z0-9]{1,32}")
_GROUP: Final = re.compile(r"g_[a-z0-9]{1,32}")
# Decision 1 (d): an entry that gives or takes these roles names the person acted on, whatever `reports.people` says.
POWER_ACTIONS: Final = frozenset({"role.granted", "role.revoked"})
POWER_ROLES: Final = frozenset({"admin", "security"})
# R3-2: so does a person added to, or taken out of, a group that holds one of them (the member in the entry's details).
MEMBER_ACTIONS: Final = frozenset({"member.added", "member.removed"})
_UNREAD_NOTE: Final = (
    "the note of the administration log's forwarder cannot be read: it goes no further until it is put right"
)
_FORGED_NOTE: Final = (
    "the note of the administration log's forwarder does not hold (it was not written with this server's key): it goes"
    " no further until it is put right"
)
FORWARDING: Final = "siem.forwarding"  # the log's entry of where the forwarding began: `{from}`, by the server
_LOST_NOTE: Final = (
    "the note of the administration log's forwarder is gone though it forwarded before (siem.forwarding): it goes no"
    " further until it is put right"
)
PASSED: Final = (
    "the administration log's forwarder is past the newest entry the log holds (its sequence set back by hand?): the"
    " entries written next may never reach the SIEM; `latet server log verify` and the SIEM's copy say what"
)
UNHELD: Final = (
    "the administration log holds an entry whose keyed hash does not hold (changed, or written, by hand): it goes to"
    " the SIEM all the same, and `latet server log verify` says which"
)


class Entry(Protocol):
    """An entry of the administration log, as E0b's `AuditEntry` has it, with its place in the keyed chain."""

    @property
    def id(self) -> int: ...
    @property
    def prev(self) -> str: ...
    @property
    def hash(self) -> str: ...
    @property
    def ts(self) -> float: ...
    @property
    def actor(self) -> str: ...
    @property
    def action(self) -> str: ...
    @property
    def target(self) -> str | None: ...
    @property
    def details(self) -> Mapping[str, Any]: ...
    @property
    def address(self) -> str | None: ...


class AuditReader(Protocol):
    def query(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 500,
    ) -> Sequence[Entry]: ...

    def after(
        self,
        after_id: int,
        limit: int = ...,
        *,
        since: float | None = None,
        until: float | None = None,
        action: str | None = None,
    ) -> Sequence[Entry]: ...

    def newest_id(self) -> int: ...

    def holds(self, entry: Entry) -> bool: ...

    def first_kept(self) -> int: ...

    def tag(self, text: str) -> str: ...

    def tagged(self, text: str, tag: object) -> bool: ...


def _chain(entry: Entry) -> dict[str, str] | None:
    """The entry's place in the administration log's keyed chain (R6): its ``prev`` and ``hash`` as the log holds them,
    which a copy kept by the SIEM is compared with on the server. None for a row whose hashes are not of the chain's
    shape (written by hand: `latet server log verify` finds it), so that the event goes all the same."""
    prev, found = getattr(entry, "prev", ""), getattr(entry, "hash", "")
    if (
        isinstance(prev, str)
        and isinstance(found, str)
        and _HASH.fullmatch(found)
        and (prev == "" or _HASH.fullmatch(prev))
    ):
        return {"prev": prev, "hash": found}
    return None


def _kept(identity: str, people: str, pseudonym: Callable[[str], str]) -> str | None:
    """A person as ``reports.people`` keeps one: never, by a pseudonym, or by their id. A choice it does not know keeps
    no person (as the pipeline's ``_person``)."""
    if people == "named":
        return identity
    if people == "pseudonymous":
        return pseudonym(identity)
    return None


def _stamp(ts: object, at: float) -> str:
    """An entry's time as an event writes it, or ``at`` (the round's) for a time no clock gives: not a number, an
    infinity, before 1970 or past ``LATEST`` (a row written by hand). The entry goes all the same, its chain placing
    it: never an event the schema refuses at every round."""
    try:
        if isinstance(ts, int | float) and not isinstance(ts, bool) and math.isfinite(ts) and 0 <= ts < LATEST:
            stamp = iso(ts)
            iso(epoch(stamp))  # as the schema reads it back (``checked``)
            return stamp
    except (ValueError, OverflowError, OSError):  # what a platform's clock cannot say
        pass
    return iso(at)


def _own(action: str, actor: str, target: str | None, member: str | None, details: Mapping[str, Any]) -> bool:
    """Whether an action that names who took it when an administrator takes it for someone else is the person's own,
    which their sign-in or their session records: their account made or linked, or a code for their own computer (they
    are its target), a membership the identity provider names for them (they are the member), a group their sign-in
    made (E3's `oidc.py`, `sign_in_api.py`), a request of their own withdrawn on Company approvals (E5's
    `approvals_api.py` marks it `withdrawn`: whoever asked takes no decision of the security role's, the whole-branch
    security review's L2)."""
    if action in ("person.added", "person.linked", "person.code.made"):
        return target == actor
    if action == "approval.decided":
        return details.get("withdrawn") is True
    if action in MEMBER_ACTIONS:  # only a sign-in records one's own (an administrator's by hand: source local)
        return member == actor and details.get("source") == "oidc"
    if action == "group.added":
        return details.get("source") == "oidc"
    return False


def admin_event(
    entry: Entry,
    *,
    origin: str,
    kind_of: Callable[[str], str | None],
    group_of: Callable[[str], str | None],
    administers: Callable[[str], bool],
    people: str,
    pseudonym: Callable[[str], str],
    empowers: Callable[[str], bool] | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """The `admin` event of an entry (R5, R6; Decision 1 (d), I12): the action as ``outcome``; who acted as
    ``identity`` when an identity did, named when it administers or is an API key, and otherwise kept as
    ``reports.people`` keeps a person (a person's own acts: connecting a computer, linking their account, a code of
    their own); any other actor as ``reason`` (``admin:<token id>``, E3's ``scim:<token id>``, ``server``,
    ``unknown``); what it acted on as ``rule``, a person kept as ``reports.people`` keeps one whoever acted; the entry's
    place in the chain as ``chain``; and an id made from the server's address and the entry's, so that a receiver can
    drop one sent twice. Never the entry's details or address. A person given or taken the admin or the security role is
    named in ``rule`` whatever ``reports.people`` says, and so is a person added to or taken out of a group that holds
    one now (``empowers``, R3-2: ``rule`` is then the member, not the group).

    Who administers is judged by the act as well as by the roles held when the entry is read (``administers``): an act
    only the admin or the security role may take (``ADMINISTERING``, unless it is the person's own: ``_own``) names who
    took it, so that a demotion right after acting hides nothing (Task 8's review).

    What the entry says is read as a code (``code_of``: a shape, its callers' ids, never a secret: the log holds none),
    and its time as a clock gives one (``_stamp``: else ``at``, the round's), so that a row written by hand never makes
    an event the schema refuses, which would hold every later entry: an actor or a target that begins as an identity's
    id and is not one names no one."""
    actor = code_of(entry.actor, 80)
    personal = actor is not None and actor.startswith("u_")
    identity = actor if personal and actor is not None and _IDENTITY.fullmatch(actor) else None
    action = str(entry.action)
    target = code_of(entry.target, 80)
    details = entry.details if isinstance(entry.details, Mapping) else {}
    given = details.get("identity")
    member = code_of(given, 80) if isinstance(given, str) else None
    kind = kind_of(identity) if identity else None
    shown = identity
    if identity is not None and kind != "key":
        acted = action in ADMINISTERING and not _own(action, identity, target, member, details)
        if not (acted or administers(identity)):
            shown = _kept(identity, people, pseudonym)
    role = details.get("role")
    power = action in POWER_ACTIONS and isinstance(role, str) and role in POWER_ROLES
    if (
        action in MEMBER_ACTIONS
        and empowers is not None
        and target is not None
        and _GROUP.fullmatch(target)
        and member is not None
        and _IDENTITY.fullmatch(member)
        and empowers(target)
    ):
        target, power = member, True  # R3-2: power gained or lost through a group names the member, as a grant does
    rule = target
    if target is not None and target.startswith("u_") and not power:
        # A person acted on, as reports.people keeps one (an API key is a program: by its id); one that is not of an
        # identity's shape names no one.
        if not _IDENTITY.fullmatch(target):
            rule = None
        elif kind_of(target) != "key":
            rule = _kept(target, people, pseudonym)
    event = dict.fromkeys(FIELDS) | {
        "v": 1,
        "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{origin}#audit:{entry.id}").hex,
        "ts": _stamp(entry.ts, time.time() if at is None else at),
        "type": "admin",
        "identity": shown,
        "kind": kind,
        "group": group_of(identity) if identity else None,
        "masked": {},
        "stopped": 0,
        "reason": None if personal else actor,
        "outcome": code_of(action, 80),
        "rule": rule,
        "chain": _chain(entry),
    }
    return checked(event)


# L2 of the final security review: an entry goes to the SIEM with its people kept as `reports.people` was when it was
# written (or as it is now, when that is more private), which the log's own `reports.changed` entries say: each holds
# the fields it changed and the section as it set it (`EventsApi.reports_change`).
CHANGED: Final = "reports.changed"
MODES: Final = ("group-only", "pseudonymous", "named")  # the most private first
MOST_CHANGES: Final = 1_000  # `reports.changed` entries a round reads to find the mode in force where it starts


def _private(*modes: str) -> str:
    """The most private of ``modes``: one Latet does not know keeps no person (``_kept``), and is the most private."""
    return min(modes, key=lambda mode: MODES.index(mode) if mode in MODES else -1)


def _set_by(entry: Entry, audit: AuditReader) -> str | None:
    """The `reports.people` a `reports.changed` entry set; None for any other entry, and for one that names no mode or
    whose keyed hash does not hold: a row written by hand never says which mode was in force (R4-4)."""
    if entry.action != CHANGED or not isinstance(entry.details, Mapping):
        return None
    people = entry.details.get("people")
    return people if isinstance(people, str) and people in MODES and audit.holds(entry) else None


_GAP_FIELDS: Final = frozenset({"first", "last", "count", "prev", "hash"})


def _body(last: object, gap: object) -> str:
    """What a note's tag is made of: the place read and the gap left, as the note writes them."""
    return json.dumps({"gap": gap, "last": last}, sort_keys=True)


def _note(last: int, gap: Mapping[str, Any] | None, audit: AuditReader) -> str:
    """The job's note: the last id read, the gap left since the last page queued, not marked yet (R2-2), and their
    tag under the log's key (``AuditReader.tag``), as the anchor of the chain is tagged: the note lives in a table
    anyone who writes the database could change, and a note written by hand would make Latet skip entries, or mark and
    record as its own a gap it never left (Task 8's review)."""
    return json.dumps({"gap": gap, "last": last, "tag": audit.tag(_body(last, gap))}, sort_keys=True)


def _noted(text: str, audit: AuditReader) -> tuple[int, dict[str, Any] | None]:
    """The note ``_note`` wrote: its last id read, and its gap. ValueError, in fixed words that never quote it, for a
    note whose tag does not hold (written without this server's key) and for one of any form a round never writes (an id
    that is no count; a gap that does not end at that id, starts before the first id or after its end, or counts more
    entries than its ids or none): the job goes no further, never from an id it cannot trust nor with a marker of a
    range it was not given."""
    try:
        note = json.loads(text)
        body = _body(note.get("last"), note.get("gap")) if isinstance(note, dict) else None
    except (ValueError, RecursionError):
        raise ValueError(_UNREAD_NOTE) from None
    if not isinstance(note, dict) or body is None:
        raise ValueError(_UNREAD_NOTE)
    if not audit.tagged(body, note.get("tag")):
        raise ValueError(_FORGED_NOTE)
    last, gap = note.get("last"), note.get("gap")
    if set(note) != {"last", "gap", "tag"} or type(last) is not int or last < 0:
        raise ValueError(_UNREAD_NOTE)
    if gap is None:
        return last, None
    if not (
        isinstance(gap, dict)
        and set(gap) == _GAP_FIELDS
        and all(type(gap[name]) is int for name in ("first", "last", "count"))
        and all(gap[name] is None or isinstance(gap[name], str) for name in ("prev", "hash"))
    ):
        raise ValueError(_UNREAD_NOTE)
    first, end, count = gap["first"], gap["last"], gap["count"]
    if not (end == last and 1 <= first <= end and 1 <= count <= end - first + 1):
        raise ValueError(_UNREAD_NOTE)
    return last, {name: gap[name] for name in ("first", "last", "count", "prev", "hash")}


def _widened(gap: Mapping[str, Any] | None, entries: Sequence[Entry]) -> dict[str, Any]:
    """The gap Latet leaves, widened by ``entries`` it read and did not queue: the first id, the last, how many, and
    the chain across them, the ``prev`` of the first entry left out and the ``hash`` of the last, only while every
    entry it stands for has a chain and each follows the one before (as Task 4's marker of a collapse): ``prev`` and
    ``hash`` null otherwise, never a link the entries do not make (an entry taken out while nothing was sent), nor a
    missing chain read as the log's first."""
    chains = [_chain(entry) for entry in entries]
    if gap is None:
        start = chains[0]
        first, count = entries[0].id, 0
        prev = start["prev"] if start is not None else None
        running: str | None = prev  # what the next entry must follow
    else:
        first, count, prev = gap["first"], gap["count"], gap["prev"]
        running = gap["hash"] if prev is not None else None
    for found in chains:
        if running is None or found is None or found["prev"] != running:
            running = None
            break
        running = found["hash"]
    if running is None:
        prev = None
    return {"first": first, "last": entries[-1].id, "count": count + len(entries), "prev": prev, "hash": running}


def _both(*writes: Callable[[Any], None] | None) -> Callable[[Any], None] | None:
    """The entries of the log a page writes in its transaction, in order (where the forwarding began, a gap's): one
    ``noted`` for ``put_audit``, None when there are none."""
    found = [write for write in writes if write is not None]
    if not found:
        return None

    def write(conn: Any) -> None:
        for each in found:
            each(conn)

    return write


class AdminForwarder:
    def __init__(
        self,
        audit: AuditReader,
        queue: SiemQueue,
        store: EventStore,
        lease: LeaseFn,
        *,
        outputs: Callable[[], Sequence[str]],
        producing: Callable[[], bool],
        origin: str,
        kind_of: Callable[[str], str | None],
        group_of: Callable[[str], str | None],
        administers: Callable[[str], bool],
        people: Callable[[], str],
        pseudonym: Callable[[str], str],
        empowers: Callable[[str], bool] | None = None,
        noted: Noting | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.audit, self.queue, self.store, self.lease = audit, queue, store, lease
        self.outputs, self.producing, self.origin = outputs, producing, origin
        self.kind_of, self.group_of, self.administers = kind_of, group_of, administers
        self.people, self.pseudonym, self.clock = people, pseudonym, clock
        self.empowers, self.noted = empowers, noted  # R3-2; R3-1: what writes a gap's entry of the log
        self._passed = Paced(60.0, clock=clock)  # a place past the newest entry, said once a minute

    def round(self) -> bool:
        """Queue for each output the entries written since the last round, by id: each once, in order, the job's note
        (the last id read, and a gap not marked yet) written in the same transaction as the queue's rows (I4), only over
        the note the round read, and only while the lease is still this instance's (R2-10). While the SIEM is paused, or
        has no output, the entries are only noted, as one gap; the next page queued begins with its marker (R2-2). The
        first round notes where it starts (``_start``), though it reads nothing, and records it in the log
        (`siem.forwarding {from}`, with its first note): once that entry exists, a note gone (deleted, or set to
        nothing, by hand) stops the job in fixed words, never a first round again that would skip what came before the
        last hour. The note is tagged with the log's key (``_note``), and one whose tag does not hold stops the job
        (``_noted``). A page that holds an entry whose keyed hash does not hold is said, in fixed words, and so is a
        round that reads nothing while its place is past the newest entry the log holds (a sequence set back by hand),
        once a minute. Who each identity is is asked once a round (R2-10). Each entry is kept as `reports.people` was
        when it was written, read from the log's `reports.changed` entries where the round starts and as its pages cross
        one, or as it is now when that is more private: a backlog sent after a switch to `named` names none of what came
        before it (L2 of the final security review). True when some were queued, or a whole round's pages were read: the
        next may follow at once."""
        with self.lease(LOCK, LOCK_SECONDS) as lease:
            if not lease:
                return False
            now = self.clock()
            _, text = self.store.job(JOB)
            if text is not None:
                last, gap = _noted(text, self.audit)
            elif self.audit.after(0, 1, action=FORWARDING):
                raise ValueError(_LOST_NOTE)  # forwarded before: its note was taken away
            else:
                last, gap = self._start(now), None
            beginning = self._beginning(last) if text is None else None  # with the first note written
            empowers = functools.cache(self.empowers) if self.empowers is not None else None
            known = (functools.cache(self.kind_of), functools.cache(self.group_of), functools.cache(self.administers))
            queued = more = False
            in_force: str | None = None  # the people in force where the round reads: from the log, as a page is queued
            for _ in range(PAGES):
                entries = list(self.audit.after(last, PAGE))
                if not lease.held():  # the lease's next holder reads on from the note
                    break
                if not entries:
                    if text is None:  # the first round: where it starts, noted and recorded
                        note = _note(last, None, self.audit)
                        self.queue.put_audit(
                            [], [], refs=[], job=JOB, note=note, expected=None, at=now, noted=beginning
                        )
                    elif self._passed_by(last) and self._passed.due():
                        log.warning(PASSED)
                    break
                if not all(self.audit.holds(entry) for entry in entries):
                    log.warning(UNHELD)
                outputs = list(self.outputs()) if self.producing() else []
                if outputs:
                    people = self.people()
                    if in_force is None:
                        in_force = self._in_force_after(last, people)
                    events = [self._gap(gap, now)] if gap is not None else []
                    for entry in entries:  # L2: each kept as people were when it was written, or now if more private
                        events.append(self._event(entry, _private(in_force, people), known, empowers, now))
                        in_force = _set_by(entry, self.audit) or in_force
                    refs = ([gap["first"]] if gap is not None else []) + [entry.id for entry in entries]
                    recording, gap = self._recording(gap), None
                else:
                    events, refs, gap, recording = [], [], _widened(gap, entries), None
                    in_force = None  # read again from the log where the next page queued starts
                last = entries[-1].id
                note = _note(last, gap, self.audit)
                written = self.queue.put_audit(
                    outputs, events, refs=refs, job=JOB, note=note, expected=text, at=now,
                    noted=_both(beginning, recording),
                )  # fmt: skip
                if not written:
                    break  # another instance moved the note since it was read: nothing written
                text, queued, more, beginning = note, queued or bool(events), len(entries) == PAGE, None
                if not more:
                    break
            return queued or more

    def _gap(self, gap: Mapping[str, Any], now: float) -> dict[str, Any]:
        """The marker of a gap Latet left (R2-2): its ids, how many, the chain across it."""
        return gap_event(gap["first"], gap["last"], gap["count"], gap["prev"], gap["hash"], origin=self.origin, at=now)

    def _recording(self, gap: Mapping[str, Any] | None) -> Callable[[Any], None] | None:
        """R3-1: what writes a gap's entry of the log, `siem.gap {output: null, from, to, count}` by the server, in the
        transaction that queues its marker; the entry is read by a later round like any other."""
        noted = self.noted
        if gap is None or noted is None:
            return None
        details = {"output": None, "from": gap["first"], "to": gap["last"], "count": gap["count"]}
        return lambda conn: noted(conn, GAP, details)

    def _beginning(self, start: int) -> Callable[[Any], None] | None:
        """What records where the forwarding begins, `siem.forwarding {from}` by the server, in the transaction that
        writes the first note (Task 8's re-review): the entry that tells a note gone from a first round."""
        noted = self.noted
        if noted is None:
            return None
        return lambda conn: noted(conn, FORWARDING, {"from": start})

    def _in_force_after(self, last: int, now: str) -> str:
        """The `reports.people` in force when the entry after ``last`` was written (L2), from the newest
        `reports.changed` entries that hold (``_set_by``; ``MOST_CHANGES`` of them, newest first): the mode the newest
        at or before ``last`` set. With none kept there (none was written, or retention took it): the mode the first
        after ``last`` set, when it left `people` as it was; the most private of the two others, when it changed it; and
        ``now``, the mode now, when none came after either (then none changed it since). When more changes were written
        than are read, and none of those read is at or before ``last``: `group-only`."""
        found = list(self.audit.query(action=CHANGED, limit=MOST_CHANGES))
        changes = [(entry, mode) for entry in found if (mode := _set_by(entry, self.audit)) is not None]
        before = next((mode for entry, mode in changes if entry.id <= last), None)
        if before is not None:
            return before
        if len(found) >= MOST_CHANGES:
            return MODES[0]
        after = [(entry, mode) for entry, mode in changes if entry.id > last]
        if not after:
            return now
        first, mode = min(after, key=lambda pair: pair[0].id)
        fields = first.details.get("fields")
        if isinstance(fields, list) and "people" not in fields:
            return mode
        return _private(*(other for other in MODES if other != mode))

    def _passed_by(self, last: int) -> bool:
        """Whether the place noted is past the newest entry the log holds, and past the last one a prune dropped: the
        entries written next would take ids below it, which no round reads (a sequence set back by hand, ``setval``)."""
        return last > self.audit.newest_id() and last > self.audit.first_kept() - 1

    def _start(self, now: float) -> int:
        """Where the first round begins: before the first entry dated within ``FIRST`` of now (the log's last hour, and
        no further ahead), else after the log's newest entry; by the ids the log's sequence gave, as ``after`` reads
        them. An entry dated by hand long ago, or far ahead, or past the sequence (Task 8's review) moves the start
        neither past the entries of the last hour nor past those to come. The newest entry is read first: one committed
        between the two reads is then never passed over (Task 8's re-review)."""
        newest = int(self.audit.newest_id())
        recent = list(self.audit.after(0, 1, since=now - FIRST, until=now + FIRST))
        return int(recent[0].id) - 1 if recent else newest

    def _event(
        self,
        entry: Entry,
        people: str,
        known: tuple[Callable[..., Any], ...],
        empowers: Callable[[str], bool] | None,
        now: float,
    ) -> dict[str, Any]:
        kind_of, group_of, administers = known
        return admin_event(
            entry, origin=self.origin, kind_of=kind_of, group_of=group_of, administers=administers, people=people,
            pseudonym=self.pseudonym, empowers=empowers, at=now,
        )  # fmt: skip
