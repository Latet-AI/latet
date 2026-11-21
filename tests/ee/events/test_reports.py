"""Reports: requests, values and stops by group, project, agent, API key and kind of value, with the period before; by
the week when the period is long; a group lead sees their groups; people as the company chose; dates said wrong in
plain words; an export a spreadsheet opens as it is; and the administration log by name, its CSV safe too."""

import csv
import io
import json
import uuid
from dataclasses import astuple
from datetime import UTC, date, timedelta, timezone

import pytest

from latet.ee.events import reports as reports_module
from latet.ee.events.reports import (
    Filters,
    Names,
    NotAllowed,
    Period,
    ReportError,
    TooLarge,
    Viewer,
    audit_file,
    audit_rows,
    build_report,
    export_file,
    export_rows,
    reads_audit,
    report_viewer,
)
from latet.ee.events.schema import checked
from latet.ee.events.store import MemoryEvents
from latet.ee.roles import Role
from latet.events import make_event

from .fakes import Entry

DAY = 1_789_948_800.0  # 2026-09-21T00:00:00Z, a Monday
WEEK = Period(date(2026, 9, 21), date(2026, 9, 27))
EMILY, JAMES, CI = "u_emilycarte", "u_jameswilso", "u_billingci2"
NAMES = Names(groups={"g_paymentsxx": "Payments", "g_platformxx": "Platform"},
              identities={EMILY: "Emily Carter", JAMES: "James Wilson", CI: "Billing CI"},
              kinds={EMILY: "person", JAMES: "person", CI: "key"})  # fmt: skip
EVERYTHING = Viewer(None, "group-only", "edit")
LEAD = Viewer(frozenset({"g_paymentsxx"}), "named", None)
TAB, CR, LF = chr(9), chr(13), chr(10)


def _add(store, event_type, at, /, **fields):
    """An event of ``event_type`` at ``at`` written to ``store`` (positional: ``kind`` is a field, person or key)."""
    store.write(uuid.uuid4().hex, [checked(make_event(event_type, clock=lambda: at, **fields))], at=at)


def _csv(text):
    return list(csv.DictReader(io.StringIO(text.lstrip(chr(0xFEFF)))))


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.server)])
def empty(request):
    """An event log with nothing in it: in memory, and in PostgreSQL (marker `server`)."""
    if request.param == "memory":
        return MemoryEvents()
    from latet.ee.events.pg import PgEvents

    return PgEvents(request.getfixturevalue("database"))


@pytest.fixture
def store(empty):
    events = empty
    payments = {
        "identity": EMILY,
        "kind": "person",
        "group": "g_paymentsxx",
        "agent": "claude-code",
        "project": "billing-api",
    }
    _add(events, "exchange", DAY + 3600, masked={"EMAIL": 2, "IBAN": 1}, **payments)
    _add(events, "exchange", DAY + 7200, identity=JAMES, kind="person", group="g_platformxx", agent="codex",
         project="web-shop", masked={"EMAIL": 1})  # fmt: skip
    _add(events, "stopped", DAY + 86400 + 60, stopped=1, reason="leak", **payments)
    _add(events, "exchange", DAY + 90000, identity=CI, kind="key", agent="other", project="billing-api",
         masked={"AWS_ACCESS_KEY": 1})  # fmt: skip
    _add(events, "approval", DAY + 90000, outcome="approved", masked={"PERSON": 1}, by=JAMES, **payments)  # E5's
    _add(events, "change", DAY + 90000, identity=JAMES, kind="person", group="g_platformxx", outcome="rejected",
         reason="command", rule="allow_values", by=JAMES)  # fmt: skip
    _add(events, "exchange", DAY - 5 * 86400, masked={"EMAIL": 4}, **payments)  # the week before
    return events


def test_a_report_counts_requests_values_and_stops_by_group_project_agent_and_api_key(store):
    report = build_report(store, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    totals = report["totals"]
    assert (totals["exchanges"], totals["stopped"], totals["masked"]) == (4, 1, 5)
    assert (totals["approvals"], totals["changes"], totals["uncounted"]) == ({"approved": 1}, {"rejected": 1}, 0)
    assert report["before"] == {"exchanges": 1, "stopped": 0, "masked": 4}
    groups = {r["label"]: (r["exchanges"], r["stopped"], r["masked"]) for r in report["by_group"]}
    assert groups == {"Payments": (2, 1, 3), "Platform": (1, 0, 1), None: (1, 0, 1)}  # the API key has no group
    assert [(r["label"], r["exchanges"]) for r in report["by_key"]] == [("Billing CI", 1)]
    assert [r["key"] for r in report["by_project"]] == ["billing-api", "web-shop"]
    assert [r["key"] for r in report["by_agent"]] == ["claude-code", "codex", "other"]
    assert report["by_category"] == [
        {"key": "EMAIL", "masked": 3}, {"key": "AWS_ACCESS_KEY", "masked": 1}, {"key": "IBAN", "masked": 1},
    ]  # fmt: skip
    assert report["stopped_by_reason"] == [{"key": "leak", "count": 1}]
    days = [(s["key"], s["exchanges"], s["stopped"], s["masked"]) for s in report["series"]]
    assert days[:3] == [("2026-09-21", 2, 0, 4), ("2026-09-22", 2, 1, 1), ("2026-09-23", 0, 0, 0)] and len(days) == 7
    assert report["by_person"] is None and report["by_approver"] is None and report["period"]["step"] == "day"
    nothing = {"count": 0, "exchanges": 0, "stopped": 0, "masked": 0}  # every key shown: nothing left out
    assert report["others"] == dict.fromkeys(("by_group", "by_project", "by_agent", "by_key"), nothing) | {
        "by_person": None, "by_approver": None,
    }  # fmt: skip


def test_an_api_keys_applications_api_calls_are_among_its_requests_by_operation():
    events = MemoryEvents()
    key = {"identity": CI, "kind": "key", "agent": "apps", "rule": "t_k3yfortest"}
    _add(events, "apps", DAY + 60, outcome="mask", masked={"EMAIL": 2}, **key)
    _add(events, "apps", DAY + 120, outcome="scan", **key)  # a scan masks nothing: the call alone
    _add(events, "exchange", DAY + 180, identity=CI, kind="key", agent="other", masked={"EMAIL": 1})
    report = build_report(events, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    assert report["totals"]["exchanges"] == 3 and report["totals"]["apps"] == {"mask": 1, "scan": 1}
    assert [(r["label"], r["exchanges"], r["masked"]) for r in report["by_key"]] == [("Billing CI", 3, 3)]
    assert [r["key"] for r in report["by_agent"]] == ["apps", "other"] and report["by_group"][0]["label"] is None


def test_a_kind_of_value_narrows_the_report_to_the_requests_that_masked_one(store):
    report = build_report(store, WEEK, Filters(category="IBAN"), EVERYTHING, NAMES, tz=UTC)
    assert (report["totals"]["exchanges"], report["totals"]["masked"]) == (1, 1)
    assert report["by_category"] == [{"key": "IBAN", "masked": 1}]


def test_a_long_period_is_shown_by_the_week(store):
    report = build_report(store, Period(date(2026, 7, 1), date(2026, 10, 31)), Filters(), EVERYTHING, NAMES, tz=UTC)
    assert report["period"]["step"] == "week" and report["series"][0]["key"] == "2026-06-29"  # its first Monday
    assert [s["key"] for s in report["series"] if s["exchanges"]] == ["2026-09-14", "2026-09-21"]


def test_a_group_lead_sees_the_groups_they_belong_to_only(store):
    lead = Viewer(frozenset({"g_paymentsxx"}), "group-only", None)
    report = build_report(store, WEEK, Filters(), lead, NAMES, tz=UTC)
    assert [r["label"] for r in report["by_group"]] == ["Payments"] and report["totals"]["exchanges"] == 2
    assert report["by_key"] == [] and [g["id"] for g in report["options"]["groups"]] == ["g_paymentsxx"]
    assert report["others"]["by_key"] == {"count": 0, "exchanges": 0, "stopped": 0, "masked": 0}
    totals = report["totals"]  # the decisions and stops of their groups alone: Platform's change is none of theirs
    assert (totals["approvals"], totals["changes"], report["stopped_by_reason"]) == ({"approved": 1}, {}, [
        {"key": "leak", "count": 1}])  # fmt: skip
    with pytest.raises(NotAllowed):
        build_report(store, WEEK, Filters(group="g_platformxx"), lead, NAMES, tz=UTC)


def test_a_group_leads_groups_hold_in_the_export_and_in_what_the_filters_offer(store):
    rows, kinds = export_rows(store, WEEK, Filters(), LEAD, NAMES, tz=UTC)
    assert {(row["group"], row["name"]) for row in rows} == {("Payments", "Emily Carter")} and kinds == [
        "EMAIL",
        "IBAN",
    ]
    options = build_report(store, WEEK, Filters(), LEAD, NAMES, tz=UTC)["options"]
    assert (options["projects"], options["agents"], options["categories"]) == (["billing-api"], ["claude-code"],
                                                                               ["EMAIL", "IBAN"])  # fmt: skip
    with pytest.raises(NotAllowed):
        export_rows(store, WEEK, Filters(group="g_platformxx"), LEAD, NAMES, tz=UTC)
    with pytest.raises(NotAllowed):
        export_file(store, WEEK, Filters(group="g_platformxx"), LEAD, NAMES, "json", tz=UTC)


def test_an_api_key_never_reaches_a_group_lead_even_one_counted_in_their_group(store):
    """An API key is a program in no group (R8): one whose event carried a group all the same is still none of a
    group lead's, in the report, its export and what its filters offer."""
    _add(store, "exchange", DAY + 3600, identity=CI, kind="key", group="g_paymentsxx", agent="ci-bot", project="ledger",
         masked={"IBAN": 7})  # fmt: skip
    report = build_report(store, WEEK, Filters(), LEAD, NAMES, tz=UTC)
    assert report["by_key"] == [] and (report["totals"]["exchanges"], report["totals"]["masked"]) == (2, 3)
    assert [r["key"] for r in report["by_person"]] == ["Emily Carter"]
    assert "ci-bot" not in report["options"]["agents"] and "ledger" not in report["options"]["projects"]
    rows, _ = export_rows(store, WEEK, Filters(), LEAD, NAMES, tz=UTC)
    assert {row["kind"] for row in rows} == {"person"} and "Billing CI" not in {row["name"] for row in rows}


# The store keeps its people by id, as under `named`: `pseudonymous` shows none of those rows (L1 of the final security
# review), and a person kept by a pseudonym by it (test_rows_kept_by_id_while_named_show_no_person_under_pseudonymous).
@pytest.mark.parametrize(
    ("people", "shown"),
    [("group-only", None), ("pseudonymous", set()), ("named", {"Emily Carter", "James Wilson"})],
)
def test_people_appear_as_the_company_chose_and_api_keys_never_among_them(store, people, shown):
    persons = build_report(store, WEEK, Filters(), Viewer(None, people, "edit"), NAMES, tz=UTC)["by_person"]
    assert (None if persons is None else {r["key"] for r in persons}) == shown


@pytest.mark.parametrize(
    ("people", "shown"),
    [("group-only", {""}), ("pseudonymous", {""}), ("named", {"Emily Carter", "James Wilson"})],
)
def test_an_export_names_people_as_the_company_chose_and_api_keys_always(store, people, shown):
    rows, _ = export_rows(store, WEEK, Filters(), Viewer(None, people, "edit"), NAMES, tz=UTC)
    assert {row["name"] for row in rows if row["kind"] == "person"} == shown
    assert {row["name"] for row in rows if row["kind"] == "key"} == {"Billing CI"}


def test_by_group_only_an_export_holds_no_row_of_one_person():
    events = MemoryEvents()
    for identity in (EMILY, JAMES):
        _add(events, "exchange", DAY + 60, identity=identity, kind="person", group="g_paymentsxx", agent="codex",
             project="web-shop")  # fmt: skip
    rows, _ = export_rows(events, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    assert [(row["name"], row["exchanges"]) for row in rows] == [("", 2)]


def test_a_pseudonym_is_never_an_id_left_as_it_is():
    """With `pseudonymous`, a person kept by their id (before the switch) is shown as no one, never by the id nor by a
    pseudonym made for it (L1 of the final security review); one kept by a pseudonym is shown by it."""
    assert Names().person(EMILY, "pseudonymous") is None and Names().person("p_abcdefghij", "pseudonymous") == (
        "p_abcdefghij"
    )
    assert NAMES.person(EMILY, "pseudonymous") is None and NAMES.person(EMILY, "group-only") is None


def test_rows_kept_by_id_while_named_show_no_person_under_pseudonymous():
    """L1 of the final security review: rows kept by a person's id while `named` was in force are never shown under
    the pseudonym that person's new events carry, which would let whoever saw the named report follow them after the
    switch. They count among the others, as `group-only` keeps them; a row kept by its pseudonym is shown by it."""
    events = MemoryEvents()
    _add(events, "exchange", DAY + 60, identity=EMILY, kind="person", group="g_paymentsxx", project="falcon",
         masked={"EMAIL": 1})  # kept while `named` was in force  # fmt: skip
    _add(events, "change", DAY + 60, identity=JAMES, kind="person", outcome="approved", reason="dashboard", by=EMILY)
    kept = make_event("exchange", clock=lambda: DAY + 120, identity=EMILY, kind="person", group="g_paymentsxx")
    events.write(uuid.uuid4().hex, [checked(kept | {"identity": "p_abcdefghij"})], at=DAY + 120)  # kept after it
    pseudonymous = Viewer(None, "pseudonymous", "edit")
    names = _bare_api()._names()  # how the routes name what a report shows
    report = build_report(events, WEEK, Filters(), pseudonymous, names, tz=UTC)
    assert [(r["key"], r["exchanges"]) for r in report["by_person"]] == [("p_abcdefghij", 1)]
    assert report["others"]["by_person"]["count"] == 1 and report["others"]["by_person"]["exchanges"] == 1
    assert report["by_approver"] == [] and report["others"]["by_approver"] == {"count": 1, "approvals": 0,
                                                                              "changes": 1}  # fmt: skip
    rows, _ = export_rows(events, WEEK, Filters(), pseudonymous, names, tz=UTC)
    assert sorted(row["name"] for row in rows) == ["", "p_abcdefghij"]
    assert names.person(EMILY, "pseudonymous") is None and names.person("p_abcdefghij", "pseudonymous") == (
        "p_abcdefghij"
    )


def _bare_api():
    """An EventsApi that names what a report shows as the routes do."""
    from types import SimpleNamespace

    from latet.ee.events.api import EventsApi

    api = EventsApi.__new__(EventsApi)
    identities = SimpleNamespace(get=lambda i: SimpleNamespace(name=NAMES.identities.get(i), kind="person"))
    api.__dict__.update(server=SimpleNamespace(identities=identities), people=SimpleNamespace(group=lambda g: None))
    return api


def test_people_kept_before_the_company_chose_group_only_are_shown_no_more():
    """A person kept by a pseudonym or by their id, before `reports.people` became `group-only`, is in no row of a
    report or an export once it is: what was kept stays as it was, what is shown is what the company chooses now."""
    events = MemoryEvents()
    emily = make_event(
        "exchange", clock=lambda: DAY + 60, identity=EMILY, kind="person", group="g_paymentsxx", agent="codex"
    )
    kept = checked(emily | {"identity": "p_abcdefghij"})  # as the pipeline keeps a person under `pseudonymous`
    events.write(uuid.uuid4().hex, [kept, checked(emily | {"id": uuid.uuid4().hex})], at=DAY + 60)
    rows, _ = export_rows(events, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    assert [(row["name"], row["exchanges"]) for row in rows] == [("", 2)]
    assert build_report(events, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)["by_person"] is None
    named, _ = export_rows(events, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, tz=UTC)
    assert sorted(row["name"] for row in named) == ["Emily Carter", "p_abcdefghij"]


def test_decisions_appear_by_who_decided_as_people_do(store):
    named = build_report(store, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, tz=UTC)
    assert named["by_approver"] == [{"key": "James Wilson", "approvals": 1, "changes": 1}]
    assert named["others"]["by_approver"] == {"count": 0, "approvals": 0, "changes": 0}


def test_who_sees_what():
    def viewer(*roles, member_of=()):
        return report_viewer(roles=set(roles), member_of=member_of, people="named")

    assert viewer(Role.USER, Role.SECURITY) == Viewer(None, "named", "edit")
    assert viewer(Role.USER, Role.AUDITOR) == Viewer(None, "named", "read")
    assert viewer(Role.USER, Role.GROUP_LEAD, member_of=["g_paymentsxx"]) == Viewer(
        frozenset({"g_paymentsxx"}), "named", None
    )
    assert viewer(Role.USER, Role.GROUP_LEAD) is None and viewer(Role.USER) is None and viewer(Role.ADMIN) is None
    assert all(reads_audit({role}) for role in (Role.ADMIN, Role.AUDITOR, Role.SECURITY))
    assert not reads_audit({Role.GROUP_LEAD}) and not reads_audit({Role.USER})


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        ("2026-10-01", "2026-09-01", "ends before"),
        ("2025-01-01", "2026-06-01", "366 days"),
        ("1 Oct", None, "YYYY-MM-DD"),
    ],
)
def test_dates_said_wrong_are_refused_in_plain_words(start, end, why):
    with pytest.raises(ReportError, match=why):
        Period.parse(start, end, date(2026, 10, 12))
    assert Period.parse(None, None, date(2026, 10, 12)) == Period(date(2026, 9, 13), date(2026, 10, 12))


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [("2026-W40-1", None, "YYYY-MM-DD"), ("20261001", None, "YYYY-MM-DD"), ("1999-12-31", "2000-01-02", "2000-01-01"),
     ("2999-12-30", "3000-01-01", "2999-12-31"), (None, "2000-01-10", "2000-01-01"), ("0001-01-01", None, "2000")],
)  # fmt: skip
def test_dates_no_report_reads_are_refused_in_plain_words_never_an_error_of_the_clock(start, end, why):
    with pytest.raises(ReportError, match=why):
        Period.parse(start, end, date(2026, 10, 12))


def test_the_csv_opens_in_a_spreadsheet_as_it_is():
    events = MemoryEvents()
    _add(events, "exchange", DAY + 60, group="g_paymentsxx", agent="codex", project='=HYPERLINK("evil.example")',
         masked={"EMAIL": 2})  # fmt: skip
    _add(events, "exchange", DAY + 120, group="g_platformxx", agent="codex",
         project='Citt\N{LATIN SMALL LETTER A WITH GRAVE}, "nord"', masked={"IBAN": 1})  # fmt: skip
    names = Names(groups={"g_paymentsxx": "+Payments", "g_platformxx": "Platform" + LF + "EU"})
    name, kind, text = export_file(events, WEEK, Filters(), EVERYTHING, names, "csv", tz=UTC)
    assert (name, kind) == ("latet-report-2026-09-21-2026-09-27.csv", "text/csv")
    assert text.startswith(chr(0xFEFF) + "day,group,kind,name,project,agent,exchanges,stopped,masked,EMAIL,IBAN")
    assert [(r["group"], r["project"], r["EMAIL"], r["IBAN"]) for r in _csv(text)] == [
        ("'+Payments", "'" + '=HYPERLINK("evil.example")', "2", "0"),
        ("Platform" + LF + "EU", 'Citt\N{LATIN SMALL LETTER A WITH GRAVE}, "nord"', "0", "1"),
    ]


@pytest.mark.parametrize("start", ["=", "+", "-", "@", TAB, CR])
def test_every_start_of_a_formula_is_quoted_in_both_csvs(start):
    events = MemoryEvents()
    _add(events, "exchange", DAY + 60, identity=CI, kind="key", group="g_paymentsxx", agent="codex")
    names = Names(groups={"g_paymentsxx": start + "1+1"}, identities={EMILY: start + "Emily", CI: start + "CI"})
    [row] = _csv(export_file(events, WEEK, Filters(), EVERYTHING, names, "csv", tz=UTC)[2])
    assert (row["group"], row["name"]) == ("'" + start + "1+1", "'" + start + "CI")
    rows = audit_rows([Entry(1, DAY, EMILY, "person.added", EMILY, {}, start + "1")], names, addresses=True)
    [line] = _csv(audit_file(rows, WEEK, "csv")[2])
    assert line["actor_name"] == line["target_name"] == "'" + start + "Emily" and line["address"] == "'" + start + "1"


def test_the_json_export_holds_the_rows_and_the_administration_log_only_when_given(store):
    administration = [{"id": 1, "action": "person.added"}]
    name, kind, text = export_file(store, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, "json", tz=UTC,
                                   administration=administration)  # fmt: skip
    body = json.loads(text)
    assert (name, kind) == ("latet-report-2026-09-21-2026-09-27.json", "application/json")
    assert body["totals"]["exchanges"] == 4 and body["categories"] == ["AWS_ACCESS_KEY", "EMAIL", "IBAN"]
    assert {(row["kind"], row["name"]) for row in body["rows"]} == {
        ("person", "Emily Carter"), ("person", "James Wilson"), ("key", "Billing CI"),
    }  # fmt: skip
    assert body["administration"] == administration
    assert "administration" not in json.loads(export_file(store, WEEK, Filters(), EVERYTHING, NAMES, "json", tz=UTC)[2])
    cut = json.loads(export_file(store, WEEK, Filters(), EVERYTHING, NAMES, "json", tz=UTC,
                                 administration_truncated=True)[2])  # fmt: skip
    assert cut["administration_truncated"] is True and "administration" not in cut  # said, never cut silently (I11)


def test_a_reports_json_export_never_holds_part_of_the_administration_log_as_if_whole(store, monkeypatch):
    """I11: past its most the log is left out of the file and the file says so; given cut, it is left out too."""
    monkeypatch.setattr(reports_module, "MOST_ADMINISTRATION", 1)
    many = json.loads(export_file(store, WEEK, Filters(), EVERYTHING, NAMES, "json", tz=UTC,
                                  administration=[{"id": 1}, {"id": 2}])[2])  # fmt: skip
    assert many["administration_truncated"] is True and "administration" not in many
    cut = json.loads(export_file(store, WEEK, Filters(), EVERYTHING, NAMES, "json", tz=UTC,
                                 administration=[{"id": 2}], administration_truncated=True)[2])  # fmt: skip
    assert cut["administration_truncated"] is True and "administration" not in cut


def test_an_export_past_its_rows_is_refused_never_cut(store, monkeypatch):
    monkeypatch.setattr(reports_module, "MOST_ROWS", 2)
    with pytest.raises(TooLarge, match="narrow the period"):
        export_rows(store, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, tz=UTC)  # M3


class _Counted:
    """An event log that counts the rows an export reads of it, and refuses to give them all at once (R2-8)."""

    def __init__(self, store):
        self.store, self.read, self.closed = store, 0, False

    def query(self, *args, **kwargs):
        raise AssertionError("an export reads the log one row at a time")

    def each(self, *args, **kwargs):
        try:
            for row in self.store.each(*args, **kwargs):
                self.read += 1
                yield row
        finally:
            self.closed = True


def test_an_export_reads_the_requests_rows_one_at_a_time_and_stops_past_its_most(empty, monkeypatch):
    """M3, R2-8: an export never holds more rows than its most before it weighs them, and reads only the rows of
    requests (the decisions, sign-ins and changes of the period are none of its rows)."""
    for n in range(4):
        _add(
            empty,
            "approval",
            DAY + 60,
            identity=EMILY,
            kind="person",
            group="g_paymentsxx",
            outcome="approved",
            by=JAMES,
        )
        _add(empty, "sign-in", DAY + 60 + n, identity=JAMES, kind="person", group="g_platformxx", outcome="ok")
    for n in range(5):
        _add(empty, "exchange", DAY + 60, identity=EMILY, kind="person", group="g_paymentsxx", project=f"p{n}")
    monkeypatch.setattr(reports_module, "MOST_ROWS", 2)
    counted = _Counted(empty)
    with pytest.raises(TooLarge) as refused:
        export_rows(counted, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    # Closed by the export itself, while its refusal (which holds the export's frame) is still held: its transaction ends
    # then, never when the reader is collected.
    assert counted.read == 3 and counted.closed and refused.value is not None


def _projects(store, n, at=DAY + 60):
    """``n`` projects of one request each, which masked a value, in one batch: with no identity (a group's bound, a
    thousand an hour), as a client naming a new folder at each request makes them."""
    made = [make_event("exchange", clock=lambda: at, group="g_paymentsxx", agent="codex", project=f"p{k:03d}",
                       masked={"EMAIL": 1}) for k in range(n)]  # fmt: skip
    store.write(uuid.uuid4().hex, [checked(event) for event in made], at=at)


def test_the_filters_offer_the_two_hundred_choices_of_each_most_asked_for(empty):
    _projects(empty, 250)
    _add(empty, "exchange", DAY + 60, group="g_paymentsxx", agent="codex", project="p249")  # asked for more: offered
    projects = build_report(empty, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)["options"]["projects"]
    assert len(projects) == 200 and projects == [*(f"p{k:03d}" for k in range(199)), "p249"]  # M6, in their order


def test_a_ranked_list_shows_two_hundred_and_says_what_it_leaves_out(empty):
    _projects(empty, 250)
    _add(empty, "stopped", DAY + 60, group="g_paymentsxx", agent="codex", project="p249", stopped=1, reason="leak")
    report = build_report(empty, WEEK, Filters(), EVERYTHING, NAMES, tz=UTC)
    shown = report["by_project"]
    assert len(shown) == 200 and [(r["key"], r["exchanges"], r["stopped"], r["masked"]) for r in shown[:2]] == [
        ("p249", 2, 1, 1), ("p000", 1, 0, 1),
    ]  # fmt: skip
    assert [r["key"] for r in shown[1:]] == [f"p{k:03d}" for k in range(199)]
    assert report["others"]["by_project"] == {"count": 50, "exchanges": 50, "stopped": 0, "masked": 50}
    assert report["others"]["by_agent"] == {"count": 0, "exchanges": 0, "stopped": 0, "masked": 0}
    assert sum(r["exchanges"] for r in shown) + 50 == report["totals"]["exchanges"] == 251


class _Measured:
    """An event log that counts the rows each read gives a report, and refuses the reads that give every row."""

    def __init__(self, store, *, each=False):
        self.store, self.loaded, self.calls, self.allows_each = store, 0, [], each

    def query(self, *args, **kwargs):
        raise AssertionError("a report reads what the store counted and ranked, never every row")

    def each(self, *args, **kwargs):
        if not self.allows_each:
            raise AssertionError("a report reads what the store counted and ranked, never every row")
        return self.store.each(*args, **kwargs)

    def sums(self, since, until, by=(), where=None, *, category=None, limit=None):
        rows, keys = self.store.sums(since, until, by, where, category=category, limit=limit)
        self.loaded += len(rows)
        self.calls.append((tuple(by), limit))
        return rows, keys


def test_a_report_loads_no_more_rows_however_many_the_period_holds(monkeypatch):
    """Its lists and the filters' choices are ranked in the store and only their first read: one client naming a
    folder at each request (M6) for a year makes a report no heavier (the review measured 21 s and 1 GB before)."""
    monkeypatch.setattr(reports_module, "MOST_OPTIONS", 5)
    loads = []
    for many in (10, 400):
        events = MemoryEvents()
        _projects(events, many)
        _add(events, "approval", DAY + 60, identity=EMILY, kind="person", group="g_paymentsxx", outcome="approved",
             by=JAMES)  # fmt: skip
        measured = _Measured(events)
        report = build_report(measured, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, tz=UTC)
        unbounded = {(), ("hour",), ("type", "code"), ("decider", "type")}  # one row; the period's hours; codes; shown
        assert all(limit is not None or by in unbounded for by, limit in measured.calls), measured.calls
        assert len(report["by_project"]) == 5 and report["others"]["by_project"]["count"] == many - 5
        loads.append(measured.loaded)
    assert loads[0] == loads[1]


def test_a_reports_json_export_reads_its_totals_once_and_no_list(store):
    measured = _Measured(store, each=True)
    body = json.loads(export_file(measured, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, "json", tz=UTC)[2])
    assert body["totals"]["exchanges"] == 4 and body["before"]["exchanges"] == 1
    assert sorted(measured.calls) == [((), None), ((), None), (("type", "code"), None)]


@pytest.mark.parametrize(
    ("filters", "why"),
    [(Filters(project="web" + chr(0)), "project"), (Filters(project="p" * 201), "project"),
     (Filters(project="a/b"), "project"), (Filters(agent="Claude Code"), "agent"), (Filters(group="Payments"), "group"),
     (Filters(group="g_payments"), "group"), (Filters(category="email"), "kind of value"),
     (Filters(category="E" * 65), "kind of value")],
)  # fmt: skip
def test_a_filter_is_checked_as_the_log_keeps_it_and_refused_in_fixed_words(filters, why):
    events = MemoryEvents()
    for asked in (build_report, export_rows):
        with pytest.raises(ReportError, match=why) as refused:
            asked(events, WEEK, filters, EVERYTHING, NAMES, tz=UTC)
        assert not any(given and given in str(refused.value) for given in astuple(filters))  # never what was given
    with pytest.raises(ReportError, match=why):  # a group lead too: a 400, before whose group it is
        build_report(events, WEEK, filters, LEAD, NAMES, tz=UTC)


def test_the_hours_of_a_half_hour_zone_are_labelled_within_the_period():
    """In a zone of half hours (India's, +05:30) a period starts within an hour the log counts whole: what it counts is
    the period's first day's, never a day the report does not show."""
    india = timezone(timedelta(hours=5, minutes=30))
    events = MemoryEvents()
    _add(events, "exchange", DAY - 5 * 3600 - 15 * 60, group="g_paymentsxx", masked={"EMAIL": 1})  # 00:15 on the 21st
    report = build_report(events, WEEK, Filters(), EVERYTHING, NAMES, tz=india)
    assert (report["series"][0]["key"], report["series"][0]["exchanges"]) == ("2026-09-21", 1)
    assert sum(s["exchanges"] for s in report["series"]) == report["totals"]["exchanges"] == 1
    assert [row["day"] for row in export_rows(events, WEEK, Filters(), EVERYTHING, NAMES, tz=india)[0]] == [
        "2026-09-21"
    ]
    weeks = Period(date(2026, 9, 21), date(2026, 12, 31))  # by the week, from that Monday: its hour starts on a Sunday
    long = build_report(events, weeks, Filters(), EVERYTHING, NAMES, tz=india)
    assert [(s["key"], s["exchanges"]) for s in long["series"] if s["exchanges"]] == [("2026-09-21", 1)]


def test_no_one_is_no_person_in_the_lists_of_people(empty, monkeypatch):
    """The requests kept by group only (before the company chose to show people) and a decision no one is named for
    are no person: they take none of a list's places, nor count among the people it leaves out, and their counts are
    in ``others`` all the same."""
    monkeypatch.setattr(reports_module, "MOST_OPTIONS", 1)
    for at in (DAY + 60, DAY + 120, DAY + 180):  # no one, three times: the first of the rows by requests
        _add(empty, "exchange", at, kind="person", group="g_paymentsxx", agent="codex")
        _add(empty, "approval", at, identity=EMILY, kind="person", group="g_paymentsxx", outcome="approved")
    _add(empty, "exchange", DAY + 240, identity=EMILY, kind="person", group="g_paymentsxx", agent="codex")
    _add(empty, "change", DAY + 240, identity=EMILY, kind="person", group="g_paymentsxx", outcome="kept", by=JAMES)
    report = build_report(empty, WEEK, Filters(), Viewer(None, "named", "edit"), NAMES, tz=UTC)
    assert [(r["key"], r["exchanges"]) for r in report["by_person"]] == [("Emily Carter", 1)]
    assert report["others"]["by_person"] == {"count": 0, "exchanges": 3, "stopped": 0, "masked": 0}
    assert report["by_approver"] == [{"key": "James Wilson", "approvals": 0, "changes": 1}]
    assert report["others"]["by_approver"] == {"count": 0, "approvals": 3, "changes": 0}


def test_two_ids_shown_under_one_name_are_one_row_named_after_the_limit(monkeypatch):
    """The store ranks ids, a report names them: two people of one name are one row, and what the limit left out is
    said."""
    monkeypatch.setattr(reports_module, "MOST_OPTIONS", 2)
    namesake = "u_emilycart2"  # another Emily Carter
    names = Names(identities={**NAMES.identities, namesake: "Emily Carter"}, kinds=NAMES.kinds)
    events = MemoryEvents()
    emily = make_event("exchange", clock=lambda: DAY + 60, identity=EMILY, kind="person", masked={"EMAIL": 1})
    james = make_event("exchange", clock=lambda: DAY + 60, identity=JAMES, kind="person")
    events.write(uuid.uuid4().hex, [checked(emily), checked(emily | {"id": uuid.uuid4().hex, "identity": namesake}),
                                    checked(james)], at=DAY + 60)  # fmt: skip
    _add(events, "exchange", DAY + 60, identity=CI, kind="key", masked={"IBAN": 3})  # a program's: no person's
    report = build_report(events, WEEK, Filters(), Viewer(None, "named", "edit"), names, tz=UTC)
    assert [(r["key"], r["exchanges"], r["masked"]) for r in report["by_person"]] == [("Emily Carter", 2, 2)]
    assert report["others"]["by_person"] == {"count": 1, "exchanges": 1, "stopped": 0, "masked": 0}  # James alone
    assert report["others"]["by_key"] == {"count": 0, "exchanges": 0, "stopped": 0, "masked": 0}


def test_the_administration_log_names_who_acted_and_on_what_oldest_first():
    names = Names(groups={"g_paymentsxx": "Payments"}, identities=NAMES.identities, kinds=NAMES.kinds)
    rows = audit_rows([Entry(2, DAY + 60, JAMES, "group.added", "g_paymentsxx", {"source": "manual"}, "10.0.0.8"),
                       Entry(1, DAY, "admin:3f9a", "person.added", EMILY, {}, "10.0.0.7"),
                       Entry(3, DAY + 90, JAMES, "siem.changed", "siem", {})], names, addresses=True)  # fmt: skip
    assert [(r["action"], r["actor"]["name"], r["actor"]["kind"], r["target"]["name"]) for r in rows] == [
        ("person.added", None, "token", "Emily Carter"), ("group.added", "James Wilson", "person", "Payments"),
        ("siem.changed", "James Wilson", "person", None),  # a target shaped as no id is looked up as nothing (M9)
    ]  # fmt: skip
    assert rows[0]["ts"] == "2026-09-21T00:00:00Z" and rows[1]["details"] == {"source": "manual"}
    assert [r["address"] for r in rows] == ["10.0.0.7", "10.0.0.8", None]


def test_the_addresses_are_an_administrators_to_read():
    rows = audit_rows([Entry(1, DAY, EMILY, "token.added", EMILY, {}, "10.0.4.31")], NAMES, addresses=False)
    assert rows[0]["address"] is None and rows[0]["actor"]["name"] == "Emily Carter"  # Decision 1 (d), I12
    [line] = _csv(audit_file(rows, WEEK, "csv")[2])
    assert line["address"] == "" and "10.0.4.31" not in audit_file(rows, WEEK, "json")[2]


def test_a_target_is_looked_up_as_a_group_only_when_it_is_shaped_as_a_groups_id():
    """E3 finds a group by its id or its name: a target such as `siem` is never looked up as a group of that name, nor
    an actor that is no identity's id as an identity (M9)."""
    names = Names(
        groups={"siem": "Security team", "g_paymentsxx": "Payments"}, identities={EMILY: "Emily", "bob": "Bob"}
    )
    assert [names.target(t) for t in ("siem", "g_paymentsxx", EMILY, "bob", None)] == [None, "Payments", "Emily", None,
                                                                                         None]  # fmt: skip
    [row] = audit_rows([Entry(1, DAY, "bob", "person.added", "siem")], names, addresses=True)
    assert (row["actor"], row["target"]) == ({"id": "bob", "name": None, "kind": None}, {"id": "siem", "name": None})


def test_a_groups_id_is_shaped_as_e3_makes_one():
    people = pytest.importorskip("latet.ee.people")
    assert reports_module._GROUP_ID.pattern == people.GROUP_ID.pattern


def test_every_actor_the_log_records_is_said_as_what_it_is():
    rows = audit_rows([Entry(1, DAY, "scim:s_k3yfortest", "person.disabled", EMILY),
                       Entry(2, DAY, "server", "server.migrated"), Entry(3, DAY, "unknown", "calls.refused"),
                       Entry(4, DAY, CI, "apps.unmasked", CI, {"token": "t_k3yfortest", "restored": 2}),
                       Entry(5, DAY, "u_removed000", "person.code.made")], NAMES, addresses=False)  # fmt: skip
    assert [(r["actor"]["kind"], r["actor"]["name"]) for r in rows] == [
        ("scim", None), ("server", None), ("unknown", None), ("key", "Billing CI"), (None, None),
    ]  # fmt: skip


def test_an_entry_written_by_hand_is_shown_all_the_same():
    """A time no clock gives, or details that are no mapping (a row written by hand), never keep the page from
    showing the log: `latet server log verify` says which entry does not hold."""
    rows = audit_rows([Entry(1, float("nan"), "server", "a"), Entry(2, 1e300, "server", "b"),
                       Entry(3, DAY, "server", "c", None, ["not", "a", "mapping"])], NAMES, addresses=True)  # fmt: skip
    assert [(r["ts"], r["details"]) for r in rows] == [(None, {}), (None, {}), ("2026-09-21T00:00:00Z", {})]
    assert len(_csv(audit_file(rows, WEEK, "csv")[2])) == 3


def test_the_administration_logs_csv_opens_in_a_spreadsheet_as_it_is():
    names = Names(identities={JAMES: "=cmd|' /C calc'!A0", EMILY: "@Emily"})
    rows = audit_rows(
        [Entry(1, DAY, JAMES, "person.added", EMILY, {"email": "set"}, "10.0.0.7")], names, addresses=True
    )
    name, kind, text = audit_file(rows, WEEK, "csv")
    assert (name, kind) == ("latet-audit-2026-09-21-2026-09-27.csv", "text/csv")
    [row] = _csv(text)
    assert (row["actor_name"], row["target_name"], row["details"]) == (
        "'=cmd|' /C calc'!A0",
        "'@Emily",
        '{"email": "set"}',
    )
    name, kind, text = audit_file(rows, WEEK, "json")
    assert (name, kind) == ("latet-audit-2026-09-21-2026-09-27.json", "application/json")
    assert json.loads(text)["entries"][0]["actor"]["name"] == "=cmd|' /C calc'!A0"  # JSON keeps it as it is
