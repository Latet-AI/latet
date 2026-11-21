"""A licensed server of two instances: a morning of Emily's, a program's request and its call of the applications API
add up in the report of the security role, by group and by API key, with no value in the log, the report, its exports or
the events the SIEM receives; the administration log is on the Audit page and reaches the SIEM with its chain and no
detail; a gap marker no entry of the log names is reported by `latet server log verify`; each person sees what their
role allows."""

import json
import time
from collections import Counter

import pytest

from latet.ee.events.store import gap_event
from latet.ee.roles import Role
from latet.events import FIELDS

from ..people.server_support import API, BY_HAND, SERVER_TESTS, add_person, as_admin, sign_in_as
from .day import forbidden, strings
from .receivers import HttpsReceiver, certificate, until
from .servers import APPS, AUTH, EMAIL, NOW_DAY, a_morning, api_key, drained, member, ready

pytestmark = [*SERVER_TESTS, pytest.mark.e2e]
EE = f"{API}/ee"
UI = {"x-latet-ui": "1"}  # the dashboard's header, which an export asks for as a change does
ALL = ("type", "identity", "kind", "group", "agent", "project", "code", "decider")
REQUESTS = ("exchange", "stopped", "apps")


def _today() -> dict[str, str]:  # the gateway's exchanges are timed by the clock of the world
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return {"from": day, "to": day}


@pytest.fixture
def receiver(tmp_path):
    cert, key = certificate(tmp_path)
    found = HttpsReceiver(cert, key, tls=False).start()
    yield found
    found.stop()


def test_a_morning_and_a_programs_work_add_up_by_group_and_api_key_and_hold_no_value(
    cluster, upstream, receiver, tmp_path
):
    ready(cluster, upstream, receiver=receiver)
    emily = member(cluster, "Emily Carter", EMAIL, Role.SECURITY, group="Payments")
    ci = api_key(cluster, "Billing CI")
    client, server = cluster.a, cluster.server
    a_morning(client, emily, tmp_path)
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"build the release for {EMAIL}"}]}
    assert client.post(f"{ci.route}/llm/v1/chat/completions", json=body, headers=AUTH).status_code == 200
    masked = client.post(
        f"{APPS}/mask", json={"texts": [f"to {EMAIL}"]}, headers={"authorization": f"Bearer {ci.secret}"}
    )
    assert masked.status_code == 200
    runtimes = [drained(client, server, emily), drained(client, server, ci)]
    assert server.edition.events.pipeline.flush()
    sign_in_as(cluster, emily.identity)
    report = client.get(f"{EE}/reports", params=_today()).json()
    exports = [client.get(f"{EE}/reports/export", headers=UI, params=_today() | {"format": fmt}).json()["file"]
               for fmt in ("csv", "json")]  # fmt: skip
    assert until(lambda: sum(e["type"] in REQUESTS for e in receiver.events()) == 7, timeout=30)
    rows = server.edition.events.store.query(0, time.time() + 3600, by=ALL)
    summaries = [summary for rt in runtimes for summary in rt.exchanges.summaries_since(0)]
    expected: Counter = Counter({"EMAIL": 1})  # the applications API's mask
    for summary in summaries:
        expected.update(summary["new_by_type"])
    assert report["totals"]["exchanges"] == len(summaries) + 1 == 7 and report["totals"]["stopped"] == 1
    assert report["totals"]["apps"] == {"mask": 1}
    assert {r["key"]: r["masked"] for r in report["by_category"]} == dict(+expected)
    assert {r["label"]: r["exchanges"] for r in report["by_group"]} == {"Payments": 5, None: 2}
    assert [(r["label"], r["exchanges"]) for r in report["by_key"]] == [("Billing CI", 2)]
    assert report["by_person"] is None  # group-only, the default
    bad = forbidden(runtimes[0], tmp_path) | forbidden(runtimes[1], tmp_path)
    for text in [*strings(report), *exports, *strings(rows), *strings(receiver.events())]:
        assert not any(value in text for value in bad), text


def test_the_administration_log_is_on_the_audit_page_and_reaches_the_siem_with_its_chain(cluster, upstream, receiver):
    ready(cluster, upstream, receiver=receiver, people="named")  # a person acted on is kept as people are (d)
    emily = add_person(cluster, "Emily Carter", EMAIL, Role.SECURITY)
    admin = add_person(cluster, "Sofia Bianchi", "sofia.bianchi@acme.com", Role.ADMIN)
    cluster.server.audit.record(BY_HAND, "person.added", emily.id, {"email": "set"}, "203.0.113.9")

    def sent():
        return [
            e
            for e in receiver.events()
            if e["type"] == "admin" and e["rule"] == emily.id and e["outcome"] == "person.added"
        ]

    [found] = [e for e in cluster.server.audit.query(action="person.added", limit=50) if e.address == "203.0.113.9"]
    sign_in_as(cluster, emily)
    pages = cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    page = cluster.a.get(f"{EE}/audit", params=NOW_DAY).json()
    assert until(lambda: any(e["chain"]["hash"] == found.hash for e in sent()), timeout=30)
    assert {"reports", "audit", "siem"} <= set(pages) and "person.added" in page["actions"]
    [entry] = [e for e in page["entries"] if e["details"] == {"email": "set"}]
    assert entry["target"]["name"] == "Emily Carter" and entry["actor"]["kind"] == "token"
    assert entry["address"] is None and page["addresses"] is False  # the security role reads no address (I12)
    sign_in_as(cluster, admin)
    whole = cluster.a.get(f"{EE}/audit", params=NOW_DAY).json()
    assert whole["addresses"] is True and "203.0.113.9" in [e["address"] for e in whole["entries"]]
    [event] = [e for e in sent() if e["chain"]["hash"] == found.hash]
    assert set(event) == set(FIELDS) and event["chain"] == {"prev": found.prev, "hash": found.hash}
    assert event["reason"] == BY_HAND and "203.0.113.9" not in json.dumps(sent()) and "email" not in json.dumps(sent())


def test_under_pseudonymous_an_administrative_event_never_links_a_persons_exchanges_to_their_name(
    cluster, upstream, receiver, tmp_path
):
    """M1 of the whole-branch review. The Audit page names the person an entry acted on, with the entry's id and time,
    and the SIEM's `admin` event of that entry carries its id and time too: its pseudonym is of a domain of its own,
    never the one the person's exchange events and report rows carry, so the page does not name those."""
    ready(cluster, upstream, receiver=receiver, people="pseudonymous")
    marco = member(cluster, "Marco Rossi", "marco.rossi@acme.com", group="Platform")  # no role: kept as people says
    client, server = cluster.a, cluster.server
    server.audit.record(BY_HAND, "person.changed", marco.identity.id, {"email": "set"})
    a_morning(client, marco, tmp_path)
    drained(client, server, marco)
    assert server.edition.events.pipeline.flush()

    def acted() -> list[dict]:
        return [e for e in receiver.events() if e["type"] == "admin" and e["outcome"] == "person.changed"]

    assert until(lambda: acted() and any(e["type"] == "exchange" for e in receiver.events()), timeout=30)
    exchanged = {e["identity"] for e in receiver.events() if e["type"] in REQUESTS}
    rows = {r["identity"] for r in server.edition.events.store.query(0, time.time() + 3600, by=("identity",))}
    [event] = acted()
    assert exchanged and rows and all(p and p.startswith("p_") for p in exchanged | rows | {event["rule"]})
    assert event["rule"] not in exchanged and event["rule"] not in rows  # the Audit page names no exchange of his


def test_a_gap_marker_no_entry_of_the_log_names_is_reported_by_verify(cluster, upstream):
    ready(cluster, upstream)
    events, now, origin = cluster.server.edition.events, cluster.server.clock(), cluster.server.settings.url
    entries = [{"id": f"{i:032x}", "type": "admin", "chain": {"prev": "", "hash": f"{i:064x}"}} for i in (1, 2, 3)]
    events.store.put(["hook"], entries, at=now, refs=[1, 2, 3])
    assert events.store.collapse("hook", 1, origin=origin, at=now, noted=events.delivery.noted) == (1, 3, 3)
    forged = gap_event(100, 200, 101, "a" * 64, "b" * 64, origin=origin, at=now)  # written in the database by hand
    events.store.put(["hook"], [forged], at=now, refs=[100])
    with cluster.server.db.transaction() as conn:  # R4-1: a form Latet never writes, typed '', spaced, its count text
        conn.execute("INSERT INTO ee_siem_queue (output, at, type, event) VALUES ('hook', %s, '', %s)",
                     (now, '{"type": "admin", "outcome": "siem.gap", "reason": "audit:300-300", "stopped": "1"}'))  # fmt: skip
    verified = cluster.a.get("/_latet/server/v1/admin/log/verify", headers=as_admin(cluster)).json()
    assert verified["ok"] is True and [1, 3, 3] in verified["gaps"]["recorded"]  # Latet's: its entry, in the chain
    found = [(m["output"], m["reason"], m["readable"]) for m in verified["gaps"]["unrecorded"]]
    assert found == [("hook", "audit:100-200", True), ("hook", "audit:300-300", False)]  # R3-1, R4-1
    with cluster.server.db.transaction() as conn:  # a row that is not JSON: the check fails closed, never passes
        conn.execute(
            "INSERT INTO ee_siem_queue (output, at, type, event) VALUES ('hook', %s, 'admin', 'not JSON')", (now,)
        )
    failed = cluster.a.get("/_latet/server/v1/admin/log/verify", headers=as_admin(cluster)).json()
    assert set(failed["gaps"]) == {"error"} and failed["ok"] is True  # the chain's own answer stands beside it


def test_each_person_sees_what_their_role_allows(cluster, upstream, monkeypatch):
    ready(cluster, upstream)
    lead = member(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.GROUP_LEAD, group="Platform")
    nobody = member(cluster, "Marco Rossi", "marco.rossi@acme.com", group="Platform")
    admin = add_person(cluster, "Sofia Bianchi", "sofia.bianchi@acme.com", Role.ADMIN)
    platform = next(g.id for g in cluster.people.groups() if g.name == "Platform")
    client = cluster.a
    sign_in_as(cluster, lead.identity)
    lead_pages = client.get(f"{API}/me").json()["edition"]["pages"]
    lead_report = client.get(f"{EE}/reports").json()
    assert "reports" in lead_pages and "audit" not in lead_pages and client.get(f"{EE}/audit").status_code == 403
    assert "siem" not in lead_pages  # the SIEM outputs are the security role's and auditors' (M13)
    assert lead_report["viewer"]["groups"] == [platform] and lead_report["viewer"]["siem"] is None
    assert client.get(f"{EE}/reports", params={"group": "g_" + "a" * 10}).status_code == 403
    sign_in_as(cluster, nobody.identity)
    assert "reports" not in client.get(f"{API}/me").json()["edition"]["pages"]
    assert client.get(f"{EE}/reports").status_code == 403
    lone = add_person(cluster, "Tom Becker", "tom.becker@acme.com", Role.GROUP_LEAD)  # a group lead in no group
    sign_in_as(cluster, lone)
    assert "reports" not in client.get(f"{API}/me").json()["edition"]["pages"]  # a page that would answer 403 (M13)
    assert client.get(f"{EE}/reports").status_code == 403
    auditor = add_person(cluster, "Priya Nair", "priya.nair@acme.com", Role.AUDITOR)
    sign_in_as(cluster, auditor)
    assert {"reports", "audit", "siem"} <= set(client.get(f"{API}/me").json()["edition"]["pages"])  # in no group
    sign_in_as(cluster, admin)
    admin_pages = client.get(f"{API}/me").json()["edition"]["pages"]
    assert "audit" in admin_pages and "reports" not in admin_pages  # the design's table: administrators read no report
    assert "siem" not in admin_pages
    assert client.get(f"{EE}/reports").status_code == 403 and client.get(f"{EE}/siem").status_code == 403
    assert client.get(f"{EE}/audit", params=NOW_DAY).status_code == 200

    def unreadable(identity):
        raise OSError("the database restarts")

    for server in cluster.servers:  # People's database fails between /me's read of the roles and that of the groups
        monkeypatch.setattr(server.edition.people, "groups_of", unreadable)
    sign_in_as(cluster, lead.identity)
    me = client.get(f"{API}/me")
    assert me.status_code == 200 and "reports" not in me.json()["edition"]["pages"]  # never 500 (R2-7)


def test_a_license_with_the_siem_and_no_reports_lists_the_siem_page_alone(cluster, upstream):
    """M13: the SIEM outputs are a page of their own, for a license that lists the SIEM without reports."""
    from ..people.server_support import license

    license(cluster, features=["roles", "siem"])
    security = add_person(cluster, "Emily Carter", EMAIL, Role.SECURITY)
    sign_in_as(cluster, security)
    pages = cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    assert "siem" in pages and "reports" not in pages and "audit" not in pages
    assert cluster.a.get(f"{EE}/siem").status_code == 200


def test_a_person_in_no_group_and_an_id_the_server_does_not_know_are_looked_up_as_none(cluster):
    """Task 6's carry: the pipeline holds a batch whenever a lookup raises, so a person in no group, an API key and an
    unknown id answer None, and only a server that fails raises."""
    pipeline = cluster.server.edition.events.pipeline
    assert pipeline.group_of is not None and pipeline.kind_of is not None
    lone = add_person(cluster, "Tom Becker", "tom.becker@acme.com")
    key, _secret = cluster.server.identities.add_key("Billing CI")
    assert (pipeline.group_of(lone.id), pipeline.kind_of(lone.id)) == (None, "person")
    assert (pipeline.group_of(key.id), pipeline.kind_of(key.id)) == (None, "key")
    assert (pipeline.group_of("u_nobodyhere"), pipeline.kind_of("u_nobodyhere")) == (None, None)
