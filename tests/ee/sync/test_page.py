"""The Term sync page's routes on a server: the security role sees the sources' state and the terms gone, asks for a
sync and removes gone terms by their ids; nobody else gets an answer or a term; the gone list and every change need a
session the company's sign-in opened while the sign-in settings ask for one; /me lists the page for whoever may use it."""

import functools

import httpx
import pytest

from latet.ee.roles import Role
from latet.ee.settings import SOURCES_CHANGED, TERM_FILES
from latet.ee.sync.graph import read_graph
from latet.ee.sync.service import CHANNEL, TICK
from latet.ee.sync.sources import read_url

from ..events.servers import company_session
from ..people.fake_provider import SSO
from ..people.server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
    UI,
    add_person,
    as_admin,
    license,
    paused,
    settle,
)

pytestmark = SERVER_TESTS
PAGE = f"{API}/ee/term-sync"
TERMS = f"{ADMIN_EE}/term-sync"


@pytest.fixture
def requests_made():
    """What the instances' url and Graph readers asked for, each request's host and Authorization header: none is
    reached (tests on 127.0.0.1)."""
    return []


@pytest.fixture
def cluster(cluster, requests_made):
    """E3's cluster, its url and Graph sources read through a transport that answers 503 here and notes the host each
    request was for and what it carried (as test_routes.py's), once every instance hears ee:terms."""

    def refused(request: httpx.Request) -> httpx.Response:
        requests_made.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(refused))
    for server in cluster.servers:
        readers = server.edition.terms.readers
        readers["url"] = functools.partial(read_url, client=client)
        readers["graph"] = functools.partial(read_graph, client=client)
    bus = cluster.server.bus
    settle(lambda: bus.client.pubsub_numsub(bus.key(CHANNEL))[0][1] >= len(cluster.servers))
    yield cluster
    client.close()


def synced(cluster, *, gone=None):
    """Every source set up synced as asked: no sync asked for left, and a last run for each (and, given, as many terms
    gone). The tests' clock stands still: a test moves it a second before it asks (test_routes.py's ``synced``)."""
    admin = as_admin(cluster)

    def done() -> bool:
        state = cluster.a.get(TERMS, headers=admin).json()
        sources = [s for s in state["sources"] if s["configured"]]
        ran = bool(sources) and all(s["asked"] is None and s["last"] for s in sources)
        return ran and (gone is None or state["gone"] == gone)

    settle(done, seconds=10)


@pytest.fixture
def gone(cluster):
    """A source synced twice, Halden Insurance gone from it the second time."""
    folder = cluster.server.data / TERM_FILES
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "customers-page.csv"
    path.write_text("company\nHalden Insurance\nKestrel Analytics\n")
    admin = as_admin(cluster)
    source = {"name": "customers", "kind": "file", "path": path.name, "mapping": {"company": "CLIENT"}}
    cluster.a.put(f"{TERMS}/sources", json={"sources": [source]}, headers=admin)
    # Every instance's copy of the sources has it (ee:settings): whichever takes the lease runs it when asked.
    settle(lambda: all(server.edition.terms.sources() for server in cluster.servers))
    for text, left in (("company\nHalden Insurance\nKestrel Analytics\n", 0), ("company\nKestrel Analytics\n", 1)):
        path.write_text(text)
        cluster.clock.advance(1)
        cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
        synced(cluster, gone=left)
    yield path
    path.unlink(missing_ok=True)


def etag_of(cluster):
    return cluster.a.get(f"{PAGE}/sources").json()["etag"]


def test_the_security_role_sees_the_state_and_the_terms_gone_and_removes_them(cluster, gone):
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    state = cluster.a.get(PAGE).json()
    assert state["gone"] == 1 and state["sources"][0]["name"] == "customers" and "Halden" not in str(state)
    [term] = cluster.b.get(f"{PAGE}/gone").json()["items"]
    assert term["value"] == "Halden Insurance"
    for whole in ({"all": True}, {"source": "customers"}, {"unreadable": True}):  # by their ids alone
        assert cluster.a.post(f"{PAGE}/remove", json=whole, headers=UI).status_code == 400
    done = cluster.a.post(f"{PAGE}/remove", json={"ids": [term["id"]]}, headers=UI)
    assert done.json() == {"removed": 1, "by_type": {"CLIENT": 1}}
    [entry] = cluster.server.audit.query(action="terms.removed")
    assert entry.actor == security.id
    assert cluster.a.post(f"{PAGE}/run", json={}, headers=UI).status_code == 202
    [entry] = cluster.server.audit.query(action="terms.sync.asked", actor=security.id)
    assert entry.details == {"source": None}


def test_a_person_without_the_security_role_is_refused_and_sees_no_term(cluster, gone):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    company_session(cluster, emily, "sso")
    for answer in (
        cluster.a.get(PAGE),
        cluster.a.get(f"{PAGE}/gone"),
        cluster.a.post(f"{PAGE}/run", json={}, headers=UI),
    ):
        assert answer.status_code == 403 and "Halden" not in answer.text
    assert cluster.a.get(f"{PAGE}/sources").status_code == 403  # terms.manage's alone ([D4])
    assert cluster.a.put(f"{PAGE}/secrets/customers", json={"value": "x"}, headers=UI).status_code == 403


def test_without_roles_in_the_license_nobody_reads_term_sync_in_the_dashboard(cluster, gone):
    """Fix round 1's C1: E3's gate lets everyone read an action outside ROLES_ONLY while roles do not decide; without
    roles in the license, neither the status, the gone list nor the page is open, the security role held or not."""
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    license(cluster, features=["locked-rules", "approvals", "reports", "siem", "term-sync", "sso"])  # no roles
    for person in (security, emily):
        company_session(cluster, person, "sso")
        for answer in (cluster.a.get(PAGE), cluster.a.get(f"{PAGE}/gone"), cluster.a.get(f"{PAGE}/sources")):
            assert answer.status_code == 403 and "Halden" not in answer.text
        assert "term-sync" not in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    assert cluster.a.get(f"{TERMS}/gone", headers=as_admin(cluster)).json()["total"] == 1  # the CLI's


def test_the_security_role_edits_the_sources_and_a_secret_from_the_page(cluster, gone, requests_made):  # [D4]
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    shown = cluster.a.get(f"{PAGE}/sources").json()
    assert shown["sources"][0]["name"] == "customers" and "customers" in shown["text"]
    edited = (
        "sources:\n"
        f"  - {{name: customers, kind: file, path: {gone.name}, mapping: {{company: CLIENT}}}}\n"
        "  - {name: colleagues, kind: graph, tenant: acme.onmicrosoft.com, client_id: 6f1c2b9e,"
        " mapping: {displayName: PERSON}}\n"
    )  # the editor's text, as a person types it
    saved = cluster.a.put(f"{PAGE}/sources", json={"text": edited, "etag": shown["etag"]}, headers=UI)
    assert saved.status_code == 200 and saved.json()["added"] == ["colleagues"]
    stale = cluster.a.put(f"{PAGE}/sources", json={"text": shown["text"], "etag": shown["etag"]}, headers=UI)
    assert stale.status_code == 409 and stale.json()["code"] == "sources-changed"
    synced(cluster)  # colleagues ran once, set up: no secret, so nothing was asked of Graph
    assert requests_made == []
    value = "graph-" + "client-secret-4b1d"
    etag = saved.json()["etag"]
    old = cluster.a.put(f"{PAGE}/secrets/colleagues", json={"value": value, "etag": shown["etag"]}, headers=UI)
    assert old.status_code == 409 and old.json() == {"error": SOURCES_CHANGED, "code": "sources-changed"}
    cluster.clock.advance(1)
    kept = cluster.a.put(f"{PAGE}/secrets/colleagues", json={"value": value, "etag": etag}, headers=UI)
    assert (kept.status_code, kept.json()) == (200, {"saved": "colleagues"})
    [entry] = cluster.server.audit.query(action="terms.secret.changed")
    assert entry.actor == security.id and value not in cluster.a.get(f"{PAGE}/sources").text
    # The ruling of 2026-11-06: the secret set asks a sync of its source in the same transaction, which runs at once.
    [asked] = cluster.server.audit.query(action="terms.sync.asked", actor=security.id)
    assert asked.details == {"source": "colleagues"}
    settle(lambda: any(host == "login.microsoftonline.com" for host, _ in requests_made), seconds=10)
    removed = cluster.a.request("DELETE", f"{PAGE}/secrets/colleagues", json={"etag": etag}, headers=UI)
    assert (removed.status_code, removed.json()) == (200, {"removed": "colleagues"})
    assert "Halden" not in str([e.details for e in cluster.server.audit.query()])


def test_a_persons_change_of_the_sources_or_a_secret_names_the_sources_it_was_made_from(cluster, gone):  # [D4]
    """Root's carry: the page sends the sources' etag with each change, and a person's change without one is refused,
    nothing changed: a secret is sealed for the source as its setter read it, or not at all."""
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    shown = cluster.a.get(f"{PAGE}/sources").json()
    for answer in (
        cluster.a.put(f"{PAGE}/sources", json={"sources": shown["sources"]}, headers=UI),
        cluster.a.put(f"{PAGE}/sources", json={"text": shown["text"]}, headers=UI),
        cluster.a.put(f"{PAGE}/secrets/customers", json={"value": "x"}, headers=UI),
        cluster.a.request("DELETE", f"{PAGE}/secrets/customers", headers=UI),
    ):
        assert answer.status_code == 400 and "etag" in answer.json()["error"], answer.text
    assert cluster.server.audit.query(action="terms.*", actor=security.id) == []


def test_a_secret_set_by_the_administration_asks_a_sync_of_its_source_at_once(cluster, requests_made):
    """The ruling of 2026-11-06: a source that failed no-secret runs once its secret is set, not up to 15 minutes later;
    the ask is written in the administration log before the secret's own entry (the call's, with its status)."""
    admin = as_admin(cluster)
    url = {"name": "projects", "kind": "url", "url": "https://intranet.acme.lan/api/projects",
           "headers": {"Authorization": "Bearer {secret}"}, "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    cluster.a.put(f"{TERMS}/sources", json={"sources": [url]}, headers=admin)
    synced(cluster)
    last = cluster.a.get(TERMS, headers=admin).json()["sources"][0]["last"]
    assert (last["outcome"], last["code"], requests_made) == ("failed", "no-secret", [])
    cluster.clock.advance(1)
    secret = "intranet-" + "token-7f3a"
    assert cluster.a.put(f"{TERMS}/secrets/projects", json={"value": secret}, headers=admin).status_code == 200
    [asked] = cluster.server.audit.query(action="terms.sync.asked")
    assert asked.details == {"source": "projects"} and asked.actor.startswith("admin:")
    [saved] = cluster.server.audit.query(action="terms.secret.changed")
    assert saved.details == {"saved": ["projects"], "removed": [], "status": 200}
    settle(lambda: ("intranet.acme.lan", f"Bearer {secret}") in requests_made, seconds=10)


def test_the_sources_say_where_each_connects_and_whom_a_directory_signs_in_as(cluster, gone):
    """The final review's M1: the page's secret dialog says where a source connects before it asks for the secret, as
    `latet server terms secret` does: each address as a scanner would aim it, and the DN a directory signs in as; a
    file connects nowhere."""
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    customers = {"name": "customers", "kind": "file", "path": gone.name, "mapping": {"company": "CLIENT"}}
    colleagues = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
                  "mapping": {"displayName": "PERSON"}}  # fmt: skip
    directory = {"name": "directory", "kind": "ldap", "url": "ldaps://DC1.acme.lan.", "bind": "CN=latet,DC=acme",
                 "base": "DC=acme", "mapping": {"cn": "PERSON"}}  # fmt: skip
    projects = {"name": "projects", "kind": "url", "url": "https://[fd00::5]:8443/api?k={secret}",
                "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    admin = as_admin(cluster)
    sources = [customers, colleagues, directory, projects]
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": sources}, headers=admin).status_code == 200
    assert cluster.b.get(f"{PAGE}/sources").json()["where"] == {
        "customers": {"addresses": [], "bind": None},
        "colleagues": {"addresses": ["https://graph.microsoft.com:443", "https://login.microsoftonline.com:443"],
                       "bind": None},
        "directory": {"addresses": ["ldaps://dc1.acme.lan:636"], "bind": "CN=latet,DC=acme"},
        "projects": {"addresses": ["https://[fd00::5]:8443"], "bind": None},
    }  # fmt: skip


def test_a_source_at_the_servers_own_loopback_is_the_commands_alone(cluster, gone):  # [D4], N2
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    etag = etag_of(cluster)
    loopback = [
        {"name": "local", "kind": "url", "url": "https://localhost:8443/a.json", "mapping": {"x": "CLIENT"}},
        {"name": "local", "kind": "ldap", "url": "ldap://127.0.0.1:1389", "start_tls": False,
         "base": "DC=acme", "mapping": {"cn": "PERSON"}},
    ]  # fmt: skip
    for source in loopback:
        refused = cluster.a.put(f"{PAGE}/sources", json={"sources": [source], "etag": etag}, headers=UI)
        assert refused.status_code == 403 and refused.json()["code"] == "by-command"
        assert ":8443" not in refused.text and ":1389" not in refused.text  # fixed words, never the address
    admin = as_admin(cluster)  # the command sets it, and its secret
    url = loopback[0] | {"headers": {"Authorization": "Bearer {secret}"}}
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": [url]}, headers=admin).status_code == 200
    assert cluster.a.put(f"{TERMS}/secrets/local", json={"value": "y"}, headers=admin).status_code == 200
    etag = etag_of(cluster)
    refused = cluster.a.put(f"{PAGE}/secrets/local", json={"value": "x", "etag": etag}, headers=UI)
    assert refused.status_code == 403 and refused.json()["code"] == "by-command"
    refused = cluster.a.request("DELETE", f"{PAGE}/secrets/local", json={"etag": etag}, headers=UI)
    assert refused.status_code == 403 and refused.json()["code"] == "by-command"
    kept = cluster.a.put(f"{PAGE}/sources", json={"sources": [url], "etag": etag}, headers=UI)
    assert kept.status_code == 200  # kept as the command set it: a save that leaves it as it is
    assert cluster.server.audit.query(action="terms.secret.changed", actor=security.id) == []


def test_a_person_never_takes_out_a_source_at_the_servers_own_loopback(cluster, gone):  # Task 13's review, Minor
    """Taken out from the page, the command's loopback source is refused (403 by-command) as a change of it is, nothing
    changed; the page may still take out or change the other sources while it keeps that one as it is, and the
    administration (`latet server terms sources set`) takes it out."""
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    admin = as_admin(cluster)
    customers = {"name": "customers", "kind": "file", "path": gone.name, "mapping": {"company": "CLIENT"}}
    local = {"name": "local", "kind": "url", "url": "https://localhost:8443/a.json", "mapping": {"x": "CLIENT"}}
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": [customers, local]}, headers=admin).status_code == 200
    etag = etag_of(cluster)
    for left in ([customers], []):
        refused = cluster.a.put(f"{PAGE}/sources", json={"sources": left, "etag": etag}, headers=UI)
        assert refused.status_code == 403 and refused.json()["code"] == "by-command"
        assert ":8443" not in refused.text
    renamed = cluster.a.put(f"{PAGE}/sources", json={"sources": [customers, local | {"name": "moved"}], "etag": etag},
                            headers=UI)  # fmt: skip
    assert renamed.status_code == 403 and renamed.json()["code"] == "by-command"
    # The final review's I1: kept under its name, it is not moved elsewhere nor made a source of another kind either.
    for moved in (
        local | {"url": "https://exports.acme.lan/a.json"},
        {"name": "local", "kind": "file", "path": gone.name, "mapping": {"company": "CLIENT"}},
    ):
        refused = cluster.a.put(f"{PAGE}/sources", json={"sources": [customers, moved], "etag": etag}, headers=UI)
        assert refused.status_code == 403 and refused.json()["code"] == "by-command", refused.text
        assert "exports.acme" not in refused.text and gone.name not in refused.text
    shown = cluster.a.get(f"{PAGE}/sources").json()  # nothing changed: the sources as the administration set them
    assert shown["etag"] == etag and [s["name"] for s in shown["sources"]] == ["customers", "local"]
    assert cluster.server.audit.query(action="terms.source.changed", actor=security.id) == []
    kept = cluster.a.put(f"{PAGE}/sources", json={"sources": [local], "etag": etag}, headers=UI)
    assert (kept.status_code, kept.json()["removed"]) == (200, ["customers"])  # the others are the page's
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": []}, headers=admin).status_code == 200
    assert cluster.a.get(f"{PAGE}/sources").json()["sources"] == []


def test_a_person_adds_or_moves_ten_destinations_an_hour_from_the_page(cluster, gone):  # [D4], N2
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "sso")
    for n in range(11):
        etag = etag_of(cluster)
        source = {"name": "projects", "kind": "url", "url": f"https://host{n}.acme.lan/a.json",
                  "headers": {"Authorization": "Bearer {secret}"},  # no secret set: a run stops at no-secret, no lookup
                  "mapping": {"code_name": "PROJECT"}}  # fmt: skip
        answer = cluster.a.put(f"{PAGE}/sources", json={"sources": [source], "etag": etag}, headers=UI)
        assert answer.status_code == (200 if n < 10 else 429), (n, answer.text)
    assert "host10" not in answer.text
    same = cluster.a.get(f"{PAGE}/sources").json()  # a save that moves nothing is not counted
    kept = {"sources": same["sources"], "etag": same["etag"]}
    assert cluster.a.put(f"{PAGE}/sources", json=kept, headers=UI).status_code == 200
    admin = as_admin(cluster)  # the administration's are not counted
    moved = [{**same["sources"][0], "url": "https://host11.acme.lan/a.json"}]
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": moved}, headers=admin).status_code == 200


def test_a_one_time_code_session_neither_reads_nor_edits_the_sources(cluster, gone):  # [D4]
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "code")
    for answer in (
        cluster.a.get(f"{PAGE}/sources"),
        cluster.a.put(f"{PAGE}/sources", json={"sources": []}, headers=UI),
        cluster.a.put(f"{PAGE}/secrets/customers", json={"value": "x"}, headers=UI),
        cluster.a.request("DELETE", f"{PAGE}/secrets/customers", headers=UI),
    ):
        assert answer.status_code == 403 and answer.json().get("code") == "company-sign-in"
        assert "customers-page" not in answer.text


def test_a_session_of_a_one_time_code_reads_the_state_and_neither_the_terms_nor_a_change(cluster, gone):
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)  # company_changes: sso, by default
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    company_session(cluster, security, "code")
    assert cluster.a.get(PAGE).status_code == 200
    for answer in (
        cluster.a.get(f"{PAGE}/gone"),
        cluster.b.get(f"{PAGE}/gone"),
        cluster.a.post(f"{PAGE}/run", json={}, headers=UI),
        cluster.a.post(f"{PAGE}/remove", json={"ids": ["0" * 24]}, headers=UI),
    ):
        assert answer.status_code == 403 and answer.json().get("code") == "company-sign-in"
        assert "Halden" not in answer.text
    assert "sign in with your company account" in cluster.a.get(f"{PAGE}/gone").json()["error"]


def test_while_the_license_is_paused_the_page_reads_and_changes_nothing(cluster, gone):
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    paused(cluster)
    company_session(cluster, security, "sso")  # signed in after the pause: sessions last twelve hours
    assert cluster.a.get(PAGE).json()["paused"] is True
    assert cluster.a.get(f"{PAGE}/gone").json()["total"] == 1  # what it holds can be read
    etag = etag_of(cluster)
    for answer in (
        cluster.a.post(f"{PAGE}/run", json={}, headers=UI),
        cluster.a.post(f"{PAGE}/remove", json={"ids": ["0" * 24]}, headers=UI),
        cluster.a.put(f"{PAGE}/sources", json={"sources": [], "etag": etag}, headers=UI),
    ):
        assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text


def test_me_lists_the_page_for_whoever_may_use_it(cluster):
    security = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com", Role.SECURITY)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    company_session(cluster, security, "sso")
    assert "term-sync" in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    company_session(cluster, emily, "sso")
    assert "term-sync" not in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    license(cluster, features=["locked-rules", "approvals", "reports", "siem", "sso", "roles"])  # no term sync
    company_session(cluster, security, "sso")
    assert "term-sync" not in cluster.a.get(f"{API}/me").json()["edition"]["pages"]


def test_latet_server_status_says_a_failing_source(cluster, gone):
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert (line.status, line.detail) == ("ok", "2 of 50,000 terms kept from 1 source")
    gone.unlink()  # the export is gone: the next run fails
    admin = as_admin(cluster)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)

    def failed() -> bool:
        return cluster.a.get(TERMS, headers=admin).json()["sources"][0]["last"]["outcome"] == "failed"

    settle(failed, seconds=10)
    cluster.clock.advance(12 * 60)
    lines = cluster.server.edition.checks()
    [line] = [c for c in lines if c.title == "Term sync"]
    assert line.status == "warn" and "customers: failed 12 minutes ago (" in line.detail
    assert "Halden" not in line.detail and gone.name not in line.detail


def test_latet_server_status_says_terms_behind_the_store_and_terms_that_cannot_be_opened(cluster, gone, monkeypatch):
    """The design security review's B-I1 and B-I2: an instance whose terms trail the store says so, and one that cannot
    open some of them says it as an error (it keeps the terms it had)."""
    sync = cluster.server.edition.terms
    state = sync.status()
    behind = {"loaded": 1, "store": 2, "seconds": 3 * TICK}
    monkeypatch.setattr(sync, "status", lambda: {**state, "behind": behind})
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and "version 1 while the server holds 2" in line.detail
    monkeypatch.setattr(sync, "status", lambda: {**state, "behind": {**behind, "seconds": TICK}})  # within two ticks
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert line.status == "ok"
    monkeypatch.setattr(sync, "status", lambda: {**state, "unreadable": 3})
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert line.status == "fail" and "3 synced terms cannot be opened" in line.detail


def test_latet_server_status_says_a_pause_a_type_the_policy_lets_through_and_a_state_it_cannot_read(
    cluster, gone, monkeypatch
):
    """R30, a pause, and a failure said by its type alone: never raised, never its words."""
    edition = cluster.server.edition
    policy = "latet_rules: 1\nexceptions: company\npolicies: {CLIENT: allow}\n"
    assert cluster.a.put(f"{ADMIN_EE}/policy", json={"policy": policy}, headers=as_admin(cluster)).status_code == 200
    settle(lambda: edition.policy.floor() is not None)
    [line] = [c for c in edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and "customers maps CLIENT, which the policy lets through" in line.detail
    paused(cluster)
    [line] = [c for c in edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and "paused by the license: the terms synced so far stay masked" in line.detail

    def broken():
        raise RuntimeError("postgresql://latet:hunter2@db.acme.lan/latet")

    monkeypatch.setattr(edition.terms, "status", broken)
    [line] = [c for c in edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and "RuntimeError" in line.detail and "hunter2" not in line.detail


def test_latet_server_status_says_a_license_without_term_sync_and_nothing_that_contradicts_it(cluster, gone):
    """Task 12's review, Minor 1: a license that lists locked rules and not term sync masks no synced term; the line
    says so, and not that a type the policy lets through is masked all the same."""
    edition = cluster.server.edition
    policy = "latet_rules: 1\nexceptions: company\npolicies: {CLIENT: allow}\n"
    assert cluster.a.put(f"{ADMIN_EE}/policy", json={"policy": policy}, headers=as_admin(cluster)).status_code == 200
    license(cluster, features=["locked-rules", "approvals", "reports", "siem", "sso", "roles"])  # no term sync
    settle(lambda: edition.policy.floor() is not None)
    [line] = [c for c in edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and "the license does not list term sync" in line.detail
    assert "masked all the same" not in line.detail and "lets through" not in line.detail


def test_no_term_sync_line_while_no_source_was_ever_set(cluster):
    assert [c for c in cluster.server.edition.checks() if c.title == "Term sync"] == []


def test_a_license_without_term_sync_while_terms_are_kept_says_they_stop(cluster, gone):
    admin_person = add_person(cluster, "Ada Lin", "ada.lin@acme.com", Role.ADMIN)
    company_session(cluster, admin_person, "sso")
    features = ["locked-rules", "approvals", "reports", "siem", "sso", "roles"]  # no term-sync
    without = cluster.signer.make(id="L-2027-0002", features=features).decode()
    answer = cluster.a.put(f"{API}/ee/license", json={"license": without}, headers=UI)
    assert answer.status_code == 409 and answer.json().get("terms") == "stop" and "policy" not in answer.json()
    assert "synced terms stop being masked" in answer.json()["error"] and "server's policy" not in answer.text
    assert cluster.server.edition.standing().license.id != "L-2027-0002"  # nothing changed
    done = cluster.a.put(f"{API}/ee/license", json={"license": without, "policy_stops": True}, headers=UI)
    assert done.status_code == 200, done.text
    [entry] = cluster.server.audit.query(action="license.installed", actor=admin_person.id)
    assert entry.details["terms"] == "stop"
