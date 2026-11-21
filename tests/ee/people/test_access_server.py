"""On a server: what each role reaches of the Enterprise routes, E1's and E2's included, on both instances, and what
/me says (ee/access.py through the edition); and the License page's install and remove, for the license's
administrators (the user's decision (a) of 2026-10-25)."""

import asyncio
from types import SimpleNamespace

from starlette.routing import Route

from latet.config import Config
from latet.ee.access import _Gate
from latet.ee.roles import Role

from .server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
    UI,
    add_person,
    as_admin,
    end_trial,
    license,
    on_trial,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS

POLICY = "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"


def test_me_names_the_roles_the_actions_and_the_pages_of_the_person_signed_in(cluster):
    sign_in_as(cluster, add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN))
    edition = cluster.a.get(f"{API}/me").json()["edition"]
    assert edition["roles"] == ["admin", "user"]
    assert {"people.manage", "keys.manage", "sign-in.edit", "license.manage", "audit.read"} <= set(edition["actions"])
    assert "policy.edit" not in edition["actions"]
    assert "people" in edition["pages"] and "policy" not in edition["pages"]


def test_everyone_reads_the_floor_and_only_policy_readers_get_its_page(cluster):
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
    assert cluster.a.get(f"{API}/ee/policy").status_code != 403
    assert "policy" not in cluster.a.get(f"{API}/me").json()["edition"]["pages"]
    assert cluster.a.get(f"{API}/ee/license").status_code == 403
    sign_in_as(cluster, add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.SECURITY))
    assert "policy" in cluster.a.get(f"{API}/me").json()["edition"]["pages"]  # E2 lists it, E3's filter keeps it


def test_the_license_page_is_its_administrators_and_a_person_with_no_role_changes_nothing(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
    refused = cluster.a.get(f"{API}/ee/license")
    assert refused.status_code == 403 and "license.manage" in refused.json()["error"]
    text = cluster.signer.make(users=500).decode()
    for answer in (  # the gate's refusal, before the route reads the body
        cluster.a.put(f"{API}/ee/license", json={"license": text}, headers=UI),
        cluster.a.request("DELETE", f"{API}/ee/license", json={"policy_stops": True}, headers=UI),
    ):
        assert answer.status_code == 403 and "license.manage" in answer.json()["error"]
    sign_in_as(cluster, emily)
    shown = cluster.a.get(f"{API}/ee/license").json()
    assert shown["state"] == "valid" and shown["license"]["users"] == 300  # still the one installed
    assert cluster.server.audit.query(action="license.*", actor=emily.id) == []


def test_an_administrator_installs_and_removes_the_license_from_the_dashboard_on_every_instance(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    text = cluster.signer.make(users=500).decode()
    unmarked = cluster.a.put(f"{API}/ee/license", json={"license": text})  # behind the dashboard's checks too
    assert unmarked.status_code == 403 and unmarked.json() == {"error": "missing X-Latet-UI header"}
    installed = cluster.a.put(f"{API}/ee/license", json={"license": text}, headers=UI)
    assert installed.status_code == 200, installed.text
    assert installed.json()["license"]["users"] == 500 and isinstance(installed.json()["users_in_use"], int)
    settle(lambda: cluster.b.get(f"{API}/ee/license").json()["license"]["users"] == 500)
    (entry,) = cluster.server.audit.query(action="license.installed", actor=emily.id)
    assert entry.details == {"users": 500, "expires": "2027-10-31", "trial": False}
    assert entry.address == "203.0.113.7"
    removed = cluster.b.request("DELETE", f"{API}/ee/license", headers=UI)  # through the other instance
    assert removed.status_code == 200 and removed.json()["removed"] is True, removed.text
    assert [e.actor for e in cluster.server.audit.query(action="license.removed")] == [emily.id]
    settle(lambda: cluster.a.get(f"{API}/ee/license").json()["state"] == "none")
    # Without a license nobody manages from the dashboard: `latet server license install` does it.
    again = cluster.a.put(f"{API}/ee/license", json={"license": cluster.signer.make().decode()}, headers=UI)
    assert again.status_code == 403 and cluster.server.edition.standing().license is None


def test_a_license_file_that_cannot_be_used_is_refused_and_nothing_changes(cluster):
    sign_in_as(cluster, add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN))
    for body in ({"license": "{}"}, {"licence": "x"}, ["x"]):
        answer = cluster.a.put(f"{API}/ee/license", json=body, headers=UI)
        assert answer.status_code == 400, answer.text
    assert cluster.a.put(f"{API}/ee/license", content=b"x" * (300 * 1024), headers=UI).status_code == 413
    removal = cluster.a.request("DELETE", f"{API}/ee/license", json=[True], headers=UI)  # not an object
    assert removal.status_code == 400 and "policy_stops" in removal.json()["error"]
    assert cluster.server.edition.standing().license.users == 300
    assert [entry.actor for entry in cluster.server.audit.query(action="license.installed")] == [BY_HAND]


def test_the_license_is_weighed_again_in_the_changes_transaction(cluster):
    # Instance A hears nothing and keeps its copy of the license (REFRESH_SECONDS): B removes it meanwhile.
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()
    sign_in_as(cluster, add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN))
    assert cluster.a.get(f"{API}/ee/license").json()["state"] == "valid"  # A's copy read
    cluster.servers[1].edition.remove_license(actor=BY_HAND)
    for answer in (
        cluster.a.put(f"{API}/ee/license", json={"license": cluster.signer.make().decode()}, headers=UI),
        cluster.a.request("DELETE", f"{API}/ee/license", headers=UI),
    ):
        assert answer.status_code == 403 and answer.json()["code"] == "not-manageable", answer.text
    assert cluster.servers[1].edition.standing().license is None  # nothing installed by the dashboard


def test_once_a_trial_ended_the_dashboard_neither_installs_nor_removes_a_license(cluster):
    on_trial(cluster)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    end_trial(cluster)
    sign_in_as(cluster, emily)
    for answer in (
        cluster.a.put(f"{API}/ee/license", json={"license": cluster.signer.make().decode()}, headers=UI),
        cluster.a.put(f"{API}/ee/license", content=b"x" * (300 * 1024), headers=UI),  # said before the body is read
        cluster.a.request("DELETE", f"{API}/ee/license", headers=UI),
    ):
        assert answer.status_code == 403 and answer.json()["code"] == "trial-ended", answer.text
    assert cluster.server.edition.standing().license.trial  # `latet server license install|remove` does it


def test_a_change_that_stops_the_servers_policy_is_made_only_once_confirmed(cluster):
    assert cluster.a.put(f"{ADMIN_EE}/policy", json={"policy": POLICY}, headers=as_admin(cluster)).status_code == 200
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    without = cluster.signer.make(features=["sso", "roles"]).decode()  # no locked rules: the policy would stop
    asked = cluster.a.put(f"{API}/ee/license", json={"license": without}, headers=UI)
    assert asked.status_code == 409 and asked.json()["policy"] == "stops", asked.text
    asked = cluster.a.request("DELETE", f"{API}/ee/license", headers=UI)
    assert asked.status_code == 409 and asked.json()["policy"] == "stops"
    assert cluster.server.edition.standing().license.users == 300
    assert cluster.server.audit.query(action="license.*", actor=emily.id) == []  # nothing written
    done = cluster.a.put(f"{API}/ee/license", json={"license": without, "policy_stops": True}, headers=UI)
    assert done.status_code == 200, done.text
    (entry,) = cluster.server.audit.query(action="license.installed", actor=emily.id)
    assert entry.details["policy"] == "stops"
    # The license now lists no locked rules: removing it stops nothing more, and needs no confirmation.
    assert cluster.a.request("DELETE", f"{API}/ee/license", headers=UI).status_code == 200


def test_a_role_revoked_through_one_instance_applies_at_the_next_request_on_the_other(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    assert cluster.b.get(f"{API}/ee/license").status_code != 403
    cluster.people.revoke(emily.id, Role.ADMIN, actor=BY_HAND)  # through instance A
    assert cluster.b.get(f"{API}/ee/license").status_code == 403


def test_every_enterprise_route_of_the_dashboard_is_under_a_rule(cluster):
    sign_in_as(cluster, add_person(cluster, "Emily Carter", "emily.carter@acme.com"))
    assert cluster.a.get(f"{API}/me").status_code == 200  # the person's space is built, its routes with it
    assert cluster.server.edition.access.unruled == set()


def test_each_route_the_server_mounts_is_under_the_rule_its_page_needs(cluster):
    dashboard = SimpleNamespace(
        guarded=lambda handler: handler, rt=SimpleNamespace(identity=None, config=Config()), _in_pool=asyncio.to_thread
    )
    ruled = {
        (route.path, tuple(sorted(route.methods))): route.app.rule.path  # type: ignore[attr-defined]
        for route in cluster.server.edition.dashboard_routes(dashboard)
        if isinstance(route, Route) and isinstance(route.app, _Gate)
    }
    people = {  # Task 8's (ee/people_api.py): each under its page's rule, `latet server people link` not served here
        ("/people", ("GET", "HEAD")),
        ("/people", ("POST",)),
        ("/people/{id}/code", ("POST",)),
        ("/people/{id}/disable", ("POST",)),
        ("/people/{id}/enable", ("POST",)),
        ("/groups", ("GET", "HEAD")),
        ("/groups", ("POST",)),
        ("/groups/{id}", ("PUT",)),
        ("/groups/{id}", ("DELETE",)),
        ("/groups/{id}/members", ("POST",)),
        ("/groups/{id}/members/{person}", ("DELETE",)),
        ("/roles", ("GET", "HEAD")),
        ("/roles", ("POST",)),
        ("/roles/{subject}/{role}", ("DELETE",)),
    }
    keys = {  # Task 9's (ee/keys.py): the keys one may open and those made; below, keys.manage's; the key's space
        ("/keys", ("GET", "HEAD")): "/ee/keys",
        ("/keys", ("POST",)): "/ee/keys",
        ("/keys/{id}/rotate", ("POST",)): "/ee/keys/*",
        ("/keys/{id}/disable", ("POST",)): "/ee/keys/*",
        ("/keys/{id}/enable", ("POST",)): "/ee/keys/*",
        ("/keys/{id}/controllers", ("POST",)): "/ee/keys/*",
        ("/keys/{id}/controllers/{subject}", ("DELETE",)): "/ee/keys/*",
        ("/keys/{key}/space/{rest:path}", ("DELETE", "GET", "HEAD", "POST", "PUT")): "/ee/keys/*/space",
    }
    sign_in = {  # Task 10's (ee/sign_in_api.py): sign-in.edit's, but the code anyone makes to connect a computer
        ("/sign-in", ("GET", "HEAD")): "/ee/sign-in",
        ("/sign-in", ("PUT",)): "/ee/sign-in",
        ("/sign-in/secret", ("PUT",)): "/ee/sign-in",
        ("/sign-in/secret", ("DELETE",)): "/ee/sign-in",
        ("/sign-in/test", ("POST",)): "/ee/sign-in",
        ("/sign-in/scim-tokens", ("POST",)): "/ee/sign-in",
        ("/sign-in/scim-tokens/{id}", ("DELETE",)): "/ee/sign-in",
        ("/sign-in/code", ("POST",)): "/ee/sign-in/code",
        ("/notices/told", ("POST",)): "/ee/notices/told",  # O6: anyone, of their own dashboard's notice
    }
    events = {  # E4's (ee/events/api.py, wired by its Task 11); the reports' settings and the skip: `latet server`'s
        ("/reports", ("GET", "HEAD")): "/ee/reports",
        ("/reports/export", ("GET", "HEAD")): "/ee/reports",
        ("/audit", ("GET", "HEAD")): "/ee/audit",
        ("/audit/export", ("GET", "HEAD")): "/ee/audit",
        ("/siem", ("GET", "HEAD")): "/ee/siem",
        ("/siem", ("PUT",)): "/ee/siem",
        ("/siem/secret", ("PUT",)): "/ee/siem",
        ("/siem/test", ("POST",)): "/ee/siem",
    }
    approvals = {  # E5's (ee/approvals_api.py, wired by its Task 7): one rule, the routes check who decides
        ("/approvals", ("GET", "HEAD")),
        ("/approvals/count", ("GET", "HEAD")),
        ("/approvals/history", ("GET", "HEAD")),
        ("/approvals/batches/{id}", ("GET", "HEAD")),
        ("/approvals/batches/{id}/images/{item}", ("GET", "HEAD")),
        ("/approvals/changes/{id}", ("POST",)),
        ("/approvals/batches/{id}", ("POST",)),
    }
    term_sync = {  # E6's (ee/sync/api.py, Task 12): one row, terms.manage's, its sources and their secrets with it
        ("/term-sync", ("GET", "HEAD")),
        ("/term-sync/gone", ("GET", "HEAD")),
        ("/term-sync/run", ("POST",)),
        ("/term-sync/remove", ("POST",)),
        ("/term-sync/sources", ("GET", "HEAD")),
        ("/term-sync/sources", ("PUT",)),
        ("/term-sync/secrets/{source}", ("PUT",)),
        ("/term-sync/secrets/{source}", ("DELETE",)),
    }
    assert ruled == {
        (f"{API}/ee/license", ("GET", "HEAD")): "/ee/license",
        (f"{API}/ee/license", ("DELETE", "PUT")): "/ee/license",
        (f"{API}/ee/policy", ("GET", "HEAD")): "/ee/policy",
        # Task 13's (ee/policy_api.py): the policy's editor, for policy.edit; its text a rule of its own
        (f"{API}/ee/policy/text", ("GET", "HEAD")): "/ee/policy/text",
        (f"{API}/ee/policy", ("PUT",)): "/ee/policy",
        (f"{API}/ee/policy", ("DELETE",)): "/ee/policy",
        **{(f"{API}/ee{path}", methods): "/ee/" + path.split("/")[1] for path, methods in people},
        **{(f"{API}/ee{path}", methods): rule for (path, methods), rule in keys.items()},
        **{(f"{API}/ee{path}", methods): rule for (path, methods), rule in sign_in.items()},
        **{(f"{API}/ee{path}", methods): rule for (path, methods), rule in events.items()},
        **{(f"{API}/ee{path}", methods): "/ee/approvals" for path, methods in approvals},
        **{(f"{API}/ee{path}", methods): "/ee/term-sync" for path, methods in term_sync},
    }
    assert len(cluster.server.edition.dashboard_routes(dashboard)) == len(ruled)  # every one of them behind the gate
    assert cluster.server.edition.access.unruled == set()  # none left out (a Mount) nor refused for want of a rule


def test_without_roles_in_the_license_the_pages_are_read_only_for_everyone(cluster):
    license(cluster, features=["locked-rules", "sso"])
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
    assert cluster.a.get(f"{API}/ee/license").status_code != 403
    edition = cluster.a.get(f"{API}/me").json()["edition"]
    assert edition["roles"] == [] and edition["actions"] == [] and "people" not in edition["pages"]
    assert {"license", "policy"} <= set(edition["pages"])  # everyone reads them (R23: policy_page's reads)
    changed = cluster.a.put(f"{API}/ee/license", json={"license": cluster.signer.make().decode()}, headers=UI)
    assert changed.status_code == 403 and "latet server" in changed.json()["error"]


def test_once_a_trial_ended_roles_still_decide_who_reads_and_nobody_reads_more_or_less(cluster):
    on_trial(cluster)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    end_trial(cluster)
    sign_in_as(cluster, daniel)
    assert cluster.a.get(f"{API}/ee/license").status_code == 403  # as before the end: no role, no License page
    assert cluster.a.get(f"{API}/me").json()["edition"]["roles"] == ["user"]  # everyone's, which holds no action
    sign_in_as(cluster, emily)
    me = cluster.a.get(f"{API}/me").json()["edition"]
    assert me["license"]["state"] == "trial-ended" and me["roles"] == ["admin", "user"] and "license" in me["pages"]
    assert cluster.b.get(f"{API}/ee/license").json()["state"] == "trial-ended"  # where a license is installed


def test_the_licenses_headcount_is_told_to_its_administrators_alone(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    # R19 (E1's "E3 gives them back to them"); E1's count is read again within REFRESH_SECONDS, so only its presence
    assert isinstance(cluster.a.get(f"{API}/ee/license").json()["users_in_use"], int)
    assert isinstance(cluster.a.get(f"{API}/me").json()["edition"]["license"]["users_in_use"], int)
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
    assert cluster.a.get(f"{API}/me").json()["edition"]["license"]["users_in_use"] is None
