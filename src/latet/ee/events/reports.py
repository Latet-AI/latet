"""Reports of a server's event log: by period, group, project, agent and kind of value, with the period before for
the trend; by person only as the company chose (``reports.people``) and as the one asking may see; by API key always;
and the administration log as the Audit page and its export show it. Counts only: the log holds no value, and a report
adds none."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import Counter
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, Literal

from latet.ee.events.admin import Entry
from latet.ee.events.schema import _TYPE, NotAnEvent, _project, iso
from latet.ee.events.store import REQUESTS, SUMS, EventStore
from latet.ee.roles import can
from latet.events import CODE

People = Literal["group-only", "pseudonymous", "named"]
_GROUP_ID = re.compile(r"g_[a-z2-7]{10}")  # E3's group ids (latet.ee.people.GROUP_ID, which needs the server extra)
MOST_ROWS = 500_000  # rows an export holds at most: past them it is refused (413), never cut (M3)
MOST_OPTIONS = 200  # choices of each filter at most (M6: a client names its projects)
MOST_ADMINISTRATION = 5_000  # entries of the administration log a report's JSON export holds at most (I11)
# REQUESTS (the store's): the requests a report counts, a gateway's exchange, a stopped one, an applications-API call.
DECISIONS = ("approval", "change")  # E5's, counted by who decided
MAX_DAYS = 366
DEFAULT_DAYS = 30
WEEKS_AFTER = 92  # days: a longer period is shown by the week
# The days a report reads: no event is older, and none is dated past them; a day outside them is said in plain words,
# never an error of the clock (before 1970, or past the year 9999, which Python's dates do not hold).
FIRST_DAY, LAST_DAY = date(2000, 1, 1), date(2999, 12, 31)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FORMULA = ("=", "+", "-", "@", chr(9), chr(13))  # what a spreadsheet would take for the start of a formula
_SCOPE = ("kind", "group")  # what of the viewer's scope the filters' choices are read within


class ReportError(ValueError):
    """A report that cannot be made as asked, said in plain words (400)."""


class TooLarge(ReportError):
    """An export past ``MOST_ROWS`` rows (413): refused, never cut."""


class NotAllowed(Exception):
    """A report the one asking may not see (403)."""


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def step(self) -> Literal["day", "week"]:
        return "week" if self.days > WEEKS_AFTER else "day"

    def previous(self) -> Period:
        """The period of the same length just before: the trend's."""
        return Period(self.start - timedelta(days=self.days), self.start - timedelta(days=1))

    def bounds(self, tz: tzinfo | None) -> tuple[float, float]:
        """From midnight of its first day to midnight after its last, in seconds (``tz`` None: the instance's)."""

        def midnight(day: date) -> float:
            return datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()

        return midnight(self.start), midnight(self.end + timedelta(days=1))

    def buckets(self) -> list[str]:
        """Its days, or the Mondays of its weeks: the series' keys."""
        day = self.start if self.step == "day" else self.start - timedelta(days=self.start.weekday())
        step = timedelta(days=1 if self.step == "day" else 7)
        out = []
        while day <= self.end:
            out.append(day.isoformat())
            day += step
        return out

    def bucket(self, hour: int, tz: tzinfo | None) -> str:
        """The series' key of an hour the log counted: the day it starts on (or its week's Monday), kept within the
        period. The log counts whole hours: in a zone of half or quarter hours (+05:30, +05:45) the first hour a period
        touches starts the day before it, and its requests are the first day's."""
        day = min(max(datetime.fromtimestamp(hour * 3600, tz).date(), self.start), self.end)
        if self.step == "week":
            day -= timedelta(days=day.weekday())
        return day.isoformat()

    @classmethod
    def parse(cls, start: str | None, end: str | None, today: date) -> Period:
        """A period as asked (``YYYY-MM-DD``, both days included): the last 30 days unless said, a year at most, within
        ``FIRST_DAY`` and ``LAST_DAY``."""
        if any(text and not _DATE.fullmatch(text) for text in (start, end)):
            raise ReportError("dates are written YYYY-MM-DD")
        try:
            last = date.fromisoformat(end) if end else today
            first = date.fromisoformat(start) if start else last - timedelta(days=DEFAULT_DAYS - 1)
        except (ValueError, OverflowError):
            raise ReportError("dates are written YYYY-MM-DD") from None
        if first > last:
            raise ReportError("the period ends before it starts")
        if first < FIRST_DAY or last > LAST_DAY:
            raise ReportError(f"a report reads the days between {FIRST_DAY} and {LAST_DAY}")
        if (last - first).days + 1 > MAX_DAYS:
            raise ReportError(f"a report covers {MAX_DAYS} days at most")
        return cls(first, last)


@dataclass(frozen=True)
class Filters:
    group: str | None = None
    project: str | None = None
    agent: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Viewer:
    """What one person may see: every group (``groups`` None) or, as a group lead, the groups they belong to; people as
    far as ``people`` goes; and the SIEM outputs (``siem``: edit, read, or not at all)."""

    groups: frozenset[str] | None
    people: People
    siem: Literal["edit", "read"] | None


def report_viewer(*, roles: Collection[Any], member_of: Collection[str], people: People) -> Viewer | None:
    """What someone with ``roles`` (E3's) may see of the reports, as the design's table says; None: nothing. Security
    edits the SIEM outputs, auditors read them; a group lead sees the groups they belong to."""
    if can(roles, "reports.all"):
        return Viewer(None, people, "edit" if can(roles, "siem.edit") else "read")
    if can(roles, "reports.group") and member_of:
        return Viewer(frozenset(member_of), people, None)
    return None


def reads_audit(roles: Collection[Any]) -> bool:
    """Whether someone with ``roles`` reads the administration log: auditors, the security role, administrators."""
    return can(roles, "audit.read")


@dataclass
class Names:
    """How a report names what it shows: groups and identities by name, an identity's kind, people as the log kept
    them."""

    groups: Mapping[str, str] = field(default_factory=dict)
    identities: Mapping[str, str] = field(default_factory=dict)
    kinds: Mapping[str, str] = field(default_factory=dict)

    def person(self, identity: str, people: People) -> str | None:
        """A person as the viewer may see them; None: not shown. Under `pseudonymous`, by the pseudonym the log kept
        alone: a row kept by an id while `named` was in force shows no one, never the pseudonym the person's new events
        carry, which would let whoever saw it named follow them from then on (L1 of the final security review)."""
        if not identity or people == "group-only":
            return None
        if identity.startswith("p_"):
            return identity  # kept as a pseudonym when it happened
        return self.identities.get(identity, identity) if people == "named" else None

    def key(self, identity: str) -> str:
        """An API key by its name: a program, shown whatever ``people`` says."""
        return self.identities.get(identity, identity)

    def group(self, group: str) -> str | None:
        return self.groups.get(group, group) if group else None

    def target(self, target: str | None) -> str | None:
        """What an entry of the administration log acted on, by its name when it is an identity (a `u_` id) or a group
        (an id shaped as E3's): anything else (a section, `siem`) is looked up as nothing (M9)."""
        if not target:
            return None
        if target.startswith("u_"):
            return self.identities.get(target)
        return self.groups.get(target) if _GROUP_ID.fullmatch(target) else None


class _Tally:
    """Requests, stopped ones and values masked, by key. With a kind of value chosen: only the requests that masked
    one of that kind, and only its values."""

    def __init__(self, category: str | None) -> None:
        self.category = category
        self.rows: dict[Any, dict[str, Any]] = {}

    def add(self, key: Any, row: Mapping[str, Any]) -> None:
        kind = row["category"]
        if key is None or row["type"] not in REQUESTS or (self.category is not None and kind != self.category):
            return
        tally = self.rows.setdefault(key, {"exchanges": 0, "stopped": 0, "masked": 0, "by_category": Counter()})
        if kind:
            tally["masked"] += row["masked"]
            tally["by_category"][kind] += row["masked"]
        if not kind or self.category is not None:  # the requests themselves: the '' rows, or the chosen kind's
            tally["exchanges"] += row["events"]
            tally["stopped"] += row["events"] if row["type"] == "stopped" else 0

    def get(self, key: Any) -> dict[str, int]:
        found = self.rows.get(key) or {"exchanges": 0, "stopped": 0, "masked": 0}
        return {name: found[name] for name in ("exchanges", "stopped", "masked")}


def _checked(filters: Filters) -> None:
    """Each filter as the log keeps what it chooses: refused otherwise (400), in fixed words that never repeat it."""
    group, project, agent, category = filters.group, filters.project, filters.agent, filters.category
    if group is not None and not (isinstance(group, str) and _GROUP_ID.fullmatch(group)):
        raise ReportError("a group is chosen by its id")
    if project is not None:
        try:
            _project(project)
        except NotAnEvent:
            raise ReportError("a project is a name of 200 printable characters at most, never a path") from None
    if agent is not None and not (isinstance(agent, str) and CODE.fullmatch(agent)):
        raise ReportError("an agent is a code: lowercase letters, digits and . _ : -, 80 at most")
    if category is not None and not (isinstance(category, str) and _TYPE.fullmatch(category)):
        raise ReportError("a kind of value is chosen by its type, such as EMAIL or IBAN")


def _where(filters: Filters, viewer: Viewer) -> dict[str, set[str]]:
    """What every query of a report or an export keeps: the filters (each checked: ``_checked``), within what the
    viewer may see. A group lead sees the groups they belong to (NotAllowed for another) and no API key: a key is a
    program in no group (R8), and one whose event carried a group all the same is still none of theirs."""
    _checked(filters)
    where: dict[str, set[str]] = {}
    if filters.group is not None:
        if viewer.groups is not None and filters.group not in viewer.groups:
            raise NotAllowed("you see the reports of the groups you belong to")
        where["group"] = {filters.group}
    elif viewer.groups is not None:
        where["group"] = set(viewer.groups)
    if viewer.groups is not None:
        where["kind"] = {"person", ""}
    if filters.project is not None:
        where["project"] = {filters.project}
    if filters.agent is not None:
        where["agent"] = {filters.agent}
    return where


def _within(where: Mapping[str, Collection[str]], name: str, values: Collection[str]) -> dict[str, set[str]]:
    """``where`` with ``name`` kept to ``values`` as well: within what it keeps already, never wider (a group lead's
    kinds stay theirs: asked for the API keys, a group lead's report reads none)."""
    out = {key: set(chosen) for key, chosen in where.items()}
    out[name] = set(values) & out[name] if name in out else set(values)
    return out


def _sums(row: Mapping[str, Any]) -> dict[str, int]:
    return {name: int(row[name]) for name in SUMS}


def _left(total: Mapping[str, Any], shown: Sequence[Mapping[str, Any]], hidden: int) -> dict[str, int]:
    """What a ranked list leaves out: how many of its keys it does not show, and its totals minus the rows it shows."""
    return {"count": hidden} | {name: int(total[name]) - sum(int(row[name]) for row in shown) for name in SUMS}


def _by_code(codes: Iterable[Mapping[str, Any]], kind: str, default: str = "") -> dict[str, int]:
    """Events of one type by their code (outcome or reason), from ``sums`` by type and code."""
    out: Counter[str] = Counter()
    for row in codes:
        if row["type"] == kind and row["exchanges"]:
            out[row["code"] or default] += int(row["exchanges"])
    return dict(out)


def _summary(
    store: EventStore, period: Period, filters: Filters, viewer: Viewer, *, tz: tzinfo | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What a report and its JSON export begin with, read once: the period and the one before, the filters, how people
    are shown, the totals (with the decisions, the applications API's calls and the uncounted requests among them) and
    the period before's; and the counts by type and code it took them from."""
    where = _where(filters, viewer)
    requests = _within(where, "type", REQUESTS)
    since, until = period.bounds(tz)
    before = period.previous()
    total = store.sums(since, until, (), requests, category=filters.category)[0][0]
    earlier = store.sums(*before.bounds(tz), (), requests, category=filters.category)[0][0]
    codes, _ = store.sums(since, until, ("type", "code"), where)  # every type: the decisions' among them
    uncounted = sum(int(r["exchanges"]) for r in codes if r["type"] == "exchange" and r["code"] == "uncounted")
    summary = {
        "period": {
            "from": period.start.isoformat(),
            "to": period.end.isoformat(),
            "days": period.days,
            "step": period.step,
        },
        "previous": {"from": before.start.isoformat(), "to": before.end.isoformat()},
        "filters": {
            "group": filters.group,
            "project": filters.project,
            "agent": filters.agent,
            "category": filters.category,
        },
        "people": viewer.people,
        "totals": _sums(total)
        | {
            "approvals": _by_code(codes, "approval"),
            "changes": _by_code(codes, "change"),
            "apps": _by_code(codes, "apps"),  # the applications API's calls by operation (R7)
            "uncounted": uncounted,
        },
        "before": _sums(earlier),
    }
    return summary, codes


def _people(
    rows: Iterable[Mapping[str, Any]], key: str, names: Names, people: People
) -> tuple[dict[str, list[Mapping[str, Any]]], int]:
    """The rows the store ranked by an identity (``key``: who did, or who decided), by the person each is shown as,
    and how many ids are shown. Naming comes after the store's limit: two ids shown under one name (a person kept by
    their id, then by their pseudonym; two people of one name) are one person; one past the limit is among the others
    though a row shown has its name."""
    shown: dict[str, list[Mapping[str, Any]]] = {}
    ids = 0
    for row in rows:
        person = names.person(row[key], people)
        if person is not None:
            shown.setdefault(person, []).append(row)
            ids += 1
    return shown, ids


def build_report(
    store: EventStore, period: Period, filters: Filters, viewer: Viewer, names: Names, *, tz: tzinfo | None = None
) -> dict[str, Any]:
    """The report of ``period`` as ``viewer`` may see it. NotAllowed: a group they do not belong to; ReportError: a
    filter the log could not hold. Every read keeps the viewer's groups (``_where``) and is counted and ranked in the
    store (``EventStore.sums``): each list shows its first ``MOST_OPTIONS`` keys, and ``others`` says what each leaves
    out (how many keys, and the totals minus the rows shown). A report reads what it shows, never every row."""
    summary, codes = _summary(store, period, filters, viewer, tz=tz)
    where = _where(filters, viewer)
    requests = _within(where, "type", REQUESTS)
    since, until = period.bounds(tz)
    category, totals = filters.category, summary["totals"]

    def top(
        by: str, kept: Mapping[str, Collection[str]] = requests, most: int = MOST_OPTIONS
    ) -> tuple[list[dict[str, Any]], int]:
        return store.sums(since, until, (by,), kept, category=category, limit=most)

    def whole(kept: Mapping[str, Collection[str]]) -> dict[str, Any]:
        return store.sums(since, until, (), kept, category=category)[0][0]

    series = {key: dict.fromkeys(SUMS, 0) for key in period.buckets()}
    for row in store.sums(since, until, ("hour",), requests, category=category)[0]:  # 8,784 hours at most
        bucket = series.get(period.bucket(int(row["hour"]), tz))
        if bucket is not None:  # always: ``bucket`` keeps each hour within the period
            for name in SUMS:
                bucket[name] += int(row[name])
    lists: dict[str, Any] = {}
    others: dict[str, Any] = {}
    for name, by, label in (("by_group", "group", names.group), ("by_project", "project", _or_none),
                            ("by_agent", "agent", _or_none)):  # fmt: skip
        rows, keys = top(by)
        lists[name] = [{"key": row[by], "label": label(row[by]), **_sums(row)} for row in rows]
        others[name] = _left(totals, lists[name], keys - _named(rows, by))
    api_keys = _within(requests, "kind", {"key"})  # none, for a group lead
    rows, keys = top("identity", api_keys)
    lists["by_key"] = [{"key": row["identity"], "label": names.key(row["identity"]), **_sums(row)} for row in rows]
    others["by_key"] = _left(whole(api_keys), lists["by_key"], keys - _named(rows, "identity"))
    lists["by_person"] = lists["by_approver"] = others["by_person"] = others["by_approver"] = None
    if viewer.people != "group-only":
        persons = _within(requests, "kind", {"person", ""})
        rows, keys = top("identity", persons, MOST_OPTIONS + 1)  # no one (kept by group only) takes no place
        shown, ids = _people(_some(rows, "identity"), "identity", names, viewer.people)
        merged: list[dict[str, Any]] = [{"key": p, "label": p} | _added(found) for p, found in shown.items()]
        lists["by_person"] = sorted(merged, key=lambda r: (-r["exchanges"], -r["masked"], r["key"]))
        # Among what it leaves out: the requests of no one shown (kept by group only, before the company chose).
        others["by_person"] = _left(whole(persons), lists["by_person"], keys - ids)
        lists["by_approver"], others["by_approver"] = _approvers(store, (since, until), where, names, viewer.people,
                                                                 totals)  # fmt: skip
    kinds = [r for r in top("category", most=MOST_OPTIONS + 1)[0] if r["category"]][:MOST_OPTIONS]
    stops = _by_code(codes, "stopped", "other")
    # The filters' choices: the most asked for of each, within the viewer's groups (and the group chosen), never
    # another's, nor an API key's for a group lead; whatever project, agent or kind of value is chosen.
    scope = _within({name: values for name, values in where.items() if name in _SCOPE}, "type", REQUESTS)

    def offered(by: str) -> list[str]:
        rows, _ = store.sums(since, until, (by,), scope, limit=MOST_OPTIONS + 1)
        return sorted([row[by] for row in rows if row[by]][:MOST_OPTIONS])

    return summary | {
        "series": [{"key": key, **counted} for key, counted in series.items()],
        **lists,
        "by_category": [{"key": r["category"], "masked": int(r["masked"])} for r in kinds],
        "stopped_by_reason": [{"key": k, "count": n} for k, n in sorted(stops.items(), key=lambda kv: (-kv[1], kv[0]))],
        "options": {
            "groups": [{"id": group, "name": names.group(group) or group} for group in offered("group")],
            "projects": offered("project"),
            "agents": offered("agent"),
            "categories": offered("category"),
        },
        "others": others,
    }


def _or_none(key: str) -> str | None:
    return key or None


def _named(rows: Iterable[Mapping[str, Any]], by: str) -> int:
    """How many of ``rows`` name something: the store counts no empty key (no group, no project, no one) among the keys
    there were, so neither does a list among the keys it shows."""
    return sum(1 for row in rows if row[by])


def _some(rows: Iterable[Mapping[str, Any]], by: str) -> list[Mapping[str, Any]]:
    """The first ``MOST_OPTIONS`` of the rows the store ranked (read one more) that name someone: the requests or the
    decisions of no one take none of a list of people's places, and are in its ``others``."""
    return [row for row in rows if row[by]][:MOST_OPTIONS]


def _added(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """The sums of rows shown as one (two ids under one name)."""
    out: dict[str, int] = dict.fromkeys(SUMS, 0)
    for row in rows:
        for name in SUMS:
            out[name] += int(row[name])
    return out


def _approvers(
    store: EventStore,
    span: tuple[float, float],
    where: Mapping[str, Collection[str]],
    names: Names,
    people: People,
    totals: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The decisions E5 records (`approval`, `change`) by who made them, as people may be seen: the store ranks who
    decided most and gives the first ``MOST_OPTIONS``, then the decisions of those shown by type; and what the list
    leaves out (who else decided, and the decisions of no one shown)."""
    decisions = _within(where, "type", DECISIONS)
    rows, keys = store.sums(*span, ("decider",), decisions, limit=MOST_OPTIONS + 1)  # no one takes no place
    shown, ids = _people(_some(rows, "decider"), "decider", names, people)
    person_of = {row["decider"]: person for person, found in shown.items() for row in found}
    decided: dict[str, Counter[str]] = {person: Counter() for person in shown}
    if person_of:
        split, _ = store.sums(*span, ("decider", "type"), _within(decisions, "decider", person_of))
        for row in split:
            decided[person_of[row["decider"]]][row["type"]] += int(row["exchanges"])
    out = [{"key": person, "approvals": n["approval"], "changes": n["change"]} for person, n in decided.items()]
    out.sort(key=lambda r: (-(r["approvals"] + r["changes"]), r["key"]))
    approvals, changes = sum(totals["approvals"].values()), sum(totals["changes"].values())
    left = {"count": keys - ids, "approvals": approvals - sum(r["approvals"] for r in out),
            "changes": changes - sum(r["changes"] for r in out)}  # fmt: skip
    return out, left


def export_rows(
    store: EventStore, period: Period, filters: Filters, viewer: Viewer, names: Names, *, tz: tzinfo | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """The report's rows: one a day (or a week) for each group, kind, name (an API key's always, a person's when
    shown), project and agent, with requests, stopped ones, values masked and those of each kind; and the kinds of
    value, its columns. The log's rows of requests are read one at a time (``EventStore.each``: a cursor on the
    server, R2-8), and past ``MOST_ROWS`` rows the export stops reading and is refused (TooLarge, M3), the cursor
    closed then."""
    since, until = period.bounds(tz)
    tally = _Tally(filters.category)
    by = ("hour", "type", "group", "kind", "identity", "project", "agent")
    found = store.each(since, until, by=by, where=_where(filters, viewer) | {"type": set(REQUESTS)})
    try:
        for row in found:
            kind = row["kind"]
            name = names.key(row["identity"]) if kind == "key" else names.person(row["identity"], viewer.people) or ""
            tally.add((period.bucket(row["hour"], tz), row["group"], kind, name, row["project"], row["agent"]), row)
            if len(tally.rows) > MOST_ROWS:  # M3: two copies of millions of rows in an instance's memory, else
                raise TooLarge(f"an export holds {MOST_ROWS:,} rows at most: narrow the period or the filters")
    finally:
        if isinstance(found, Generator):
            found.close()  # its transaction ends now, never when the reader is collected
    kinds = sorted({k for counted in tally.rows.values() for k in counted["by_category"]})
    rows = []
    for (day, group, kind, name, project, agent), counted in sorted(tally.rows.items(), key=lambda item: item[0]):
        rows.append(
            {
                "day": day,
                "group": names.group(group) or "",
                "kind": kind,
                "name": name,
                "project": project,
                "agent": agent,
                "exchanges": counted["exchanges"],
                "stopped": counted["stopped"],
                "masked": counted["masked"],
                "by_category": dict(counted["by_category"]),
            }
        )
    return rows, kinds


def _cell(value: object) -> str:
    """A text cell a spreadsheet opens as text: one that starts like a formula gets a quote first."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA) else text


def to_csv(rows: list[dict[str, Any]], kinds: list[str]) -> str:
    """The rows as CSV: a BOM first (so that Excel reads UTF-8), and a column for each kind of value."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    header = ["day", "group", "kind", "name", "project", "agent", "exchanges", "stopped", "masked"]
    writer.writerow([*header, *map(_cell, kinds)])
    for row in rows:
        texts = [row["day"], row["group"], row["kind"], row["name"], row["project"], row["agent"]]
        counts = [row["exchanges"], row["stopped"], row["masked"], *(row["by_category"].get(kind, 0) for kind in kinds)]
        writer.writerow([*map(_cell, texts), *counts])
    return chr(0xFEFF) + out.getvalue()


def export_file(
    store: EventStore,
    period: Period,
    filters: Filters,
    viewer: Viewer,
    names: Names,
    fmt: str,
    *,
    tz: tzinfo | None = None,
    administration: list[dict[str, Any]] | None = None,
    administration_truncated: bool = False,
) -> tuple[str, str, str]:
    """A report as a file (``fmt``: csv, else JSON, which holds ``administration`` when given): its name, media type
    and text. The administration log is there whole or not at all (I11): past ``MOST_ADMINISTRATION`` entries, or given
    as cut (``administration_truncated``), it is left out and the file says so."""
    rows, kinds = export_rows(store, period, filters, viewer, names, tz=tz)
    stem = f"latet-report-{period.start.isoformat()}-{period.end.isoformat()}"
    if fmt == "csv":
        return f"{stem}.csv", "text/csv", to_csv(rows, kinds)
    summary, _ = _summary(store, period, filters, viewer, tz=tz)  # its totals, read once: no ranked list
    body = {key: summary[key] for key in ("period", "filters", "people", "totals", "before")}
    body |= {"categories": kinds, "rows": rows}
    cut = administration_truncated or (administration is not None and len(administration) > MOST_ADMINISTRATION)
    if cut:  # more entries than a report's file holds: the Audit page's export has them all
        body["administration_truncated"] = True
    elif administration is not None:
        body["administration"] = administration
    return f"{stem}.json", "application/json", json.dumps(body, indent=1, ensure_ascii=False)


def _actor(actor: str, names: Names) -> dict[str, Any]:
    """Who acted, as the Audit page shows them (R5): an identity (a person, or an API key) by its name and kind, null
    both once it is no longer known; an administration token (`admin:<id>`), SCIM's token (`scim:<id>`, E3), the
    server's own machine (`server`, E0c and E1) and a call refused before its token was known (`unknown`, E0b) by what
    they are. Anything else (a row written by hand) is looked up as nothing (M9)."""
    if actor.startswith("admin:"):
        return {"id": actor, "name": None, "kind": "token"}
    if actor.startswith("scim:"):
        return {"id": actor, "name": None, "kind": "scim"}
    if actor in ("server", "unknown"):
        return {"id": actor, "name": None, "kind": actor}
    if not actor.startswith("u_"):
        return {"id": actor, "name": None, "kind": None}
    return {"id": actor, "name": names.identities.get(actor), "kind": names.kinds.get(actor)}


def _time(seconds: object) -> str | None:
    """An entry's time as events write it; None for a time no clock gives (a row written by hand), which never keeps
    the page from showing the log."""
    if not isinstance(seconds, int | float) or isinstance(seconds, bool) or not math.isfinite(seconds):
        return None
    try:
        return iso(seconds)
    except (ValueError, OverflowError, OSError):
        return None


def audit_rows(entries: Iterable[Entry], names: Names, *, addresses: bool) -> list[dict[str, Any]]:
    """The administration log's entries as the Audit page and its export show them, oldest first: who acted and on
    what, by name; the action; its details (settings' names and counts: no part records a value); and from where, for
    an administrator alone (``addresses``, which every caller says: Decision 1 (d), I12). Every entry, for whoever reads
    the log (`audit.read`): who acted is named whatever `reports.people` says."""
    out = []
    for entry in sorted(entries, key=lambda e: e.id):
        details = entry.details
        out.append(
            {
                "id": entry.id,
                "ts": _time(entry.ts),
                "action": entry.action,
                "actor": _actor(str(entry.actor), names),
                "target": {"id": entry.target, "name": names.target(entry.target)} if entry.target else None,
                "details": dict(details) if isinstance(details, Mapping) else {},
                "address": entry.address if addresses else None,
            }
        )
    return out


def audit_file(rows: Sequence[Mapping[str, Any]], period: Period, fmt: str) -> tuple[str, str, str]:
    """The administration log as a file (``fmt``: csv, else JSON): its name, media type and text."""
    stem = f"latet-audit-{period.start.isoformat()}-{period.end.isoformat()}"
    if fmt != "csv":
        body = {"period": {"from": period.start.isoformat(), "to": period.end.isoformat()}, "entries": list(rows)}
        return f"{stem}.json", "application/json", json.dumps(body, indent=1, ensure_ascii=False)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["time", "actor", "actor_name", "action", "target", "target_name", "details", "address"])
    for row in rows:
        actor, target = row["actor"], row["target"] or {}
        details = json.dumps(row["details"], ensure_ascii=False, sort_keys=True) if row["details"] else ""
        texts = [row["ts"], actor["id"], actor["name"], row["action"], target.get("id"), target.get("name"), details,
                 row["address"]]  # fmt: skip
        writer.writerow([_cell(text) for text in texts])
    return f"{stem}.csv", "text/csv", chr(0xFEFF) + out.getvalue()
