"""The security role sets the server's policy from the Policy page: E2's ServerPolicy behind E3's gate, the person as
the actor, every space on every instance raised to it (ee/policy_api.py)."""

import logging

import psycopg
import pytest

from latet.api.rest import COOKIE
from latet.ee.policy_server import PgPolicyStore, StoredPolicy
from latet.ee.roles import Role

from .fake_provider import SSO
from .server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    HOST,
    SERVER_TESTS,
    UI,
    add_person,
    as_admin,
    end_trial,
    on_trial,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS

POLICY = "latet_rules: 1\nprofile: max\nterms: [{value: Project Falcon, type: PROJECT}]\n"
NONE = {"policy": None, "problems": [], "etag": None}
COMPANY_SIGN_IN = {
    "error": "sign in with your company account to change what applies to everyone",
    "code": "company-sign-in",
}
READ_BY_COMPANY_SIGN_IN = {
    "error": "sign in with your company account to read the policy's text",
    "code": "company-sign-in",
}


@pytest.fixture
def priya(cluster):
    """Priya Shah, of the security role, signed in to the dashboard."""
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.SECURITY)
    sign_in_as(cluster, priya)
    return priya


def _changes(cluster):
    return sorted(cluster.server.audit.query(action="policy.changed"), key=lambda entry: entry.id)


def _save(client, text, **more):
    """PUT /ee/policy as the editor sends it: the text, and the etag of the policy it replaces, as read now."""
    etag = client.get(f"{API}/ee/policy/text").json()["etag"]
    return client.put(f"{API}/ee/policy", headers=UI, json={"policy": text, "etag": etag, **more})


def _remove(client):
    """DELETE /ee/policy as the editor sends it: the etag of the policy it removes, as read now."""
    etag = client.get(f"{API}/ee/policy/text").json()["etag"]
    return client.request("DELETE", f"{API}/ee/policy", headers=UI, json={"etag": etag})


def test_the_security_role_sets_the_policy_and_removes_it_each_change_by_the_person(cluster, priya):
    saved = _save(cluster.a, POLICY)
    assert saved.status_code == 200, saved.text
    assert saved.json()["keys"] == ["profile", "terms"] and saved.json()["problems"] == []  # both instances apply it
    shown = cluster.b.get(f"{API}/ee/policy/text").json()
    stored = shown["policy"]
    assert (stored["text"], stored["set_by"], stored["in_force"]) == (POLICY, priya.id, True)
    assert shown["etag"] == stored["etag"] == saved.json()["etag"]
    assert _remove(cluster.a).json() == {"removed": True}
    assert cluster.b.get(f"{API}/ee/policy/text").json() == NONE
    set_, removed = _changes(cluster)
    assert [(entry.actor, entry.address) for entry in (set_, removed)] == [(priya.id, "203.0.113.7")] * 2
    assert set_.details["keys"] == ["profile", "terms"] and removed.details == {"removed": True}
    assert "Project Falcon" not in repr([set_.details, removed.details])


@pytest.mark.parametrize("roles", [(), (Role.AUDITOR,), (Role.ADMIN,)])
def test_whoever_has_no_policy_edit_neither_reads_the_text_nor_changes_the_policy(cluster, roles):
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", *roles))
    refused = cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": POLICY, "etag": None})
    assert refused.status_code == 403 and "policy.edit" in refused.json()["error"]
    assert cluster.a.delete(f"{API}/ee/policy", headers=UI).status_code == 403
    assert cluster.a.get(f"{API}/ee/policy/text").status_code == 403
    assert cluster.a.get(f"{API}/ee/policy").status_code == 200  # the floor, which every person reads
    assert _changes(cluster) == []


def test_a_policy_the_server_cannot_read_is_refused_with_its_reason_and_the_stored_one_stays(cluster, priya):
    assert _save(cluster.a, POLICY).status_code == 200
    refused = _save(cluster.a, "profile: [max")
    assert refused.status_code == 400 and "not valid YAML" in refused.json()["error"]
    assert cluster.a.put(f"{API}/ee/policy", headers=UI, json={"text": POLICY, "etag": None}).status_code == 400
    assert cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": POLICY}).status_code == 400  # no etag
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY
    assert len(_changes(cluster)) == 1


def test_a_policy_with_keys_left_out_whole_is_set_only_when_the_body_says_they_were_seen(cluster, priya):
    # R27, as E2's administration route (its security review's L-a): one malformed term leaves out every term.
    text = POLICY.replace("type: PROJECT}", "type: PROJECT, case_sensitive: often}")
    refused = _save(cluster.a, text)
    assert refused.status_code == 400 and "terms" in refused.json()["error"] and "Falcon" not in refused.text
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"] is None and _changes(cluster) == []
    done = _save(cluster.a, text, accept_notices=True)
    assert done.status_code == 200 and done.json()["keys"] == ["profile"] and done.json()["problems"] == []


def test_a_policy_set_or_removed_through_one_instance_reaches_a_space_on_the_other_at_once(cluster, priya):
    """Instance B's own looks are stopped: within settle's five seconds it learns the change only from the message
    each change publishes once it commits (``committed()``), the set and the removal alike."""
    cluster.servers[1].edition.policy.close()  # no look of its own (every 10 s): only ee:policy tells it
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    sign_in_as(cluster, daniel)
    assert cluster.b.get(f"{API}/me").json()["profile"] == "balanced"  # his space, built on instance B
    sign_in_as(cluster, priya)
    assert _save(cluster.a, POLICY).status_code == 200
    sign_in_as(cluster, daniel)
    settle(lambda: cluster.b.get(f"{API}/me").json()["profile"] == "max")  # B heard it on ee:policy: in use, raised
    sign_in_as(cluster, priya)
    assert _remove(cluster.a).json() == {"removed": True}
    sign_in_as(cluster, daniel)
    settle(lambda: cluster.b.get(f"{API}/me").json()["profile"] == "balanced")  # heard again: his own settings


def test_once_a_trial_ended_the_policy_is_frozen_read_and_never_changed_from_the_page(cluster):
    on_trial(cluster)
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.SECURITY)
    sign_in_as(cluster, priya)
    assert _save(cluster.a, POLICY).status_code == 200
    end_trial(cluster)
    for refused in (
        _save(cluster.a, "latet_rules: 1\nprofile: balanced\n"),
        cluster.a.delete(f"{API}/ee/policy", headers=UI),
    ):
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY  # frozen, in force (E2's R2)
    assert len(_changes(cluster)) == 1


# --- what else guards it -----------------------------------------------------------------------------------------------


def _session(cluster, person, via):
    """The person's dashboard session, opened as ``via`` says: `latet dashboard`'s one-time code (``code``), or the
    company's single sign-on (``sso``)."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(person.id, via=via), domain=HOST, path="/_latet")


def _company_changes(cluster, value):
    """The sign-in settings with ``company_changes`` set to ``value`` (None: no single sign-on set up at all), known to
    both instances."""
    cluster.server.edition.settings_store.set_section(
        "sso", None if value is None else {**SSO, "company_changes": value}, actor=BY_HAND
    )

    def known(server):
        sso = server.edition.settings().sso
        return sso is None if value is None else sso is not None and sso.company_changes == value

    settle(lambda: all(known(server) for server in cluster.servers))


def test_the_policys_text_and_its_changes_take_a_company_sign_in_while_the_server_asks_for_one(cluster):
    """Decision (c), H2: the policy applies to everyone, and its text holds the company's terms and values in clear.
    While `company_changes: sso`, a session `latet dashboard`'s one-time code opened (which an agent at the person's
    computer can open) reads the Policy page's floor, which holds no value, and neither reads the policy's text nor
    sets nor removes the policy; a session the company sign-in opened does. With `any-session`, or no single sign-on
    set up, a code session does too."""
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.SECURITY)
    _company_changes(cluster, "sso")
    _session(cluster, priya, "code")
    assert cluster.a.get(f"{API}/ee/policy").status_code == 200  # the floor: names and counts, never a value
    refused = cluster.a.get(f"{API}/ee/policy/text")
    assert refused.status_code == 403 and refused.json() == READ_BY_COMPANY_SIGN_IN, refused.text
    for refused in (
        cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": POLICY, "etag": None}),
        cluster.a.put(f"{API}/ee/policy", headers=UI, content=b"x" * (5 * 1024 * 1024)),  # before the body is read
        cluster.a.delete(f"{API}/ee/policy", headers=UI),
    ):
        assert refused.status_code == 403 and refused.json() == COMPANY_SIGN_IN, refused.text
    assert _changes(cluster) == []
    _session(cluster, priya, "sso")
    assert cluster.a.get(f"{API}/ee/policy/text").json() == NONE
    assert _save(cluster.a, POLICY).status_code == 200
    assert [entry.actor for entry in _changes(cluster)] == [priya.id]
    _company_changes(cluster, "any-session")
    _session(cluster, priya, "code")
    assert cluster.a.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY
    _company_changes(cluster, None)  # no single sign-on: any session of the security role
    assert cluster.a.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY
    assert _remove(cluster.a).json() == {"removed": True}


def test_a_save_replaces_only_the_policy_it_was_read_from(cluster, priya):
    """Someone else's change made after the text was read, from the page or from `latet server policy set`, is never
    overwritten unseen: the save answers 409 in fixed words and changes nothing; read again, it is made."""
    seen = cluster.a.get(f"{API}/ee/policy/text").json()["etag"]  # none stored yet
    assert cluster.b.put(f"{ADMIN_EE}/policy", headers=as_admin(cluster), json={"policy": POLICY}).status_code == 200
    text = "latet_rules: 1\nprofile: balanced\n"
    stale = cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": text, "etag": seen})
    assert stale.status_code == 409 and stale.json() == {
        "error": "not changed: the policy was changed since its text was read here: read it again, then change it",
        "code": "policy-changed",
    }
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY and len(_changes(cluster)) == 1
    other = cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": text, "etag": "0" * 64})
    assert other.status_code == 409 and len(_changes(cluster)) == 1
    assert _save(cluster.a, text).status_code == 200  # read again: replaces the one it was read from
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == text
    for wrong in ({"policy": text}, {"policy": text, "etag": 7}, {"policy": text, "etag": ""}):
        assert cluster.a.put(f"{API}/ee/policy", headers=UI, json=wrong).status_code == 400, wrong
    assert len(_changes(cluster)) == 2


def _another_change_waiting_a_moment(server):
    """`latet server policy set` on another instance, which waits 200 ms at most for what holds the policy."""
    stored = StoredPolicy("latet_rules: 1\nprofile: balanced\n", "1" * 64, 1_791_191_520.0, BY_HAND)
    with server.db.transaction() as other:
        other.execute("SET LOCAL lock_timeout = '200ms'")
        PgPolicyStore(server.db, server.master).save(stored, conn=other)


def test_a_save_holds_off_every_other_change_of_the_policy_until_it_commits(cluster):
    """The etag a save checks stays the stored one until the save commits: a change on any instance, a policy stored
    or none, waits for it (here: gives up after 200 ms), so nothing lands between the check and the write."""
    for stored in (None, "1" * 64):
        with cluster.server.db.transaction() as conn:
            assert cluster.server.edition.policy.held_etag(conn) == stored
            with pytest.raises(psycopg.errors.LockNotAvailable):
                _another_change_waiting_a_moment(cluster.servers[1])
        _another_change_waiting_a_moment(cluster.servers[1])  # the save's transaction ended: it goes through


def test_a_stored_policy_the_server_cannot_read_is_replaced_or_removed_from_the_page(cluster, priya):
    """A policy stored that the server cannot read (made by a newer release, sealed with another master key) is shown
    with its problem and no text, and the etag that replaces it: the security role replaces or removes it."""
    newer = StoredPolicy("latet_rules: 99\nprofile: max\n", "e" * 64, 1_791_191_520.0, BY_HAND)
    PgPolicyStore(cluster.server.db, cluster.server.master).save(newer)
    shown = cluster.a.get(f"{API}/ee/policy/text").json()
    assert shown["policy"] is None and len(shown["problems"]) == 1 and shown["etag"] == "e" * 64
    stale = cluster.a.put(f"{API}/ee/policy", headers=UI, json={"policy": POLICY, "etag": None})
    assert stale.status_code == 409  # none was read: it would overwrite a policy unseen
    assert _save(cluster.a, POLICY).status_code == 200
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == POLICY
    PgPolicyStore(cluster.server.db, cluster.server.master).save(newer)
    assert _remove(cluster.a).json() == {"removed": True}
    assert cluster.b.get(f"{API}/ee/policy/text").json() == NONE


def test_the_license_is_weighed_again_in_the_changes_transaction(cluster, priya):
    """Instance A hears nothing and keeps its copy of the license (every feature): B installs one without locked rules
    meanwhile. A change from A is refused in its transaction, as the license is there, and nothing changes."""
    assert cluster.a.put(f"{ADMIN_EE}/policy", headers=as_admin(cluster), json={"policy": POLICY}).status_code == 200
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()
    assert cluster.a.get(f"{API}/ee/policy/text").status_code == 200  # A's copy read: locked rules listed
    cluster.servers[1].edition.install_license(cluster.signer.make(features=["sso", "roles"]), actor=BY_HAND)
    for refused in (
        _save(cluster.a, "latet_rules: 1\nprofile: balanced\n"),
        _remove(cluster.a),
    ):
        assert refused.status_code == 403 and refused.json()["feature"] == "locked-rules", refused.text
    assert cluster.b.get(f"{ADMIN_EE}/policy", headers=as_admin(cluster)).json()["policy"]["text"] == POLICY
    assert len(_changes(cluster)) == 1  # the administration token's alone


def test_a_body_past_twice_a_rules_files_size_is_refused_unread(cluster, priya):
    refused = cluster.a.put(f"{API}/ee/policy", headers=UI, content=b"x" * (4 * 1024 * 1024 + 1))
    assert refused.status_code == 413 and refused.json() == {"error": "larger than a rules file may be"}
    assert cluster.a.get(f"{API}/ee/policy/text").json()["policy"] is None and _changes(cluster) == []


def test_a_database_that_does_not_answer_is_said_in_fixed_words_and_by_its_type(cluster, priya, monkeypatch, caplog):
    def unanswered():
        raise ConnectionError("connection to 10.0.0.5 refused")

    monkeypatch.setattr(cluster.server.edition.policy, "admin_view", unanswered)
    with caplog.at_level(logging.WARNING, logger="latet.ee.policy_api"):
        answer = cluster.a.get(f"{API}/ee/policy/text")
    assert (answer.status_code, answer.headers["retry-after"]) == (503, "1")
    assert answer.json() == {"error": "the server's policy cannot be read now: try again"}
    assert "ConnectionError" in caplog.text and "10.0.0.5" not in caplog.text + answer.text


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------


def test_a_removal_takes_away_only_the_policy_it_was_read_from(cluster, priya):
    """As a save: a policy set after its text was read, from the page or from `latet server policy set`, is never
    removed unseen (409 in fixed words, nothing changed); a body that names no etag is refused; read again, it goes."""
    assert _save(cluster.a, POLICY).status_code == 200
    seen = cluster.a.get(f"{API}/ee/policy/text").json()["etag"]
    text = "latet_rules: 1\nprofile: balanced\n"
    assert cluster.b.put(f"{ADMIN_EE}/policy", headers=as_admin(cluster), json={"policy": text}).status_code == 200
    for stale in ({"etag": seen}, {"etag": None}):
        refused = cluster.a.request("DELETE", f"{API}/ee/policy", headers=UI, json=stale)
        assert refused.status_code == 409 and refused.json() == {
            "error": "not changed: the policy was changed since its text was read here: read it again, then change it",
            "code": "policy-changed",
        }
    for wrong in ({}, {"etag": 7}, {"etag": ""}, {"etag": "e" * 129}, [seen]):
        refused = cluster.a.request("DELETE", f"{API}/ee/policy", headers=UI, json=wrong)
        assert refused.status_code == 400 and "etag" in refused.json()["error"], wrong
    unnamed = cluster.a.request("DELETE", f"{API}/ee/policy", headers=UI)  # no body: as the page sent it before
    assert unnamed.status_code == 400 and seen not in unnamed.text
    assert cluster.b.get(f"{API}/ee/policy/text").json()["policy"]["text"] == text and len(_changes(cluster)) == 2
    assert _remove(cluster.a).json() == {"removed": True}  # read again: removes the one it was read from
    assert cluster.b.get(f"{API}/ee/policy/text").json() == NONE
    assert cluster.a.request("DELETE", f"{API}/ee/policy", headers=UI, json={"etag": None}).json() == {"removed": False}


def test_the_texts_company_sign_in_is_weighed_on_the_settings_postgresql_has(cluster, priya):
    """Instance A hears nothing and keeps its copy of the sign-in settings (any session may read the text) while B sets
    `company_changes: sso`: A's read of the text weighs the settings as PostgreSQL has them, never its copy."""
    _company_changes(cluster, "any-session")
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()
    assert cluster.server.edition.settings().sso.company_changes == "any-session"  # A's copy
    cluster.servers[1].edition.settings_store.set_section(
        "sso", {**SSO, "company_changes": "sso"}, actor=BY_HAND
    )  # told on ee:settings, which A no longer hears
    assert cluster.server.edition.settings().sso.company_changes == "any-session"  # A's copy, still
    _session(cluster, priya, "code")
    refused = cluster.a.get(f"{API}/ee/policy/text")
    assert refused.status_code == 403 and refused.json() == READ_BY_COMPANY_SIGN_IN, refused.text
    _session(cluster, priya, "sso")
    assert cluster.a.get(f"{API}/ee/policy/text").json() == NONE
