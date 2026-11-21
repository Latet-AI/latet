"""Task 10's routes on a server whose edition wires them (Task 11), E4's jobs running behind them, as each role, each
session and each standing of the license meets them: who only reads the outputs sees no signature and changes nothing;
a credential is refused without being repeated; the security role sets an output's secret, each change recorded, the
secret in none, and its test arrives through `latet server` (an output on this computer is the command's alone: Task
5's carry); the dashboard adds or moves ten destinations an hour a person; an export and a change take the company
sign-in where the settings ask for it; a license that produces nothing says why; `latet server`'s calls are recorded
once, under what they did; and an API key's space reaches none of E4's routes."""

import json
import threading

import pytest

from latet.ee.roles import Role
from latet.ee.settings import SiemOutput, siem_bound

from ..people.fake_provider import SSO
from ..people.server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
    add_person,
    as_admin,
    end_trial,
    on_trial,
    paused,
    settle,
    sign_in_as,
)
from .receivers import HttpsReceiver, certificate, until
from .servers import company_session, ready

pytestmark = SERVER_TESTS
EE = f"{API}/ee"
UI = {"x-latet-ui": "1"}
SENTINEL = {"name": "sentinel", "kind": "webhook", "url": "https://x.logic.azure.com/workflows/1/triggers?sig={secret}",
            "secret": "siem-sentinel", "headers": {"X-Source": "latet"}}  # fmt: skip
HIDDEN = "\N{HORIZONTAL ELLIPSIS}"


def _entries(cluster, action):
    return list(cluster.server.audit.query(action=action, limit=100))


@pytest.fixture
def receiver(tmp_path):
    cert, key = certificate(tmp_path)
    found = HttpsReceiver(cert, key, tls=False).start()
    yield found
    found.stop()


def test_who_only_reads_the_outputs_sees_no_signature_and_changes_nothing(cluster, upstream):
    ready(cluster, upstream)
    cluster.server.edition.settings_store.set_section("siem", {"outputs": [SENTINEL]}, actor=BY_HAND)
    auditor = add_person(cluster, "Priya Nair", "priya.nair@acme.com", Role.AUDITOR)
    sign_in_as(cluster, auditor)
    settle(lambda: cluster.a.get(f"{EE}/siem").json()["outputs"] != [])
    view = cluster.a.get(f"{EE}/siem").json()
    [output] = view["outputs"]
    assert output["url"] == f"https://x.logic.azure.com/{HIDDEN}" and output["headers"] == {"X-Source": HIDDEN}  # I3
    assert view["editable"] is False
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": []}).status_code == 403
    assert cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "sentinel"}).status_code == 403
    assert [e for e in _entries(cluster, "siem.*") if e.actor == auditor.id] == []


def test_a_local_outputs_secret_is_the_commands_and_its_test_arrives_each_recorded(cluster, upstream, receiver):
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    sign_in_as(cluster, security)
    pasted = [{"name": "splunk", "kind": "webhook", "url": "https://siem.acme.com/in",
               "headers": {"Authorization": "Splunk 5f0c9a2e"}}]  # fmt: skip
    refused = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": pasted})
    assert refused.status_code == 400 and "carries a credential" in refused.text and "5f0c9a2e" not in refused.text
    inside = [{"name": "meta", "kind": "webhook", "url": "https://169.254.169.254/latest"}]
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": inside}).status_code == 400  # R10
    collector = {"name": "collector", "kind": "webhook", "url": receiver.url, "secret": "siem-collector",
                 "headers": {"Authorization": "Bearer {secret}"}}  # fmt: skip
    local = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [collector]})  # this computer, from the dashboard
    assert local.status_code == 403 and "latet server siem set" in local.text  # the command's alone (I2)
    admin = as_admin(cluster)
    assert cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json={"outputs": [collector]}).status_code == 200
    settle(lambda: cluster.a.get(f"{EE}/siem").json()["outputs"] != [])
    assert cluster.a.get(f"{EE}/siem").json()["outputs"][0]["has_secret"] is False
    refused = cluster.a.put(f"{EE}/siem/secret", headers=UI, json={"name": "collector", "secret": "c0ll-ector"})
    assert refused.status_code == 403 and refused.json()["code"] == "by-command"  # its secret is the command's (L2)
    secret = cluster.a.put(f"{ADMIN_EE}/siem/secret", headers=admin, json={"name": "collector", "secret": "c0ll-ector"})
    assert secret.status_code == 200 and "c0ll-ector" not in secret.text
    bound = siem_bound(SiemOutput.model_validate(collector))
    assert cluster.server.edition.secrets.load("siem-collector", bound=bound) == "c0ll-ector"  # sealed for it (I1)
    etag = cluster.a.get(f"{EE}/siem").json()["etag"]
    kept = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [collector], "etag": etag})  # unchanged: allowed
    assert kept.status_code == 200, kept.text
    assert kept.json()["outputs"][0]["has_secret"] is True and "c0ll-ector" not in kept.text
    second = {"name": "second", "kind": "webhook", "url": "https://siem2.acme.com/in"}
    meanwhile = cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json={"outputs": [collector, second]})
    assert meanwhile.status_code == 200  # changed by `latet server siem set` since the page was read
    stale = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": [], "etag": etag})
    assert stale.status_code == 409 and stale.json()["code"] == "siem-changed"
    from_dashboard = cluster.a.post(f"{EE}/siem/test", headers=UI, json={"name": "collector"})
    assert from_dashboard.status_code == 403 and "latet server siem test" in from_dashboard.text  # I2: this computer
    tested = cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "collector"}).json()
    assert tested == {"ok": True, "detail": "it arrived"}
    assert receiver.received[-1][0]["authorization"] == "Bearer c0ll-ector"  # its secret, set by the command
    again = cluster.a.post(f"{ADMIN_EE}/siem/test", headers=admin, json={"name": "collector"})
    assert again.status_code == 429  # one test an output every 10 seconds (I2)
    # E4's delivery, running behind the routes, sends the administration log there with the same secret.
    assert until(lambda: any(e["type"] == "admin" for e in receiver.events()), timeout=30)
    assert {headers["authorization"] for headers, _ in receiver.received} == {"Bearer c0ll-ector"}
    actions = [(e.actor, e.action) for e in reversed(_entries(cluster, "siem.*")) if e.actor == security.id]
    assert actions == [(security.id, "siem.changed")]  # her save of it unchanged; the refused secret noted nothing
    [entry] = _entries(cluster, "siem.tested")
    assert entry.actor.startswith("admin:")
    assert "c0ll-ector" not in json.dumps([e.details for e in _entries(cluster, "siem.*")])
    assert "c0ll-ector" not in json.dumps(receiver.events())


def test_the_dashboard_adds_or_moves_ten_destinations_an_hour_a_person(cluster, upstream):
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    sign_in_as(cluster, security)

    def outputs(n, host="siem{i}.acme.com"):
        return [{"name": f"siem{i}", "kind": "webhook", "url": f"https://{host.format(i=i)}/in"} for i in range(n)]

    def save(found):
        etag = cluster.a.get(f"{EE}/siem").json()["etag"]
        return cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": found, "etag": etag})

    assert save(outputs(9)).status_code == 200
    assert save(outputs(9)).status_code == 200  # none moved
    moved = outputs(9)
    moved[0]["url"] = "https://siem0.acme.com/elsewhere"  # the same scheme, host and port: not moved
    assert save(moved).status_code == 200
    assert save(outputs(10)).status_code == 200  # the tenth
    other = outputs(10, host="collector{i}.acme.com")[:1] + outputs(10)[1:]  # one moved to another host: the eleventh
    refused = save(other)
    assert refused.status_code == 429 and "latet server siem set" in refused.text  # R2-4: no scanner by saves
    admin = as_admin(cluster)
    assert (
        cluster.a.put(f"{ADMIN_EE}/siem", headers=admin, json={"outputs": outputs(12)}).status_code == 200
    )  # no limit
    settle(
        lambda: [o["name"] for o in cluster.a.get(f"{EE}/siem").json()["outputs"]] == [f"siem{i}" for i in range(12)]
    )


def test_the_destinations_are_counted_before_the_saves_lock(cluster, upstream, monkeypatch):
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    sign_in_as(cluster, security)
    bus = cluster.server.bus
    allow, changed = bus.allow, threading.Event()

    def counting(*args):  # another change of the Enterprise settings meanwhile is never kept waiting (R3-3)
        def change():
            reports = {"people": "group-only", "retention_days": 365}
            cluster.server.edition.settings_store.set_section("reports", reports, actor=BY_HAND)
            changed.set()

        worker = threading.Thread(target=change)
        worker.start()
        worker.join(10)
        return allow(*args)

    monkeypatch.setattr(bus, "allow", counting)
    one = [{"name": "siem0", "kind": "webhook", "url": "https://siem0.acme.com/in"}]
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": one}).status_code == 200 and changed.is_set()

    def down(*args):
        raise ConnectionError("valkey.acme.lan:6379 does not answer")

    monkeypatch.setattr(bus, "allow", down)
    two = [*one, {"name": "siem1", "kind": "webhook", "url": "https://siem1.acme.com/in"}]
    refused = cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": two})
    assert refused.status_code == 503 and "valkey.acme.lan" not in refused.text  # fixed words, never a 500


def test_an_export_and_a_change_take_the_company_sign_in_where_the_settings_ask_for_it(cluster, upstream):
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    settle(lambda: all(server.edition.settings().sso is not None for server in cluster.servers))
    company_session(cluster, security, "code")  # `latet dashboard`'s one-time code: an agent can ask for one
    assert cluster.a.get(f"{EE}/reports").status_code == 200  # reading is never refused by it (R4)
    refused = cluster.a.get(f"{EE}/reports/export", headers=UI, params={"format": "csv"})
    assert refused.status_code == 403 and refused.json()["code"] == "company-sign-in"
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": []}).json()["code"] == "company-sign-in"
    company_session(cluster, security, "sso")
    assert cluster.a.get(f"{EE}/reports/export", headers=UI, params={"format": "csv"}).status_code == 200
    assert [e.actor for e in _entries(cluster, "reports.exported")] == [security.id]


@pytest.mark.parametrize(
    ("path", "params", "action"),
    [
        ("/reports/export", {"format": "csv"}, "reports.exported"),
        ("/audit/export", {"format": "json"}, "audit.exported"),
    ],
)
def test_an_export_from_the_dashboard_needs_its_header_and_takes_nothing_without_it(
    cluster, upstream, path, params, action
):
    """L1 of the whole-branch review: an export is a GET that changes things (its entry in the log and to the SIEM, the
    instance's one export at a time). A page a browser lets make that GET with the session's cookie (a sibling site, in
    a browser that sends no Sec-Fetch-Site) is refused it as a change is, before anything is taken or written."""
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    sign_in_as(cluster, security)
    refused = cluster.a.get(f"{EE}{path}", params=params)
    assert refused.status_code == 403 and "X-Latet-UI" in refused.json()["error"]
    assert _entries(cluster, action) == []  # nothing written
    assert cluster.a.get(f"{EE}{path}", params=params, headers=UI).status_code == 200  # nothing held: no 429
    assert [e.actor for e in _entries(cluster, action)] == [security.id]
    admin = as_admin(cluster)  # `latet server`'s calls are no page's: a token, never a cookie
    assert cluster.a.get(f"{ADMIN_EE}{path}", params=params, headers=admin).status_code == 200


@pytest.mark.parametrize(("standing", "why"), [("paused", "expired"), ("trial", "trial-ended")])
def test_a_license_that_produces_nothing_says_why_and_reads_what_it_holds(cluster, upstream, standing, why):
    ready(cluster, upstream)
    if standing == "paused":
        paused(cluster)
    else:
        on_trial(cluster)
        end_trial(cluster)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    sign_in_as(cluster, security)
    answer = cluster.a.get(f"{EE}/reports")
    assert answer.status_code == 403 and answer.json()["code"] == "not-producing"
    assert answer.json()["paused"]["why"] == why
    assert cluster.a.get(f"{EE}/siem").status_code == 200  # what was set is read in every standing (E1's)
    assert cluster.a.put(f"{EE}/siem", headers=UI, json={"outputs": []}).status_code == 403
    assert all(server.edition.keeps_events() for server in cluster.servers)  # the log keeps counting (R2)
    assert not any(server.edition.events.pipeline.siem_on() for server in cluster.servers)  # nothing to the SIEM


def test_latet_servers_calls_are_recorded_once_under_what_they_did(cluster, upstream):
    ready(cluster, upstream)
    admin = as_admin(cluster)
    exported = cluster.a.get(f"{ADMIN_EE}/reports/export", params={"format": "json"}, headers=admin)
    assert exported.status_code == 200 and "administration" in json.loads(exported.json()["file"])
    changed = cluster.a.put(f"{ADMIN_EE}/reports/settings", headers=admin, json={"people": "pseudonymous"})
    assert changed.status_code == 200 and changed.json()["people"] == "pseudonymous"
    assert [e.actor.startswith("admin:") for e in _entries(cluster, "reports.exported")] == [True]
    assert _entries(cluster, "ee.reports.read") == []  # the export's entry is the call's own
    [entry] = [e for e in _entries(cluster, "reports.changed") if e.actor != BY_HAND]  # `ready`'s by hand aside
    assert entry.details["people"] == "pseudonymous" and entry.details["fields"] == ["people"]
    assert cluster.a.put(f"{EE}/reports/settings", headers=UI, json={"people": "named"}).status_code in (
        401,
        403,
        404,
        405,
    )
    generic = cluster.a.put(f"{ADMIN_EE}/settings/siem", headers=admin, json={"outputs": []})
    assert generic.status_code == 409  # R11: the SIEM's own routes alone


def test_an_api_keys_space_reaches_none_of_e4s_routes(cluster, upstream):
    """Task 10's review: once wired, every path of E4 (Reports, its export, the administration log and its export, the
    SIEM outputs, their secret, test and skip) is refused inside an API key's space, whoever controls the key."""
    ready(cluster, upstream)
    security = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.SECURITY)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    cluster.people.add_controller(key.id, security.id, actor=BY_HAND)
    sign_in_as(cluster, security)
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/me").status_code == 200  # her key's space opens
    reads = ("ee/reports", "ee/reports/export?format=csv", "ee/audit", "ee/audit/export?format=json", "ee/siem")
    for path in reads:
        refused = cluster.a.get(f"{space}/{path}", headers=UI)  # the space refuses it, not the missing header
        assert refused.status_code == 403, path
    changes = [
        ("put", "ee/siem", {"outputs": []}),
        ("put", "ee/siem/secret", {"name": "hook", "secret": "h00k"}),
        ("post", "ee/siem/test", {"name": "hook"}),
        ("post", "ee/siem/skip", {"name": "hook"}),
        ("put", "ee/reports/settings", {"people": "named"}),
    ]
    for method, path, body in changes:
        refused = getattr(cluster.a, method)(f"{space}/{path}", headers=UI, json=body)
        assert refused.status_code in (403, 404), path  # 404: a route the dashboard does not serve at all
    assert _entries(cluster, "siem.*") == [e for e in _entries(cluster, "siem.*") if e.actor == "server"]
    assert _entries(cluster, "reports.exported") == [] == _entries(cluster, "audit.exported")
    assert _entries(cluster, "key.space.changed") == []  # refused there: nothing was changed
