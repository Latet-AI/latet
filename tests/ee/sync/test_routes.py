"""Term sync's administration routes on a server: the sources and their secrets set with their entries in the
administration log, a sync asked for and answered at once, the terms gone listed and removed, each refusal in fixed
words, and nothing of a term or a secret in the log."""

import functools
import json

import httpx
import pytest

from latet.ee.settings import SOURCES_CHANGED, TERM_FILES
from latet.ee.sync import service
from latet.ee.sync.graph import read_graph
from latet.ee.sync.service import CHANNEL
from latet.ee.sync.sources import read_url

from ..people.server_support import ADMIN_EE, SERVER_TESTS, as_admin, end_trial, on_trial, settle

pytestmark = SERVER_TESTS
TERMS = f"{ADMIN_EE}/term-sync"


@pytest.fixture
def requests_made():
    """What the instances' url and Graph readers asked for, each request's host and Authorization header: none is
    reached (tests on 127.0.0.1)."""
    return []


@pytest.fixture
def cluster(cluster, requests_made):
    """E3's cluster, its url and Graph sources read through a transport that answers 503 here and notes the host each
    request was for and what it carried, so that nothing leaves the computer and a test sees whether a request was made
    at all; once every instance hears ee:terms (a sync asked for wakes them all)."""

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


@pytest.fixture
def export(cluster):
    folder = cluster.server.data / TERM_FILES
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "customers-routes.csv"
    path.write_text("company\nHalden Insurance\nKestrel Analytics\n")
    yield path
    path.unlink(missing_ok=True)


def customers(export):
    return {"name": "customers", "kind": "file", "path": export.name, "mapping": {"company": "CLIENT"}}


def entries(cluster, action):
    return cluster.server.audit.query(action=action)


def synced(cluster):
    """Every source set up synced as asked: no sync asked for left, and a last run for each. (The tests' clock stands
    still: a test moves it a second before it asks, so that its sync is asked for after the source's last run began.)"""

    def done() -> bool:
        sources = [s for s in cluster.a.get(TERMS, headers=as_admin(cluster)).json()["sources"] if s["configured"]]
        return bool(sources) and all(s["asked"] is None and s["last"] for s in sources)

    settle(done, seconds=10)


def test_the_sources_are_set_with_their_entry_and_a_source_taken_out_leaves_its_terms_gone(cluster, export):
    admin = as_admin(cluster)
    put = cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    assert put.status_code == 200, put.text
    assert {k: put.json()[k] for k in ("added", "removed", "changed")} == {
        "added": ["customers"],
        "removed": [],
        "changed": [],
    }
    [entry] = entries(cluster, "terms.source.changed")
    # The administration gate adds the call's status to its last entry (AdminGate.entry: {**details, "status": …}).
    assert entry.actor.startswith("admin:")
    assert entry.details == {"added": ["customers"], "removed": [], "changed": [], "fields": {}, "status": 200}
    cluster.clock.advance(1)
    assert cluster.a.post(f"{TERMS}/run", json={}, headers=admin).status_code == 202
    synced(cluster)
    assert cluster.b.put(f"{TERMS}/sources", json={"sources": []}, headers=admin).json()["removed"] == ["customers"]
    assert cluster.a.get(TERMS, headers=admin).json()["gone"] == 2  # still masked, no longer in a source


@pytest.fixture
def quick(monkeypatch):
    """Every instance looks at what is due each second rather than every TICK (30 s): asked for before the cluster, so
    that its threads wait so from their first look (test_server.py's)."""
    monkeypatch.setattr(service, "TICK", 1.0)


def test_a_source_taken_out_forgets_its_last_run_and_set_up_again_or_changed_runs_at_once(quick, cluster, export):
    """The final review's M4: the change that takes a source out forgets its last run in its transaction, and so does
    one that changes it or sets it up again, so that it runs within a minute as the docs say, never at its old run's
    schedule (the tests' clock stands still: that never comes)."""
    admin = as_admin(cluster)
    sync = cluster.server.edition.terms

    def shown():
        return next((s for s in cluster.a.get(TERMS, headers=admin).json()["sources"] if s["name"] == "customers"), {})

    def last():
        return shown().get("last")

    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    synced(cluster)
    first = last()
    cluster.clock.advance(1)
    taken = cluster.a.put(f"{TERMS}/sources", json={"sources": []}, headers=admin)
    assert taken.json()["removed"] == ["customers"] and "customers" not in sync.store.runs()
    assert last() is None  # listed while its terms are kept, no longer set up: no run of it shown
    cluster.clock.advance(1)
    cluster.b.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    settle(lambda: (run := last()) is not None and run["started"] > first["started"], seconds=10)
    again = last()
    cluster.clock.advance(1)
    changed = customers(export) | {"mapping": {"company": "PERSON"}}
    assert cluster.a.put(f"{TERMS}/sources", json={"sources": [changed]}, headers=admin).json()["changed"] == [
        "customers"
    ]
    settle(lambda: (run := last()) is not None and run["started"] > again["started"], seconds=10)
    assert shown()["by_type"] == {"PERSON": 2}


def test_an_ldap_source_set_on_an_instance_without_the_ldap_extra_is_said_where_it_is_saved_and_in_the_status(
    cluster, monkeypatch
):
    """The final review's M5: an instance whose install lacks Latet's `ldap` extra says so for each LDAP source set up,
    in the save's answer, the state's problems and `latet server status`, by the sources' names alone; it leaves their
    turns to the instances that have the extra."""
    from latet.ee.sync.records import SourceError

    def unreachable(source, secret):
        raise SourceError("unreachable", "the directory did not answer (LDAPSocketOpenError)")

    for server in cluster.servers:
        server.edition.terms.readers["ldap"] = unreachable  # nothing leaves the computer, whoever runs it
        monkeypatch.setattr(server.edition.terms, "installed", lambda kind: kind != "ldap")
    admin = as_admin(cluster)
    directory = {"name": "directory", "kind": "ldap", "url": "ldaps://dc1.acme.lan", "base": "OU=Staff,DC=acme,DC=lan",
                 "mapping": {"cn": "PERSON"}}  # fmt: skip
    said = "directory: this instance has no `ldap` extra, which LDAP needs"
    saved = cluster.a.put(f"{TERMS}/sources", json={"sources": [directory]}, headers=admin).json()
    assert any(p.startswith(said) for p in saved["problems"]), saved["problems"]
    assert any(p.startswith(said) for p in cluster.a.get(TERMS, headers=admin).json()["problems"])
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert line.status == "warn" and said in line.detail and "dc1.acme" not in line.detail
    monkeypatch.undo()  # the extra installed: nothing said of it
    assert not any("`ldap` extra" in p for p in cluster.a.get(TERMS, headers=admin).json()["problems"])
    [line] = [c for c in cluster.server.edition.checks() if c.title == "Term sync"]
    assert "`ldap` extra" not in line.detail


def test_a_sync_asked_for_answers_at_once_and_is_recorded(cluster, export):
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    cluster.clock.advance(1)
    asked = cluster.b.post(
        f"{TERMS}/run", json={"source": "customers"}, headers=admin
    )  # the sources as PostgreSQL has them
    assert (asked.status_code, asked.json()) == (202, {"asked": ["customers"]})
    assert entries(cluster, "terms.sync.asked")[0].details == {"source": "customers", "status": 202}
    assert cluster.a.post(f"{TERMS}/run", json={"source": "crm"}, headers=admin).status_code == 404
    synced(cluster)


def test_the_terms_gone_are_listed_to_the_administration_and_removed_with_counts_alone(cluster, export):
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    export.write_text("company\nKestrel Analytics\n")
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    settle(lambda: cluster.a.get(TERMS, headers=admin).json()["gone"] == 1, seconds=10)
    [term] = cluster.a.get(f"{TERMS}/gone", headers=admin).json()["items"]
    assert (term["value"], term["type"], term["source"]) == ("Halden Insurance", "CLIENT", "customers")
    assert cluster.a.post(f"{TERMS}/remove", json={}, headers=admin).status_code == 400  # say which
    removed = cluster.a.post(f"{TERMS}/remove", json={"all": True}, headers=admin).json()
    assert removed == {"removed": 1, "by_type": {"CLIENT": 1}}
    [entry] = entries(cluster, "terms.removed")
    assert entry.details == {"count": 1, "by_type": {"CLIENT": 1}, "source": None, "status": 200}
    log = json.dumps([e.details for e in cluster.server.audit.query()])
    assert "Halden" not in log and "Kestrel" not in log


GRAPH = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
         "mapping": {"displayName": "PERSON"}}  # fmt: skip


def test_a_secret_set_from_sources_read_before_a_change_is_refused(cluster):
    """The review's Minor 2: a secret is sealed for the source as its setter read it, or not at all. Set or removed with
    the etag of the sources read, it is refused 409 `sources-changed` once they changed since (compared under the
    settings' lock), nothing kept and no entry written."""
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [GRAPH]}, headers=admin)
    read = cluster.a.get(f"{TERMS}/sources", headers=admin).json()["etag"]
    moved = {**GRAPH, "tenant": "contoso.onmicrosoft.com"}  # another administrator, meanwhile
    now = cluster.b.put(f"{TERMS}/sources", json={"sources": [moved]}, headers=admin).json()["etag"]
    value = "graph-" + "client-secret-4b1d"
    stale = cluster.a.put(f"{TERMS}/secrets/colleagues", json={"value": value, "etag": read}, headers=admin)
    assert stale.status_code == 409 and stale.json() == {"error": SOURCES_CHANGED, "code": "sources-changed"}
    stale = cluster.a.request("DELETE", f"{TERMS}/secrets/colleagues", json={"etag": read}, headers=admin)
    assert stale.status_code == 409 and stale.json()["code"] == "sources-changed"
    assert entries(cluster, "terms.secret.changed") == []
    saved = cluster.a.put(f"{TERMS}/secrets/colleagues", json={"value": value, "etag": now}, headers=admin)
    assert (saved.status_code, saved.json()) == (200, {"saved": "colleagues"})
    removed = cluster.b.request("DELETE", f"{TERMS}/secrets/colleagues", json={"etag": now}, headers=admin)
    assert (removed.status_code, removed.json()) == (200, {"removed": "colleagues"})
    [entry, _saved] = entries(cluster, "terms.secret.changed")
    assert entry.details == {"saved": [], "removed": ["colleagues"], "status": 200}
    assert cluster.a.delete(f"{TERMS}/secrets/colleagues", headers=admin).status_code == 404  # none kept now


def test_a_secret_left_by_a_source_taken_out_is_removed_and_each_refusal_says_why(cluster):
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [GRAPH]}, headers=admin)
    cluster.a.put(f"{TERMS}/secrets/colleagues", json={"value": "graph-" + "client-secret-4b1d"}, headers=admin)
    file = {"name": "customers", "kind": "file", "path": "customers.csv", "mapping": {"company": "CLIENT"}}
    cluster.a.put(f"{TERMS}/sources", json={"sources": [file]}, headers=admin)  # colleagues taken out
    left = cluster.a.delete(f"{TERMS}/secrets/colleagues", headers=admin)
    assert (left.status_code, left.json()) == (200, {"removed": "colleagues"})
    none = cluster.a.put(f"{TERMS}/secrets/customers", json={"value": "anything"}, headers=admin)
    assert none.status_code == 409 and none.json()["error"] == "this source reads with no secret"
    cluster.a.put(f"{TERMS}/sources", json={"sources": [GRAPH]}, headers=admin)
    empty = cluster.a.put(f"{TERMS}/secrets/colleagues", json={"value": "   "}, headers=admin)
    assert empty.status_code == 400 and empty.json()["error"] == "a secret is not empty"
    large = "sources: []\n" + "# " + "x" * 70_000 + "\n"
    too_large = cluster.a.put(f"{TERMS}/sources", json={"text": large}, headers=admin)
    assert too_large.status_code == 413
    assert [e.details["removed"] for e in entries(cluster, "terms.secret.changed")] == [["colleagues"], []]


def test_a_secret_is_kept_for_its_source_alone_and_named_never_shown(cluster):
    admin = as_admin(cluster)
    graph = {"name": "colleagues", "kind": "graph", "tenant": "acme.onmicrosoft.com", "client_id": "6f1c2b9e",
             "mapping": {"displayName": "PERSON"}}  # fmt: skip
    cluster.a.put(f"{TERMS}/sources", json={"sources": [graph]}, headers=admin)
    value = "graph-" + "client-secret-4b1d"
    saved = cluster.a.put(f"{TERMS}/secrets/colleagues", json={"value": value}, headers=admin)
    assert (saved.status_code, saved.json()) == (200, {"saved": "colleagues"})
    assert entries(cluster, "terms.secret.changed")[0].details == {
        "saved": ["colleagues"],
        "removed": [],
        "status": 200,
    }
    assert cluster.a.put(f"{TERMS}/secrets/crm", json={"value": value}, headers=admin).status_code == 404
    assert value not in json.dumps([e.details for e in cluster.server.audit.query()])
    assert value not in cluster.a.get(f"{TERMS}/sources", headers=admin).text


def test_a_source_pointed_elsewhere_never_sends_its_secret(cluster, requests_made):
    """Fix round 1's M4: a secret set, then the source's address moved: the run fails no-secret, the secret read before
    any request, so nothing is sent anywhere."""
    admin = as_admin(cluster)
    url = {"name": "projects", "kind": "url", "url": "https://intranet.acme.lan/api/projects",
           "headers": {"Authorization": "Bearer {secret}"}, "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    cluster.a.put(f"{TERMS}/sources", json={"sources": [url]}, headers=admin)
    secret = "intranet-" + "token-7f3a"
    cluster.a.put(f"{TERMS}/secrets/projects", json={"value": secret}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)  # as it was set, the source reads with its secret
    assert ("intranet.acme.lan", f"Bearer {secret}") in requests_made
    moved = {**url, "url": "https://elsewhere.acme.lan/api/projects"}
    cluster.a.put(f"{TERMS}/sources", json={"sources": [moved]}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    last = cluster.a.get(TERMS, headers=admin).json()["sources"][0]["last"]
    assert (last["outcome"], last["code"]) == ("failed", "no-secret")
    assert "elsewhere.acme.lan" not in [host for host, _ in requests_made]  # nothing sent where it points now
    changes = [e.details for e in entries(cluster, "terms.source.changed")]
    assert {"projects": ["url"]} in [d["fields"] for d in changes]  # B-I3: the fields changed, never their values
    assert "elsewhere" not in json.dumps(changes)


def test_a_source_made_to_read_other_data_never_reads_it_with_its_secret(cluster, requests_made):
    """The design security review's B-I3: the same address, another mapping: the secret is bound to what the source
    reads too, so the run fails no-secret before anything is sent."""
    admin = as_admin(cluster)
    url = {"name": "projects", "kind": "url", "url": "https://intranet.acme.lan/api/projects",
           "headers": {"Authorization": "Bearer {secret}"}, "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    cluster.a.put(f"{TERMS}/sources", json={"sources": [url]}, headers=admin)
    cluster.a.put(f"{TERMS}/secrets/projects", json={"value": "intranet-" + "token-7f3a"}, headers=admin)
    other = {**url, "records": "data.salaries", "mapping": {"salary": "AMOUNT"}}
    put = cluster.a.put(f"{TERMS}/sources", json={"sources": [other]}, headers=admin)
    assert any("projects" in line and "secret" in line for line in put.json()["problems"])  # A-R7: said at the save
    sent_before = len(requests_made)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    last = cluster.a.get(TERMS, headers=admin).json()["sources"][0]["last"]
    assert (last["outcome"], last["code"]) == ("failed", "no-secret")
    assert len(requests_made) == sent_before  # the run asked for nothing
    fields = [e.details["fields"] for e in entries(cluster, "terms.source.changed")]
    assert {"projects": ["mapping", "records"]} in fields and "salar" not in json.dumps(fields)


def test_a_sync_asked_for_as_the_run_a_save_woke_began_is_taken_by_that_run(cluster, requests_made):
    """A sources save wakes a run; when that run begins at the time a sync is asked for and is kept before the ask
    commits, it takes the ask: no run is due for it, and it stayed asked until the source's next run at its schedule
    (the release's CI on server 3.14 met it in the test above)."""
    admin = as_admin(cluster)
    url = {"name": "projects", "kind": "url", "url": "https://intranet.acme.lan/api/projects",
           "headers": {"Authorization": "Bearer {secret}"}, "mapping": {"code_name": "PROJECT"}}  # fmt: skip
    cluster.a.put(f"{TERMS}/sources", json={"sources": [url]}, headers=admin)
    synced(cluster)
    cluster.clock.advance(1)
    terms = cluster.server.edition.terms
    terms.forget(["projects"])  # as a save does, and the run it wakes begins at the time of the ask below, kept first
    terms.tick()
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)


def test_the_terms_no_key_opens_are_removed_by_the_administration_alone(cluster, export):
    """B-R5, as root ruled it: B-I2's way out by hand, `latet server terms remove --unreadable`: the rows no key of the
    server opens among the terms no source gives any more, counted in the entry, never a term; one a source still
    gives is never removed (its next run seals it again); refused to a person from the page."""
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    with cluster.server.db.transaction() as conn:  # a value moved to another row: that row no key opens
        first, second = conn.execute("SELECT id, value FROM ee_terms ORDER BY id").fetchall()
        conn.execute("UPDATE ee_terms SET value = %s WHERE id = %s", (second[1], first[0]))
    kept = cluster.a.post(f"{TERMS}/remove", json={"unreadable": True}, headers=admin)
    assert kept.json() == {"removed": 0, "by_type": {}} and entries(cluster, "terms.removed") == []  # still given
    cluster.a.put(f"{TERMS}/sources", json={"sources": []}, headers=admin)  # the source taken out: its terms gone
    removed = cluster.a.post(f"{TERMS}/remove", json={"unreadable": True}, headers=admin)
    assert removed.json() == {"removed": 1, "by_type": {"CLIENT": 1}}
    [entry] = entries(cluster, "terms.removed")
    assert entry.details == {"count": 1, "by_type": {"CLIENT": 1}, "source": None, "unreadable": True, "status": 200}


def test_a_term_sealed_with_a_newer_master_key_is_never_removed_as_unreadable(cluster, export, monkeypatch):
    """B-R5: a gone row that names a newer master key version than the answering instance holds may open on another
    instance: nothing is removed, and the refusal says so in fixed words."""
    from latet.ee.sync import pgstore
    from latet.ee.sync.store import NewerKey

    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": []}, headers=admin)

    def newer(self, *, conn=None):
        raise NewerKey("a synced term is sealed with master key version 3, newer than this server's 2")

    monkeypatch.setattr(pgstore.PgTermStore, "remove_unreadable", newer)
    refused = cluster.a.post(f"{TERMS}/remove", json={"unreadable": True}, headers=admin)
    assert refused.status_code == 409 and refused.json()["code"] == "newer-key"
    assert "version 3" not in refused.text and "newer master key version" in refused.json()["error"]
    assert entries(cluster, "terms.removed") == [] and cluster.a.get(TERMS, headers=admin).json()["gone"] == 2


def test_the_generic_settings_route_sends_to_term_syncs_own(cluster):
    answer = cluster.a.put(f"{ADMIN_EE}/settings/term_sync", json={"sources": []}, headers=as_admin(cluster))
    assert answer.status_code == 409 and "latet server terms sources set" in answer.json()["error"]


def test_after_a_trial_ends_the_status_reads_and_nothing_changes(cluster, export):
    on_trial(cluster)  # a trial on its last day: the cluster's own license is a paid one
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    end_trial(cluster)
    assert cluster.a.get(TERMS, headers=admin).status_code == 200
    refused = cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    assert refused.status_code == 403 and refused.json().get("code") == "trial-ended"


def test_the_sources_are_shown_as_yaml_with_an_etag_and_a_stale_one_changes_nothing(cluster, export):
    """`latet server terms sources set` sends its file's text with the etag of the sources it read: a change made
    meanwhile is refused 409, nothing changed; a text that is not YAML says its line alone."""
    admin = as_admin(cluster)
    first = cluster.a.get(f"{TERMS}/sources", headers=admin).json()
    assert first["sources"] == [] and "sources: []" in first["text"]
    text = f"sources:\n  - {{name: customers, kind: file, path: {export.name}, mapping: {{company: CLIENT}}}}\n"
    put = cluster.b.put(f"{TERMS}/sources", json={"text": text, "etag": first["etag"]}, headers=admin)
    assert put.status_code == 200 and put.json()["added"] == ["customers"], put.text
    shown = cluster.a.get(f"{TERMS}/sources", headers=admin).json()
    assert shown["etag"] == put.json()["etag"] != first["etag"] and "name: customers" in shown["text"]
    stale = cluster.a.put(f"{TERMS}/sources", json={"text": "sources: []\n", "etag": first["etag"]}, headers=admin)
    assert stale.status_code == 409 and stale.json()["code"] == "sources-changed"
    assert [s["name"] for s in cluster.a.get(f"{TERMS}/sources", headers=admin).json()["sources"]] == ["customers"]
    wrong = cluster.a.put(f"{TERMS}/sources", json={"text": "sources:\n  - [Halden\n"}, headers=admin)
    assert wrong.status_code == 400 and "line" in wrong.json()["error"] and "Halden" not in wrong.text
    both = cluster.a.put(f"{TERMS}/sources", json={"text": "sources: []\n", "sources": []}, headers=admin)
    assert both.status_code == 400
    assert len(entries(cluster, "terms.source.changed")) == 1  # the refusals changed nothing, and wrote no entry


def test_a_license_without_term_sync_over_one_with_it_says_the_synced_terms_stop(cluster, export):
    """R29: a license change that switches term sync off while synced terms are kept says so in its entry, as one that
    ends the policy does."""
    admin = as_admin(cluster)
    cluster.a.put(f"{TERMS}/sources", json={"sources": [customers(export)]}, headers=admin)
    cluster.clock.advance(1)
    cluster.a.post(f"{TERMS}/run", json={}, headers=admin)
    synced(cluster)
    without = cluster.signer.make(id="L-2027-0002", features=["roles", "reports"]).decode()
    assert cluster.a.put(f"{ADMIN_EE}/license", json={"license": without}, headers=admin).status_code == 200
    [entry] = [e for e in entries(cluster, "license.installed") if e.target == "L-2027-0002"]
    assert entry.details["terms"] == "stop" and "policy" not in entry.details
    again = cluster.signer.make(id="L-2027-0003", features=["roles"]).decode()  # term sync was off already
    assert cluster.a.put(f"{ADMIN_EE}/license", json={"license": again}, headers=admin).status_code == 200
    [entry] = [e for e in entries(cluster, "license.installed") if e.target == "L-2027-0003"]
    assert "terms" not in entry.details


def test_every_action_term_sync_records_is_mapped_to_the_security_role():
    from latet.ee.events.admin import PERMISSIONS

    for action in ("terms.source.changed", "terms.secret.changed", "terms.removed", "terms.sync.asked"):
        assert PERMISSIONS[action] == "terms.manage"
