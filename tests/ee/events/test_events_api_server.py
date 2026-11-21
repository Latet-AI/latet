"""Task 10's routes on a server of two instances, each guard as a request meets it: the gate's row and the person's roles,
a group lead's groups, the administration log's addresses for administrators alone, an export weighed as a change
before anything is read (the company sign-in, roles taken away), what the license produces, a reader's origins, the
outputs checked at a save and a test, the limits of destinations, tests and exports, and the skip under its conditions
and its lease. The routes are the edition's own (Task 11's ``events_api`` in ``_e3_routes``), with the event log and the
queue in the server's PostgreSQL."""

import contextlib
import json
import logging
import uuid

import pytest

from latet.api.rest import COOKIE
from latet.ee.events import api as api_module
from latet.ee.events import reports as reports_module
from latet.ee.events.api import EventsApi
from latet.ee.events.pg import PgEvents
from latet.ee.events.schema import checked
from latet.ee.roles import Role
from latet.ee.settings import SiemOutput, siem_bound
from latet.events import make_event

from ..people.fake_provider import SSO
from ..people.server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    HOST,
    NOW,
    SERVER_TESTS,
    UI,
    add_person,
    as_admin,
    paused,
    start,
    stop,
)
from .receivers import HttpsReceiver, certificate
from .servers import quiet

pytestmark = SERVER_TESTS
EE = f"{API}/ee"
DAY = 86_400.0
SENTINEL = {"name": "sentinel", "kind": "webhook", "url": "https://x.logic.azure.com/workflows/1/triggers?sig={secret}",
            "secret": "siem-sentinel", "headers": {"X-Source": "latet"}}  # fmt: skip
HIDDEN = "\N{HORIZONTAL ELLIPSIS}"


@pytest.fixture
def cluster(instances, licensed, clock):
    """E3's cluster, each instance's edition serving Task 10's routes as Task 11 wires them (``events_api``, over the
    server's PostgreSQL), its jobs stopped: these tests set queues and states by hand, and drive what they need."""
    started = start(instances, licensed, clock)
    quiet(started)
    yield started
    stop(started)


@pytest.fixture
def receiver(tmp_path):
    cert, key = certificate(tmp_path)
    found = HttpsReceiver(cert, key, tls=False).start()
    yield found
    found.stop()


def _api(cluster) -> EventsApi:
    api = cluster.server.edition.events_api
    assert api is not None
    return api


def _person(cluster, name, *roles):
    return add_person(cluster, name, name.lower().replace(" ", ".") + "@acme.com", *roles)


def _session(cluster, person, via="code"):
    """The person's dashboard session, opened with a one-time code or the company's sign-in."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(person.id, via=via), domain=HOST, path="/_latet")


def _event(cluster, *, identity, group, days=2, masked=None, kind="person"):
    at = NOW - days * DAY
    event = make_event("exchange", clock=lambda: at, identity=identity, kind=kind, group=group, agent="claude-code",
                       project="billing-api", masked=masked or {"EMAIL": 2})  # fmt: skip
    PgEvents(cluster.server.db).write(uuid.uuid4().hex, [checked(event)], at=at)


def _entries(cluster, action):
    return cluster.server.audit.query(action=action, limit=100)


def _exported(cluster):
    """The exports recorded, oldest first: (action, actor)."""
    found = [*_entries(cluster, "reports.exported"), *_entries(cluster, "audit.exported")]
    return [(entry.action, entry.actor) for entry in sorted(found, key=lambda entry: entry.id)]


def _outputs(cluster, *outputs):
    """Outputs set as `latet server siem set` sets them (the generic store, by hand)."""
    cluster.server.edition.settings_store.set_section("siem", {"outputs": list(outputs)}, actor=BY_HAND)


def _groups(cluster):
    people = cluster.people
    return people.add_group("Payments", actor=BY_HAND), people.add_group("Platform", actor=BY_HAND)


# --- who reads and changes what ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "reads", "changes"),
    [
        (None, {}, False),
        (Role.GROUP_LEAD, {"reports"}, False),
        (Role.AUDITOR, {"reports", "audit", "siem"}, False),
        (Role.SECURITY, {"reports", "audit", "siem"}, True),
        (Role.ADMIN, {"audit"}, False),
    ],
)
def test_each_role_reads_and_changes_what_its_row_of_the_gate_allows(cluster, role, reads, changes):
    payments, _ = _groups(cluster)
    person = _person(cluster, "Emily Carter", *([role] if role else []))
    cluster.people.add_member(payments.id, person.id, actor=BY_HAND)
    _session(cluster, person)
    for page in ("reports", "audit", "siem"):
        answer = cluster.a.get(f"{EE}/{page}", headers=UI)
        assert answer.status_code == (200 if page in reads else 403), (page, answer.text)
    saved = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": []})
    assert saved.status_code == (200 if changes else 403), saved.text
    secret = cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "hook", "secret": "s3cr3t"})
    assert secret.status_code == (404 if changes else 403), secret.text  # no such output, once let through
    for path in ("/reports/settings", "/siem/skip"):  # `latet server`'s alone: never the dashboard's
        assert cluster.a.post(f"{EE}{path}", headers=UI, json={"name": "hook"}).status_code in (403, 404, 405)
        assert cluster.a.put(f"{EE}{path}", headers=UI, json={"people": "named"}).status_code in (403, 404, 405)
    assert cluster.server.edition.settings().reports.people == "group-only"


def test_a_group_lead_sees_the_reports_of_the_groups_they_belong_to_alone(cluster):
    payments, platform = _groups(cluster)
    lead = _person(cluster, "Emily Carter", Role.GROUP_LEAD)
    cluster.people.add_member(payments.id, lead.id, actor=BY_HAND)
    james = _person(cluster, "James Wilson")
    _event(cluster, identity=lead.id, group=payments.id)
    _event(cluster, identity=james.id, group=platform.id, masked={"IBAN": 5})
    _session(cluster, lead)
    report = cluster.a.get(f"{EE}/reports", headers=UI).json()
    assert [row["key"] for row in report["by_group"]] == [payments.id] and report["viewer"]["groups"] == [payments.id]
    assert report["totals"]["masked"] == 2 and report["viewer"]["siem"] is None
    assert cluster.a.get(f"{EE}/reports", headers=UI, params={"group": platform.id}).status_code == 403
    exported = cluster.a.get(f"{EE}/reports/export", headers=UI, params={"format": "csv"}).json()
    assert "Payments" in exported["file"] and "Platform" not in exported["file"]
    security = _person(cluster, "Priya Nair", Role.SECURITY)
    _session(cluster, security)
    every = cluster.a.get(f"{EE}/reports", headers=UI).json()
    assert {row["key"] for row in every["by_group"]} == {payments.id, platform.id} and every["viewer"]["siem"] == "edit"
    assert cluster.a.get(f"{EE}/reports", headers=UI, params={"group": "Payments"}).status_code == 400  # by its id


def test_the_administration_log_names_everyone_and_gives_addresses_to_administrators_alone(cluster):
    """Decision 1 (d), I12: the Audit page and its export, to auditors and the security role, without addresses."""
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [SENTINEL]}).status_code == 200
    auditor = _person(cluster, "Priya Nair", Role.AUDITOR)
    admin = _person(cluster, "Daniel Okafor", Role.ADMIN)
    for person, addresses in ((auditor, False), (security, False), (admin, True)):
        _session(cluster, person)
        page = cluster.a.get(f"{EE}/audit", headers=UI, params={"action": "siem.*"}).json()
        [changed] = [entry for entry in page["entries"] if entry["action"] == "siem.changed"]
        assert changed["actor"] == {"id": security.id, "name": "Emily Carter", "kind": "person"}  # named to all
        assert changed["details"]["outputs"] == [{"name": "sentinel", "origin": "https://x.logic.azure.com"}]
        assert page["addresses"] is addresses and (changed["address"] is not None) is addresses, person.name
        exported = json.loads(cluster.a.get(f"{EE}/audit/export", headers=UI, params={"format": "json"}).json()["file"])
        [saved] = [entry for entry in exported["entries"] if entry["action"] == "siem.changed"]
        assert (saved["address"] is not None) is addresses, person.name
        assert addresses or all(entry["address"] is None for entry in exported["entries"])
    by_token = cluster.a.get(f"{ADMIN_EE}/audit", headers=as_admin(cluster)).json()
    assert by_token["addresses"] is True


# --- an export, weighed as a change ------------------------------------------------------------------------------


def _reads(cluster, monkeypatch):
    """What an export reads, counted: nothing is read for a request refused."""
    api, read = _api(cluster), []
    export, whole = api._export, api._whole_log
    monkeypatch.setattr(api, "_export", lambda *args: read.append("report") or export(*args))
    monkeypatch.setattr(api, "_whole_log", lambda *args, **kwargs: read.append("log") or whole(*args, **kwargs))
    return read


def test_an_export_takes_the_company_sign_in_before_anything_is_read(cluster, monkeypatch):
    """R4, decision (c): an export is a company-wide act; refused to a one-time code's session while the sign-in
    settings ask for the company's, nothing read nor recorded; a session the company sign-in opened exports."""
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    read = _reads(cluster, monkeypatch)
    _session(cluster, security, "code")
    assert cluster.a.get(f"{EE}/reports", headers=UI).status_code == 200  # reading is never refused by it (R4)
    for path in ("/reports/export", "/audit/export"):
        refused = cluster.a.get(f"{EE}{path}", headers=UI, params={"format": "csv"})
        assert refused.status_code == 403 and refused.json()["code"] == "company-sign-in", refused.text
    assert read == [] and _exported(cluster) == []
    _session(cluster, security, "sso")
    for path in ("/reports/export", "/audit/export"):
        assert cluster.a.get(f"{EE}{path}", headers=UI, params={"format": "csv"}).status_code == 200
    assert read == ["report", "log"]
    assert _exported(cluster) == [("reports.exported", security.id), ("audit.exported", security.id)]


def test_an_export_weighs_the_persons_roles_past_every_copy_before_anything_is_read(cluster, monkeypatch):
    """R4: a role taken away on any instance is seen in the export's own transaction, whatever a copy says."""
    payments, _ = _groups(cluster)
    priya = _person(cluster, "Priya Nair")  # no role in PostgreSQL
    cluster.people.add_member(payments.id, priya.id, actor=BY_HAND)
    read = _reads(cluster, monkeypatch)
    roles_of = cluster.people.roles_of
    monkeypatch.setattr(
        cluster.people, "roles_of", lambda i: roles_of(i) | {Role.SECURITY} if i == priya.id else roles_of(i)
    )
    _session(cluster, priya)
    assert cluster.a.get(f"{EE}/reports", headers=UI).status_code == 200  # the copy lets her read the page
    for path in ("/reports/export", "/audit/export"):
        refused = cluster.a.get(f"{EE}{path}", headers=UI, params={"format": "json"})
        assert refused.status_code == 403 and "your roles" in refused.json()["error"], refused.text
    assert read == [] and _exported(cluster) == []


def test_exports_are_refused_past_their_most_and_made_one_at_a_time(cluster, monkeypatch):
    """M3, I11 and Task 9's carry: 413 past the rows or entries an export holds, never cut; a report's JSON export
    leaves the log out past its most and says so; 429 while the instance makes another export."""
    payments, _ = _groups(cluster)
    _event(cluster, identity=_person(cluster, "James Wilson").id, group=payments.id)
    admin = as_admin(cluster)
    monkeypatch.setattr(api_module, "AUDIT_LIMIT", 1)
    whole = json.loads(
        cluster.a.get(f"{ADMIN_EE}/reports/export", headers=admin, params={"format": "json"}).json()["file"]
    )
    assert whole["administration_truncated"] is True and "administration" not in whole
    monkeypatch.setattr(api_module, "AUDIT_EXPORT_MOST", 1)
    too_many = cluster.a.get(f"{ADMIN_EE}/audit/export", headers=admin, params={"format": "csv"})
    assert too_many.status_code == 413 and "entries at most" in too_many.json()["error"]
    monkeypatch.setattr(reports_module, "MOST_ROWS", 0)
    assert cluster.a.get(f"{ADMIN_EE}/reports/export", headers=admin, params={"format": "csv"}).status_code == 413
    monkeypatch.setattr(reports_module, "MOST_ROWS", 500_000)
    api = _api(cluster)
    assert api._exporting.acquire(blocking=False)  # another export under way on this instance
    try:
        for path in ("/reports/export", "/audit/export"):
            busy = cluster.a.get(f"{ADMIN_EE}{path}", headers=admin, params={"format": "csv"})
            assert busy.status_code == 429, busy.text
    finally:
        api._exporting.release()
    assert cluster.a.get(f"{ADMIN_EE}/reports/export", headers=admin, params={"format": "csv"}).status_code == 200


# --- what the license produces -------------------------------------------------------------------------------------


def test_a_license_that_produces_nothing_says_why_and_what_was_set_still_reads(cluster):
    _outputs(cluster, SENTINEL)
    paused(cluster)
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    for path in ("/reports", "/reports/export", "/audit", "/audit/export"):
        answer = cluster.a.get(f"{EE}{path}", headers=UI)
        assert answer.status_code == 403 and answer.json()["code"] == "not-producing", path
        assert answer.json()["paused"] == {"why": "expired", "expires": "2027-03-10"}
    view = cluster.a.get(f"{EE}/siem", headers=UI).json()
    assert view["paused"]["why"] == "expired" and view["editable"] is False and view["outputs"][0]["name"] == "sentinel"
    assert cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "sentinel"}).status_code == 403
    assert cluster.a.post(f"{ADMIN_EE}/siem/test", headers=as_admin(cluster), json={"name": "sentinel"}).json()[
        "code"
    ] in ("not-producing", "not-manageable")
    assert _exported(cluster) == [] and _entries(cluster, "siem.tested") == []


# --- the SIEM outputs ------------------------------------------------------------------------------------------------


def test_a_reader_of_the_outputs_sees_their_origin_alone(cluster):
    _outputs(cluster, SENTINEL)
    auditor = _person(cluster, "Priya Nair", Role.AUDITOR)
    _session(cluster, auditor)
    view = cluster.a.get(f"{EE}/siem", headers=UI).json()
    [output] = view["outputs"]
    assert output["url"] == f"https://x.logic.azure.com/{HIDDEN}" and output["headers"] == {"X-Source": HIDDEN}  # I3
    assert view["etag"] is None and view["editable"] is False and "workflows" not in json.dumps(view)
    _session(cluster, _person(cluster, "Emily Carter", Role.SECURITY))
    whole = cluster.a.get(f"{EE}/siem", headers=UI).json()
    assert whole["outputs"][0]["url"] == SENTINEL["url"] and whole["etag"] and whole["editable"] is True


def test_the_dashboard_sets_or_removes_no_secret_of_an_output_the_command_alone_sets(cluster, receiver):
    """L2 of the whole-branch review: an output at http:// or at this computer is `latet server`'s alone (I2), and so is
    its secret. From the dashboard neither the secret's route nor a save that gives one for it changes it, whether the
    secret is set or removed; `latet server` still can."""
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    admin = as_admin(cluster)
    local = {
        "name": "collector",
        "kind": "webhook",
        "url": receiver.url,
        "headers": {"Authorization": "Bearer {secret}"},
    }
    by_command = {"outputs": [local], "secrets": {"collector": "c0llect0r-by-command"}}
    assert cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json=by_command).status_code == 200
    local["secret"] = "siem-collector"  # as the page shows it, and sends it back
    bound = siem_bound(SiemOutput.model_validate(local))
    for secret in ("guessed-v4lue", None):
        refused = cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "collector", "secret": secret})
        assert refused.status_code == 403 and refused.json()["code"] == "by-command", refused.text
        assert "latet server siem secret" in refused.text and "guessed" not in refused.text
        etag = cluster.a.get(f"{EE}/siem", headers=UI).json()["etag"]
        given = {"outputs": [local], "secrets": {"collector": secret}, "etag": etag}  # the output itself unchanged
        saved = cluster.a.put(f"{EE}/siem", headers=UI, json=given)
        assert saved.status_code == 403 and saved.json()["code"] == "by-command", saved.text
        assert "latet server siem secret" in saved.text  # L2's own refusal, not I2's of an output changed
    assert cluster.server.edition.secrets.load("siem-collector", bound=bound) == "c0llect0r-by-command"  # as it was
    assert [e for e in _entries(cluster, "siem.*") if e.actor == security.id] == []  # nothing changed, nothing noted
    renewed = cluster.a.put(f"{ADMIN_EE}/siem/secret", headers=admin, json={"name": "collector", "secret": "n3w-t0ken"})
    assert (
        renewed.status_code == 200 and cluster.server.edition.secrets.load("siem-collector", bound=bound) == "n3w-t0ken"
    )


def test_the_dashboard_saves_outputs_checked_with_their_secrets_sealed_and_recorded(cluster, receiver):
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    pasted = [{"name": "splunk", "kind": "webhook", "url": "https://siem.acme.com/in",
               "headers": {"Authorization": "Splunk 5f0c9a2e"}}]  # fmt: skip
    refused = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": pasted})
    assert refused.status_code == 400 and "carries a credential" in refused.text and "5f0c9a2e" not in refused.text
    inside = [{"name": "meta", "kind": "webhook", "url": "https://[fe80::1]/latest"}]
    unreached = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": inside})
    assert unreached.status_code == 400 and "never reaches" in unreached.text  # R10, before the transaction
    local = {"name": "collector", "kind": "webhook", "url": receiver.url}
    by_command = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [local]})  # this computer, from the dashboard
    assert by_command.status_code == 403 and "latet server siem set" in by_command.text  # the command's alone (I2)
    hook = {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in",
            "headers": {"Authorization": "Bearer {secret}"}}  # fmt: skip
    saved = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [hook], "secrets": {"hook": "h00k-t0ken"}})
    assert saved.status_code == 200 and "h00k-t0ken" not in saved.text, saved.text
    [view] = saved.json()["outputs"]
    assert view["secret"] == "siem-hook" and view["has_secret"] is True
    bound = siem_bound(SiemOutput.model_validate(hook | {"secret": "siem-hook"}))
    assert cluster.server.edition.secrets.load("siem-hook", bound=bound) == "h00k-t0ken"  # sealed for it (I1)
    # The server's own (`siem.forwarding`: where its administration log's forwarding began, at its first start) aside.
    actions = [(e.actor, e.action) for e in reversed(_entries(cluster, "siem.*")) if e.actor != "server"]
    assert actions == [(security.id, "siem.secret.changed"), (security.id, "siem.changed")]
    assert "h00k-t0ken" not in json.dumps([e.details for e in _entries(cluster, "siem.*")])
    stale = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [], "etag": "0" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "siem-changed"
    hook["secret"] = "siem-hook"  # as the page shows it, and sends it back
    admin = as_admin(cluster)
    assert cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json={"outputs": [hook, local]}).status_code == 200
    etag = cluster.a.get(f"{EE}/siem", headers=UI).json()["etag"]
    kept = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [hook, local], "etag": etag})  # set by command
    assert kept.status_code == 200, kept.text
    assert [output["has_secret"] for output in kept.json()["outputs"]] == [True, False]
    moved = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [hook, local | {"url": receiver.url + "/x"}]})
    assert moved.status_code == 403  # moved along this computer: the command's still
    unnamed = {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"}  # its secret named no more
    unnamed = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [unnamed]})
    assert unnamed.status_code == 200 and cluster.server.edition.secrets.load("siem-hook", bound=bound) is None
    assert _entries(cluster, "siem.secret.changed")[0].details == {"saved": [], "removed": ["hook"]}


def test_the_dashboard_adds_or_moves_ten_destinations_an_hour_a_person(cluster):
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)

    def outputs(n, host="siem{i}.acme.com"):
        return [{"name": f"siem{i}", "kind": "webhook", "url": f"https://{host.format(i=i)}/in"} for i in range(n)]

    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": outputs(9)}).status_code == 200
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": outputs(9)}).status_code == 200  # none moved
    moved = outputs(9)
    moved[0]["url"] = "https://siem0.acme.com/elsewhere"  # the same scheme, host and port: not moved
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": moved}).status_code == 200
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": outputs(10)}).status_code == 200  # the tenth
    other = outputs(10, host="collector{i}.acme.com")[:1] + outputs(10)[1:]  # one moved to another host: the eleventh
    refused = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": other})
    assert refused.status_code == 429 and "latet server siem set" in refused.text  # R2-4: no scanner by saves
    admin = as_admin(cluster)
    assert (
        cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json={"outputs": outputs(12)}).status_code == 200
    )  # no limit


def test_an_outputs_secret_is_set_named_after_it_and_removed(cluster):
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    set_ = cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "hook", "secret": "h00k-t0ken"})
    assert set_.json() == {"ok": True}
    [output] = cluster.a.get(f"{EE}/siem", headers=UI).json()["outputs"]
    assert output["secret"] == "siem-hook" and output["has_secret"] is True
    mine = [e.action for e in reversed(_entries(cluster, "siem.*")) if e.actor == security.id]
    assert mine == ["siem.changed", "siem.secret.changed"]
    assert cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "hook", "secret": None}).status_code == 200
    assert cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "hook", "secret": None}).status_code == 404
    assert cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "nope", "secret": "x"}).status_code == 404
    two_lines = cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "hook", "secret": "a" + chr(10) + "b"})
    assert two_lines.status_code == 400
    assert "h00k-t0ken" not in json.dumps([e.details for e in _entries(cluster, "siem.*")])


def test_a_test_reaches_a_saved_output_once_its_person_is_weighed_and_within_its_limits(cluster, receiver, monkeypatch):
    collector = {"name": "collector", "kind": "webhook", "url": receiver.url}
    _outputs(cluster, collector, {"name": "inside", "kind": "webhook", "url": "https://10.20.30.40/in"})
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    local = cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "collector"})
    assert local.status_code == 403 and "latet server siem test" in local.text and receiver.received == []  # I2
    admin = as_admin(cluster)
    tested = cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "collector"})
    assert tested.json() == {"ok": True, "detail": "it arrived"} and len(receiver.received) == 1
    again = cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "collector"})
    assert again.status_code == 429 and len(receiver.received) == 1  # one an output every 10 seconds (I2)
    [entry] = _entries(cluster, "siem.tested")
    assert entry.actor.startswith("admin:") and entry.details == {"output": "collector", "status": 200}
    assert cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "nope"}).status_code == 404
    from latet.server import egress

    monkeypatch.setattr(egress, "forbidden", lambda *args, **kwargs: True)  # egress refuses it now, not at its save
    unreached = cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "inside"})
    assert unreached.status_code == 400 and "10.20.30.40" not in unreached.text
    assert len(_entries(cluster, "siem.tested")) == 1


def test_a_test_weighs_the_persons_roles_before_the_address_is_reached(cluster, monkeypatch):
    """M15: a role taken away a moment ago, which a copy still holds, sends no test."""
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    priya = _person(cluster, "Priya Nair")
    roles_of = cluster.people.roles_of
    monkeypatch.setattr(
        cluster.people, "roles_of", lambda i: roles_of(i) | {Role.SECURITY} if i == priya.id else roles_of(i)
    )
    reached: list[str] = []
    monkeypatch.setattr(_api(cluster), "_tested", lambda output: reached.append(output.name) or (True, "it arrived"))
    _session(cluster, priya)
    refused = cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "hook"})
    assert refused.status_code == 403 and "your roles" in refused.json()["error"] and reached == []
    cluster.people.grant(priya.id, Role.SECURITY, actor=BY_HAND)
    assert cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "hook"}).json()["ok"] is True
    assert reached == ["hook"] and [e.actor for e in _entries(cluster, "siem.tested")] == [priya.id]


# --- the skip, `latet server`'s alone --------------------------------------------------------------------------------


def _held_entry(cluster, ref=7):
    entry = {"id": f"{ref:032x}", "type": "admin", "outcome": "person.added",
             "chain": {"prev": f"{ref - 1:064x}", "hash": f"{ref:064x}"}}  # fmt: skip
    PgEvents(cluster.server.db).put(["hook"], [entry], at=NOW, refs=[ref])


def test_a_skip_passes_only_what_the_receiver_refused_under_the_outputs_lease(cluster):
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    queue, admin = PgEvents(cluster.server.db), as_admin(cluster)
    _held_entry(cluster)
    waiting = cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "hook"})
    assert waiting.status_code == 409 and "nothing to skip" in waiting.text  # merely waiting its turn: never skipped
    queue.failed("hook", "it answered 400", at=NOW, retry_at=NOW + 5, blocked=True)
    with cluster.server.bus.lease("siem:hook", 60) as lease:  # another instance sends to it now
        assert lease
        sending = cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "hook"})
        assert sending.status_code == 409 and "another instance" in sending.text
    [(_, head)] = queue.peek("hook", 10)
    assert head["outcome"] == "person.added" and queue.state("hook").blocked and _entries(cluster, "siem.skipped") == []
    skipped = cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "hook"})
    assert skipped.json() == {"ok": True, "from": 7, "to": 7, "count": 1, "dropped": 0}
    [entry] = _entries(cluster, "siem.skipped")
    assert entry.actor.startswith("admin:") and entry.details == {
        "output": "hook", "from": 7, "to": 7, "count": 1, "dropped": 0, "status": 200
    }  # fmt: skip
    [(_, marker)] = queue.peek("hook", 10)
    assert (marker["outcome"], marker["reason"]) == ("siem.gap", "audit:7-7") and not queue.state("hook").blocked
    assert cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "nope"}).status_code == 404


# --- `latet server`'s calls ----------------------------------------------------------------------------------------


def test_latet_servers_calls_are_recorded_once_under_what_they_did(cluster):
    admin = as_admin(cluster)
    exported = cluster.a.get(f"{ADMIN_EE}/reports/export", params={"format": "json"}, headers=admin)
    assert exported.status_code == 200 and "administration" in json.loads(exported.json()["file"])
    assert [e.actor.startswith("admin:") for e in _entries(cluster, "reports.exported")] == [True]
    assert _entries(cluster, "ee.reports.read") == []  # the export's entry is the call's own
    assert cluster.a.get(f"{ADMIN_EE}/reports/settings", headers=admin).json() == {
        "people": "group-only", "retention_days": 365
    }  # fmt: skip
    changed = cluster.a.put(f"{ADMIN_EE}/reports/settings", headers=admin, json={"people": "pseudonymous"})
    assert changed.status_code == 200 and changed.json()["people"] == "pseudonymous"
    [entry] = _entries(cluster, "reports.changed")
    assert entry.details["people"] == "pseudonymous" and entry.details["fields"] == ["people"]
    assert cluster.a.put(f"{ADMIN_EE}/reports/settings", headers=admin, json={"teleport": 1}).status_code == 400
    assert cluster.a.put(f"{ADMIN_EE}/reports/settings", headers=admin, json={"retention_days": 0}).status_code == 400


# --- fix round 1 ----------------------------------------------------------------------------------------------------


def _demoted_while_made(cluster, monkeypatch, made, person, taken, given, group=None):
    """``person`` loses the role ``taken`` (and is given ``given``) while the export's file is made (``made``: the
    function of api.py that makes it), as an administrator on another instance would."""
    original = getattr(api_module, made)

    def demoted(*args, **kwargs):
        cluster.people.revoke(person.id, taken, actor=BY_HAND)
        cluster.people.grant(person.id, given, actor=BY_HAND)
        return original(*args, **kwargs)

    monkeypatch.setattr(api_module, made, demoted)


def test_an_export_is_given_only_while_the_roles_that_made_it_still_allow_what_it_shows(cluster, monkeypatch):
    """Task 10's review: what an export showed (every group, the log, its addresses) was read through E3's copies; the
    roles its own transaction finds must still allow it, or nothing is given nor recorded."""
    payments, _ = _groups(cluster)
    emily = _person(cluster, "Emily Carter", Role.SECURITY)
    cluster.people.add_member(payments.id, emily.id, actor=BY_HAND)
    _event(cluster, identity=emily.id, group=payments.id)
    _session(cluster, emily)
    _demoted_while_made(cluster, monkeypatch, "export_file", emily, Role.SECURITY, Role.GROUP_LEAD)
    refused = cluster.a.get(f"{EE}/reports/export", headers=UI, params={"format": "csv"})
    assert refused.status_code == 403 and "changed while the export was made" in refused.text  # every group's rows
    assert "billing-api" not in refused.text and _exported(cluster) == []
    daniel = _person(cluster, "Daniel Okafor", Role.ADMIN)
    _session(cluster, daniel)
    _demoted_while_made(cluster, monkeypatch, "audit_file", daniel, Role.ADMIN, Role.AUDITOR)
    refused = cluster.a.get(f"{EE}/audit/export", headers=UI, params={"format": "json"})
    assert refused.status_code == 403 and "changed while the export was made" in refused.text  # the addresses
    assert "203.0.113.7" not in refused.text and _exported(cluster) == []
    assert cluster.a.get(f"{EE}/audit/export", headers=UI, params={"format": "json"}).status_code == 200  # an auditor's
    assert _exported(cluster) == [("audit.exported", daniel.id)]


def test_a_tests_attempt_is_recorded_before_the_address_is_reached(cluster, monkeypatch):
    """Task 10's review: the entry of a test is committed, with the person weighed, before the connection is made: a
    failure afterwards never leaves a connection that no entry names."""
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    security = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, security)
    seen: list[list[str]] = []

    def connecting(output):
        seen.append([entry.actor for entry in _entries(cluster, "siem.tested")])
        raise RuntimeError("the connection failed in a way nobody foresaw")

    monkeypatch.setattr(_api(cluster), "_tested", connecting)
    with contextlib.suppress(Exception):
        cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "hook"})
    assert seen == [[security.id]]  # committed before the address was reached
    assert [entry.details for entry in _entries(cluster, "siem.tested")] == [{"output": "hook"}]


class _Lease:
    """A lease of the test's own: taken, and ``held`` saying whether it is still this holder's; ``fails``: giving it
    back fails as a Valkey that does not answer."""

    def __init__(self, *, held=True, fails=False):
        self._held, self._fails, self.asked = held, fails, []

    def __bool__(self):
        return True

    def held(self, margin=0.0):
        self.asked.append(margin)
        return self._held

    def release(self):
        self._held = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._fails:
            raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")
        return False


def test_a_skip_whose_lease_ran_out_before_its_commit_changes_nothing(cluster, monkeypatch):
    """Task 10's review: as the delivery asks before a batch leaves the queue, the skip asks its lease last in its
    transaction: one run out (another instance may hold it now) skips nothing, records nothing."""
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    queue, admin = PgEvents(cluster.server.db), as_admin(cluster)
    _held_entry(cluster)
    queue.failed("hook", "it answered 400", at=NOW, retry_at=NOW + 5, blocked=True)
    lease = _Lease(held=False)
    monkeypatch.setattr(cluster.server.bus, "lease", lambda name, seconds: lease)
    lost = cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "hook"})
    assert lost.status_code == 409 and lease.asked == [5.0]
    [(_, head)] = queue.peek("hook", 10)
    assert head["outcome"] == "person.added" and queue.state("hook").blocked and _entries(cluster, "siem.skipped") == []


def test_a_lease_not_given_back_after_a_skip_is_said_by_its_type_and_the_skip_stands(cluster, monkeypatch, caplog):
    _outputs(cluster, {"name": "hook", "kind": "webhook", "url": "https://siem.acme.com/in"})
    queue, admin = PgEvents(cluster.server.db), as_admin(cluster)
    _held_entry(cluster)
    queue.failed("hook", "it answered 400", at=NOW, retry_at=NOW + 5, blocked=True)
    monkeypatch.setattr(cluster.server.bus, "lease", lambda name, seconds: _Lease(fails=True))
    with caplog.at_level(logging.WARNING, logger="latet.ee.events.api"):
        skipped = cluster.a.post(f"{ADMIN_EE}/siem/skip", headers=admin, json={"name": "hook"})
    assert skipped.json() == {"ok": True, "from": 7, "to": 7, "count": 1, "dropped": 0}  # committed: it stands
    said = [record.getMessage() for record in caplog.records if record.name == "latet.ee.events.api"]
    assert len(said) == 1 and "ConnectionError" in said[0] and "valkey.acme.lan" not in said[0]
    assert "valkey" not in skipped.text and len(_entries(cluster, "siem.skipped")) == 1


def test_one_report_or_export_at_a_time_for_each_person(cluster):
    """The dashboard's threads are few: a person's report or export while theirs is made is 429, in fixed words."""
    emily = _person(cluster, "Emily Carter", Role.SECURITY)
    _session(cluster, emily)
    api = _api(cluster)
    api._busy.add(emily.id)  # hers, under way
    try:
        for path in ("/reports", "/reports/export", "/audit/export"):
            busy = cluster.a.get(f"{EE}{path}", headers=UI)
            assert busy.status_code == 429 and "still being made" in busy.text, path
        _session(cluster, _person(cluster, "Priya Nair", Role.AUDITOR))
        assert cluster.a.get(f"{EE}/reports", headers=UI).status_code == 200  # another person's own
    finally:
        api._busy.discard(emily.id)
    _session(cluster, emily)
    assert cluster.a.get(f"{EE}/reports", headers=UI).status_code == 200
    assert api._busy == set() and not api._exporting.locked()


def test_an_exports_slot_is_held_until_its_body_is_sent(cluster):
    """Task 10's review: the instance's one export is given back once the body is sent, never before (a reader that
    reads slowly would hold hundreds of megabytes per export otherwise)."""
    api, client = _api(cluster), cluster.a
    held_while_sent: list[bool] = []
    path = f"{ADMIN_EE}/reports/export"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"format=csv", "root_path": "",
        "headers": [(b"host", HOST.encode()), (b"x-latet-admin", cluster.admin_token.encode())],
        "client": ("203.0.113.7", 50000), "server": (HOST, 443),
    }  # fmt: skip

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            held_while_sent.append(api._exporting.locked())

    client.portal.call(client.app, scope, receive, send)
    assert held_while_sent == [True] and not api._exporting.locked()
