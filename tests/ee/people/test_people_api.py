"""People, groups and roles on the People and groups page of a server's dashboard, and the same routes for
`latet server` with the administration token (ee/people_api.py, ee/routes.py)."""

import psycopg
import pytest

from latet.api.rest import COOKIE
from latet.ee import people_api
from latet.ee.roles import Role
from latet.ee.routes import in_their_name

from .fake_provider import SSO
from .server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    HOST,
    SERVER_TESTS,
    UI,
    URL,
    add_person,
    as_admin,
    end_trial,
    on_trial,
    paused,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS

COMPANY_SIGN_IN = {
    "error": "sign in with your company account to change what applies to everyone",
    "code": "company-sign-in",
}


@pytest.fixture
def emily(cluster):
    """Emily Carter, administrator, signed in to the dashboard."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    return emily


def _daniel(cluster):
    return add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")


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


def _license_text(cluster):
    return cluster.signer.make(users=500).decode()


def test_an_administrator_sees_the_people_their_groups_and_their_roles(cluster, emily):
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    leads = cluster.people.add_group("Payments leads", actor=BY_HAND)
    cluster.people.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    cluster.people.add_member(leads.id, priya.id, actor=BY_HAND)
    answer = cluster.a.get(f"{API}/ee/people").json()
    assert answer["enabled"] == 2
    view = next(person for person in answer["people"] if person["name"] == "Priya Shah")
    assert view["groups"] == [{"id": leads.id, "name": "Payments leads", "source": "local"}]
    assert view["roles"] == []
    assert view["through"] == [{"role": "group-lead", "group": {"id": leads.id, "name": "Payments leads"}}]
    assert (view["linked"], view["scim_managed"], view["disabled_by"]) == (False, False, None)
    assert next(person for person in answer["people"] if person["id"] == emily.id)["roles"] == ["admin"]


def test_someone_without_the_admin_role_reaches_neither_people_groups_nor_roles(cluster):
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.SECURITY))
    assert cluster.a.get(f"{API}/ee/people").status_code == 403
    assert cluster.a.get(f"{API}/ee/groups").status_code == 403
    grant = {"subject": "daniel.okafor@acme.com", "role": "admin"}
    assert cluster.a.post(f"{API}/ee/roles", headers=UI, json=grant).status_code == 403


def test_an_administrator_adds_a_person_and_gets_the_command_that_connects_their_computer(cluster, emily):
    r = cluster.a.post(f"{API}/ee/people", headers=UI, json={"name": "Tom Becker", "email": "tom.becker@acme.com"})
    assert r.status_code == 201
    answer = r.json()
    # E0c's shape (F14): the command names the server, and the code is shown apart, out of a shell's history.
    assert answer["command"] == f"latet connect {URL}" and answer["code"] not in answer["command"]
    tom = answer["person"]["id"]
    again = cluster.a.post(f"{API}/ee/people/{tom}/code", headers=UI).json()  # before any computer of his connected
    assert again["code"] != answer["code"] and again["command"] == f"latet connect {URL}"
    assert cluster.server.identities.enrol(answer["code"], "laptop") is None  # the code before stops
    assert cluster.server.identities.enrol(again["code"], "laptop") is not None
    taken = cluster.a.post(f"{API}/ee/people", headers=UI, json={"name": "Tom B.", "email": "Tom.Becker@acme.com"})
    assert taken.status_code == 409  # E0b's EmailTaken: one enabled person per e-mail


def test_a_code_is_made_for_nobody_disabled_and_from_the_page_only_for_someone_who_never_connected(cluster, emily):
    daniel = _daniel(cluster)
    cluster.server.identities.disable(daniel.id)
    for refused in (
        cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI),
        cluster.a.post(f"{ADMIN_EE}/people/{daniel.id}/code", headers=as_admin(cluster)),
    ):
        assert refused.status_code == 409 and "disabled" in refused.json()["error"]  # F13
    cluster.server.identities.enable(daniel.id)
    made = cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI)
    assert made.status_code == 200 and made.json()["person"] == {"id": daniel.id, "name": "Daniel Okafor"}
    [entry] = cluster.server.audit.query(action="person.code.made")  # O6: what his next visit is told
    assert (entry.actor, entry.target) == (emily.id, daniel.id) and made.json()["code"] not in repr(entry)
    assert cluster.server.identities.enrol(made.json()["code"], "laptop") is not None
    # A computer of his is connected: a code from the page could connect another one he never sees (M4).
    refused = cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI)
    assert refused.status_code == 409 and "latet server people code" in refused.json()["error"]
    assert cluster.a.post(f"{ADMIN_EE}/people/{daniel.id}/code", headers=as_admin(cluster)).status_code == 200


def test_roles_are_given_to_people_and_to_groups_and_taken_back(cluster, emily):
    daniel = _daniel(cluster)
    cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Security team"})
    team = cluster.people.group("Security team")
    given = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": "Security team", "role": "security"})
    assert given.json() == {"roles": ["security"], "subject": {"kind": "group", "id": team.id, "name": "Security team"}}
    joined = cluster.a.post(f"{API}/ee/groups/{team.id}/members", headers=UI, json={"person": "daniel.okafor@acme.com"})
    assert joined.json() == {"ok": True, "person": {"id": daniel.id, "name": "Daniel Okafor"}}  # whom it named
    assert Role.SECURITY in cluster.people.roles_of(daniel.id)
    assert cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": daniel.id, "role": "user"}).status_code == 400
    taken = cluster.a.delete(f"{API}/ee/roles/{team.id}/security", headers=UI)
    assert taken.json()["roles"] == [] and cluster.people.roles_of(daniel.id) == {Role.USER}
    grants = cluster.a.get(f"{API}/ee/roles").json()["grants"]
    assert grants == [{"subject": {"kind": "person", "id": emily.id, "name": "Emily Carter"}, "role": "admin"}]


def test_a_person_is_named_by_e_mail_only_when_that_e_mail_was_given_by_hand_or_by_scim(cluster, emily):
    """H1: the e-mail of a person a sign-in made came in an ID token, and a user name can read as another's e-mail."""
    sam, _code = cluster.server.identities.add_person("Sam Rivera", "sam.rivera@acme.com", source="oidc")
    grant = {"subject": "sam.rivera@acme.com", "role": "auditor"}
    for refused in (
        cluster.a.post(f"{API}/ee/roles", headers=UI, json=grant),
        cluster.a.post(f"{ADMIN_EE}/roles", headers=as_admin(cluster), json=grant),
    ):
        assert refused.status_code == 409 and "by their id" in refused.json()["error"]
    by_id = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": sam.id, "role": "auditor"})
    assert by_id.json()["subject"] == {"kind": "person", "id": sam.id, "name": "Sam Rivera"}
    cluster.people.link(sam.id, user_name="sam.rivera", actor=BY_HAND)
    by_user_name = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": "sam.rivera", "role": "security"})
    assert by_user_name.json()["subject"]["id"] == sam.id
    # Priya's user name at the provider reads as Emily's e-mail: neither is chosen.
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="emily.carter@acme.com", actor=BY_HAND)
    named = {"subject": "Emily.Carter@acme.com", "role": "security"}
    ambiguous = cluster.a.post(f"{API}/ee/roles", headers=UI, json=named)
    assert ambiguous.status_code == 409 and "by their id" in ambiguous.json()["error"]
    assert cluster.people.given(priya.id) == frozenset() and cluster.people.given(emily.id) == {Role.ADMIN}


def test_a_person_scim_manages_is_named_by_their_id_alone(cluster, emily):
    """The re-review's R2: a SCIM token sets every value that names a person SCIM manages (user name, e-mail, name), so
    a leaked one could move them to an impostor account that a body naming the person by e-mail or user name then
    reaches. Such a person is named by their id alone, and no refusal says whom the name found; a person added by hand
    is still named by e-mail, until they are handed to SCIM."""
    laura, _code = cluster.server.identities.add_person("Laura Bianchi", "laura.bianchi@acme.com", source="scim")
    cluster.people.link(laura.id, source="scim", user_name="laura.bianchi", scim_managed=True, actor=BY_HAND)
    for subject in ("laura.bianchi@acme.com", "LAURA.BIANCHI"):
        grant = {"subject": subject, "role": "auditor"}
        for refused in (
            cluster.a.post(f"{API}/ee/roles", headers=UI, json=grant),
            cluster.a.post(f"{ADMIN_EE}/roles", headers=as_admin(cluster), json=grant),
        ):
            assert refused.status_code == 409 and "by their id" in refused.json()["error"]
            assert "SCIM" in refused.json()["error"] and laura.id not in refused.text
    team = cluster.people.add_group("Platform", actor=BY_HAND)
    joined = cluster.a.post(f"{API}/ee/groups/{team.id}/members", headers=UI, json={"person": "laura.bianchi"})
    assert joined.status_code == 409 and cluster.people.given(laura.id) == frozenset()
    by_id = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": laura.id, "role": "auditor"})
    assert by_id.json()["subject"] == {"kind": "person", "id": laura.id, "name": "Laura Bianchi"}
    daniel = _daniel(cluster)
    named = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": "daniel.okafor@acme.com", "role": "auditor"})
    assert named.json()["subject"]["id"] == daniel.id
    cluster.people.link(daniel.id, source="scim", user_name="daniel.okafor", scim_managed=True, actor=BY_HAND)
    handed = cluster.a.post(
        f"{API}/ee/roles", headers=UI, json={"subject": "daniel.okafor@acme.com", "role": "security"}
    )
    assert handed.status_code == 409 and Role.SECURITY not in cluster.people.roles_of(daniel.id)


@pytest.mark.parametrize(
    "look_alike", ["\N{KELVIN SIGN}ate.king@acme.com", "kate.k\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}ng@acme.com"]
)
def test_a_name_written_with_a_look_alike_names_nobody(cluster, emily, look_alike):
    """The final review's F3: a body's name is compared with A to Z's case aside and nothing else folded, a user name and
    an e-mail alike: a look-alike of Kate's user name and e-mail names nobody, and her own, in capitals, still names
    her."""
    kate = add_person(cluster, "Kate King", "kate.king@acme.com")
    cluster.people.link_person(kate.id, user_name="kate.king@acme.com", actor=BY_HAND)
    for refused in (
        cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": look_alike, "role": "admin"}),
        cluster.a.post(f"{ADMIN_EE}/roles", headers=as_admin(cluster), json={"subject": look_alike, "role": "admin"}),
    ):
        assert refused.status_code == 404, refused.text
    assert cluster.people.given(kate.id) == frozenset()
    named = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": "KATE.KING@acme.com", "role": "auditor"})
    assert named.json()["subject"] == {"kind": "person", "id": kate.id, "name": "Kate King"}


def test_a_path_names_people_and_groups_by_their_id_alone_and_no_refusal_quotes_what_was_sent(cluster, emily):
    """Fix round 1: the request log keeps every path, and a name or an e-mail is an identity value: a path takes ids
    alone, a body names people; and no refusal says back what the request said."""
    daniel, priya = _daniel(cluster), add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    payments = cluster.people.add_group("Payments", actor=BY_HAND)
    cluster.people.grant(payments.id, Role.AUDITOR, actor=BY_HAND)
    cluster.people.add_member(payments.id, daniel.id, actor=BY_HAND)
    cluster.people.link(daniel.id, subject="https://login.acme.test 1c2b-daniel", actor=BY_HAND)  # he signed in
    cluster.server.identities.disable(priya.id)
    for refused in (
        cluster.a.post(f"{API}/ee/people/daniel.okafor@acme.com/disable", headers=UI),
        cluster.a.post(f"{API}/ee/people/priya.shah@acme.com/enable", headers=UI),
        cluster.a.post(f"{ADMIN_EE}/people/daniel.okafor@acme.com/code", headers=as_admin(cluster)),
        cluster.a.post(
            f"{ADMIN_EE}/people/priya.shah@acme.com/link", headers=as_admin(cluster), json={"user_name": "p"}
        ),
        cluster.a.delete(f"{ADMIN_EE}/people/daniel.okafor@acme.com/link", headers=as_admin(cluster)),
        cluster.a.put(f"{API}/ee/groups/Payments", headers=UI, json={"name": "Billing"}),
        cluster.a.delete(f"{API}/ee/groups/Payments", headers=UI),
        cluster.a.post(f"{API}/ee/groups/Payments/members", headers=UI, json={"person": priya.id}),
        cluster.a.delete(f"{API}/ee/groups/Payments/members/{daniel.id}", headers=UI),
        cluster.a.delete(f"{API}/ee/roles/Payments/auditor", headers=UI),
        cluster.a.delete(f"{ADMIN_EE}/roles/daniel.okafor@acme.com/admin", headers=as_admin(cluster)),
        cluster.a.delete(f"{API}/ee/groups/{payments.id}/members/daniel.okafor@acme.com", headers=UI),
    ):
        assert refused.status_code == 404, refused.text
        assert all(said not in refused.text for said in ("daniel", "priya", "Payments", "Billing"))
    assert not cluster.server.identities.current(daniel.id).disabled and cluster.people.given(payments.id)
    assert cluster.server.identities.current(priya.id).disabled and cluster.people.account(priya.id) is None
    assert cluster.people.account(daniel.id).subject == "https://login.acme.test 1c2b-daniel"
    assert [group.name for group in cluster.people.groups()] == ["Payments"]
    assert cluster.server.audit.query(action="person.code.made") == []
    for body in ({"subject": "nobody-4f7c1e", "role": "admin"}, {"subject": "Daniel Okafor", "role": "admin"}):
        unknown = cluster.a.post(f"{API}/ee/roles", headers=UI, json=body)
        assert unknown.status_code == 404 and body["subject"] not in unknown.text
    for body in ({"subject": None, "role": "admin"}, {"subject": ["x"], "role": "admin"}, {"role": "admin"}):
        assert cluster.a.post(f"{API}/ee/roles", headers=UI, json=body).status_code == 400  # never the text "None"
    assert cluster.a.post(f"{API}/ee/groups/{payments.id}/members", headers=UI, json={"person": 7}).status_code == 400
    assert cluster.people.given(daniel.id) == frozenset()
    assert [ref.id for ref in cluster.people.members(payments.id)] == [daniel.id]  # left by no path of his e-mail


def test_latet_server_links_a_person_to_the_provider_and_unlinks_one_whose_issuer_changed(cluster):
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    link = f"{ADMIN_EE}/people/{priya.id}/link"
    made = cluster.a.post(link, headers=as_admin(cluster), json={"user_name": "priya.shah"})
    assert made.status_code == 200 and made.json() == {"ok": True, "person": {"id": priya.id, "name": "Priya Shah"}}
    assert cluster.people.unclaimed("priya.shah", scim_only=False) == priya.id  # her first sign-in finds her
    [entry] = cluster.server.audit.query(action="person.linked")
    assert entry.actor.startswith("admin:") and entry.details["fields"] == ["linked_by", "user_name"]
    assert "priya.shah" not in repr(entry.details)
    # She signed in, then the provider's issuer changed: her account names a subject no sign-in brings any more.
    cluster.people.link(priya.id, subject="https://old.acme.test 0f6a-priya", actor=BY_HAND)
    linked_once = cluster.a.post(link, headers=as_admin(cluster), json={"user_name": "priya.shah"})
    assert linked_once.status_code == 409
    assert cluster.a.delete(link, headers=as_admin(cluster)).status_code == 200
    account = cluster.people.account(priya.id)
    assert account.subject is None and account.linked_by is None  # no sign-in claims it until it is linked again
    [unlinked, *_] = cluster.server.audit.query(action="person.linked")
    assert unlinked.details["fields"] == ["subject"] and "0f6a" not in repr(unlinked.details)
    assert cluster.a.delete(link, headers=as_admin(cluster)).status_code == 404  # nothing left to unlink
    again = cluster.a.post(link, headers=as_admin(cluster), json={"user_name": "priya.shah", "scim": True})
    assert again.status_code == 200 and cluster.people.account(priya.id).scim_managed  # handed to SCIM (M3)
    assert cluster.a.post(link, headers=as_admin(cluster), json={"user_name": 7}).status_code == 400
    sign_in_as(cluster, add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN))
    page = cluster.a.post(f"{API}/ee/people/{priya.id}/link", headers=UI, json={"user_name": "priya.s"})
    assert page.status_code == 404  # `latet server`'s alone


def test_unlinking_an_account_scim_manages_leaves_it_to_no_first_sign_in_until_it_is_linked_again(cluster):
    """Fix round 1: a claim takes the whole account (roles, history, values); an account someone signed in to is never
    claimable again by itself, a SCIM user name included: only a link made on purpose opens it."""
    sam, _code = cluster.server.identities.add_person("Sam Rivera", "sam.rivera@acme.com", source="scim")
    cluster.people.grant(sam.id, Role.SECURITY, actor=BY_HAND)
    cluster.people.link(sam.id, source="scim", user_name="sam.rivera", scim_managed=True, actor="scim:s_tests")
    assert cluster.people.unclaimed("sam.rivera", scim_only=False) == sam.id  # as SCIM provisioned him
    cluster.people.link(sam.id, subject="https://old.acme.test 9e9e-sam", actor=sam.id)  # he signed in
    assert cluster.a.delete(f"{ADMIN_EE}/people/{sam.id}/link", headers=as_admin(cluster)).status_code == 200
    account = cluster.people.account(sam.id)
    assert (account.subject, account.linked_by, account.scim_managed) == (None, None, True)
    # A sign-in from the new issuer whose user-name claim is "sam.rivera" finds no account to take, nor does SCIM.
    assert cluster.people.unclaimed("sam.rivera", scim_only=False) is None
    assert cluster.people.unclaimed("sam.rivera", scim_only=True) is None
    relinked = {"user_name": "sam.rivera", "scim": True}
    assert (
        cluster.a.post(f"{ADMIN_EE}/people/{sam.id}/link", headers=as_admin(cluster), json=relinked).status_code == 200
    )
    assert cluster.people.unclaimed("sam.rivera", scim_only=False) == sam.id  # opened again, on purpose


def test_the_last_administrator_is_kept_however_they_are_one_unless_latet_server_says_so(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    cluster.people.add_member(admins.id, emily.id, actor=BY_HAND)
    sign_in_as(cluster, emily)
    refusals = [
        cluster.a.delete(f"{API}/ee/groups/{admins.id}/members/{emily.id}", headers=UI),
        cluster.a.delete(f"{API}/ee/roles/{admins.id}/admin", headers=UI),
        cluster.a.delete(f"{API}/ee/groups/{admins.id}", headers=UI),
        cluster.a.post(f"{API}/ee/people/{emily.id}/disable", headers=UI),
    ]
    for refused in refusals:
        assert refused.status_code == 409 and "last administrator" in refused.json()["error"]
    taken = cluster.a.delete(f"{ADMIN_EE}/roles/{admins.id}/admin", headers=as_admin(cluster))
    assert taken.status_code == 200 and taken.json()["roles"] == []


def test_a_person_disabled_on_the_page_loses_their_sessions_on_every_instance(cluster, emily):
    daniel = _daniel(cluster)
    session = cluster.server.sign_ins.open(daniel.id)
    him = {"id": daniel.id, "name": "Daniel Okafor"}  # whom the path named (H1)
    assert cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI).json() == {"ok": True, "person": him}
    assert cluster.server.identities.get(daniel.id).disabled
    assert cluster.people.account(daniel.id).disabled_by == "local"  # L7: SCIM does not enable him again
    settle(lambda: all(server.sign_ins.subject_of(session) is None for server in cluster.servers))  # E0b's message
    assert cluster.b.post(f"{API}/ee/people/{daniel.id}/enable", headers=UI).json() == {"ok": True, "person": him}
    assert not cluster.server.identities.current(daniel.id).disabled


def test_groups_are_made_renamed_joined_left_and_removed_and_a_membership_from_the_provider_stays(cluster, emily):
    daniel = _daniel(cluster)
    made = cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"})
    assert made.status_code == 201
    group = made.json()["group"]
    assert cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "payments"}).status_code == 409
    renamed = cluster.a.put(f"{API}/ee/groups/{group['id']}", headers=UI, json={"name": "Payments team"})
    assert renamed.json()["group"]["name"] == "Payments team"
    joined = cluster.a.post(f"{API}/ee/groups/{group['id']}/members", headers=UI, json={"person": daniel.id})
    assert joined.status_code == 200
    cluster.people.add_member(group["id"], daniel.id, source="scim", actor="scim:s_tests")
    assert cluster.a.delete(f"{API}/ee/groups/{group['id']}/members/{daniel.id}", headers=UI).status_code == 200
    left = cluster.a.delete(f"{API}/ee/groups/{group['id']}/members/{daniel.id}", headers=UI)
    assert left.status_code == 409 and "identity provider" in left.json()["error"]
    [listed] = cluster.a.get(f"{API}/ee/groups").json()["groups"]
    assert listed["members"] == [{"id": daniel.id, "name": "Daniel Okafor", "source": "scim"}]
    assert cluster.a.delete(f"{API}/ee/groups/{group['id']}", headers=UI).json() == {"ok": True}


def test_a_group_from_scim_is_neither_renamed_nor_removed_here(cluster, emily):
    group = cluster.people.add_group("Platform", source="scim", actor="scim:s_tests")
    renamed = cluster.a.put(f"{API}/ee/groups/{group.id}", headers=UI, json={"name": "Platform team"})
    assert renamed.status_code == 409
    assert cluster.a.delete(f"{API}/ee/groups/{group.id}", headers=UI).status_code == 409


def test_a_paused_license_still_disables_people_and_changes_no_group(cluster, emily):
    daniel = _daniel(cluster)
    paused(cluster)
    sign_in_as(cluster, emily)  # her session of before is past its twelve hours
    refused = cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"})
    assert refused.status_code == 403 and refused.json()["code"] == "not-manageable"
    assert "paused" in refused.json()["error"]
    assert cluster.a.get(f"{API}/ee/groups").status_code == 200  # read, not changed
    assert (
        cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI).status_code == 200
    )  # lets people in: serves


def test_once_a_trial_ended_nothing_changes_from_the_page_or_latet_server_and_both_still_read(cluster):
    on_trial(cluster)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    daniel = _daniel(cluster)
    end_trial(cluster)
    sign_in_as(cluster, emily)
    for refused in (
        cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"}),
        cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI),
        cluster.a.post(f"{ADMIN_EE}/roles", headers=as_admin(cluster), json={"subject": daniel.id, "role": "admin"}),
    ):
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    assert cluster.a.get(f"{API}/ee/people").status_code == 200
    assert cluster.b.get(f"{ADMIN_EE}/roles", headers=as_admin(cluster)).status_code == 200
    assert not cluster.server.identities.get(daniel.id).disabled  # `latet server people disable` (E0c) still can


def test_the_administration_routes_are_behind_e0bs_gate_and_each_call_is_recorded_under_its_name(cluster):
    assert cluster.a.get(f"{ADMIN_EE}/people").status_code == 401
    assert cluster.a.get(f"{ADMIN_EE}/people", headers={"x-latet-admin": "latet_adm_wrong"}).status_code == 401
    assert cluster.a.get(f"{ADMIN_EE}/people", headers={**as_admin(cluster), "origin": URL}).status_code == 403
    assert cluster.a.get(f"{ADMIN_EE}/people", headers=as_admin(cluster)).status_code == 200
    assert cluster.server.audit.query(action="ee.people.read")  # reads and refusals are recorded too
    made = cluster.a.post(f"{ADMIN_EE}/groups", headers=as_admin(cluster), json={"name": "Payments"})
    assert made.status_code == 201
    [entry] = cluster.server.audit.query(action="group.added")
    assert entry.actor.startswith("admin:")
    assert (entry.target, entry.details["status"]) == (made.json()["group"]["id"], 201)


def test_a_change_on_the_page_is_in_the_administration_log_by_the_administrator_and_from_where(cluster, emily):
    group = cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"}).json()["group"]
    [entry] = cluster.server.audit.query(action="group.added")
    assert (entry.actor, entry.target, entry.address) == (emily.id, group["id"], "203.0.113.7")


def test_a_company_wide_change_from_the_page_takes_a_company_sign_in_while_the_server_asks_for_one(cluster):
    """Decision (c), H2: `latet dashboard`'s one-time code opens a session an agent at the person's computer can open;
    while `company_changes: sso`, it reads everything and changes nothing that applies to everyone."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    daniel = _daniel(cluster)
    grant = {"subject": daniel.id, "role": "auditor"}
    _company_changes(cluster, "sso")
    _session(cluster, emily, "code")
    assert cluster.a.get(f"{API}/ee/people").status_code == 200  # reads are never refused by it
    for refused in (
        cluster.a.post(f"{API}/ee/roles", headers=UI, json=grant),
        cluster.a.post(f"{API}/ee/groups", headers=UI, content=b"not JSON"),  # said before the body is read
        cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI),  # serve routes too: a code for someone
        cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI),
        cluster.a.put(f"{API}/ee/license", headers=UI, json={"license": _license_text(cluster)}),
        cluster.a.put(f"{API}/ee/license", headers=UI, content=b"x" * (300 * 1024)),  # before the body is read
        cluster.a.request("DELETE", f"{API}/ee/license", headers=UI),
    ):
        assert refused.status_code == 403 and refused.json() == COMPANY_SIGN_IN, refused.text
    assert cluster.people.given(daniel.id) == frozenset() and cluster.server.edition.standing().license.users == 300
    assert not cluster.server.identities.current(daniel.id).disabled
    assert cluster.server.audit.query(action="person.code.made") == []
    assert cluster.a.post(f"{ADMIN_EE}/roles", headers=as_admin(cluster), json=grant).status_code == 200  # the token
    _session(cluster, emily, "sso")
    assert cluster.a.delete(f"{API}/ee/roles/{daniel.id}/auditor", headers=UI).status_code == 200
    assert cluster.a.put(f"{API}/ee/license", headers=UI, json={"license": _license_text(cluster)}).status_code == 200
    _company_changes(cluster, "any-session")
    _session(cluster, emily, "code")
    assert cluster.a.post(f"{API}/ee/roles", headers=UI, json=grant).status_code == 200
    _company_changes(cluster, None)  # no single sign-on: any administrator's session
    assert cluster.a.delete(f"{API}/ee/roles/{daniel.id}/auditor", headers=UI).status_code == 200


def test_the_company_sign_in_is_asked_again_in_the_changes_transaction(cluster):
    """Instance A hears nothing and keeps its copy of the settings: B requires the company sign-in meanwhile."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    daniel = _daniel(cluster)
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()
    _company_changes(cluster, "any-session")  # set through A, which reads its own change at once
    cluster.servers[1].edition.settings_store.set_section("sso", {**SSO, "company_changes": "sso"}, actor=BY_HAND)
    assert cluster.server.edition.settings().sso.company_changes == "any-session"  # A's copy
    _session(cluster, emily, "code")
    for refused in (
        cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": daniel.id, "role": "auditor"}),
        cluster.a.put(f"{API}/ee/license", headers=UI, json={"license": _license_text(cluster)}),
    ):
        assert refused.status_code == 403 and refused.json() == COMPANY_SIGN_IN, refused.text
    assert cluster.people.given(daniel.id) == frozenset() and cluster.server.edition.standing().license.users == 300


def test_who_makes_a_change_is_weighed_again_in_its_transaction(cluster, monkeypatch):
    """Task 3's carry: the gate weighs the roles this instance's copy holds; the change's transaction reads them again,
    so that a role taken away on any instance before it is seen there."""
    daniel = _daniel(cluster)
    sign_in_as(cluster, daniel)
    copy = frozenset({Role.USER, Role.ADMIN})  # what a copy read before his role was taken away would say
    monkeypatch.setattr(cluster.people, "roles_of", lambda identity_id: copy)
    assert cluster.a.get(f"{API}/ee/groups").status_code == 200  # the copy lets him read
    made = cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"})
    assert made.status_code == 403 and "people.manage" in made.json()["error"]
    installed = cluster.a.put(f"{API}/ee/license", headers=UI, json={"license": _license_text(cluster)})
    assert installed.status_code == 403 and "license.manage" in installed.json()["error"]
    assert cluster.people.groups() == [] and cluster.server.edition.standing().license.users == 300


def test_a_license_without_roles_installed_meanwhile_lets_no_change_through_the_page(cluster):
    """The license as the change's transaction finds it lists no roles (another instance installed it, A's copy lists
    them still): roles no longer decide, and the page changes nothing (`latet server` does); nor do a change of groups
    on the page or through `latet server`, whose feature it lacks (``held``, the gates of E1 having read A's copy)."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()
    assert cluster.a.get(f"{API}/ee/license").json()["state"] == "valid"  # A's copy read
    assert cluster.a.get(f"{API}/ee/groups").status_code == 200
    cluster.servers[1].edition.install_license(cluster.signer.make(features=["sso", "locked-rules"]), actor=BY_HAND)
    installed = cluster.a.put(f"{API}/ee/license", headers=UI, json={"license": _license_text(cluster)})
    assert installed.status_code == 403 and "latet server" in installed.json()["error"], installed.text
    for made in (
        cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"}),
        cluster.a.post(f"{ADMIN_EE}/groups", headers=as_admin(cluster), json={"name": "Payments"}),
    ):
        assert made.status_code == 403 and made.json()["feature"] == "roles", made.text
    assert cluster.servers[1].edition.standing().license.users == 300  # the one B installed, still
    assert cluster.people.groups() == []


def test_a_person_disabled_has_no_role_in_a_changes_transaction(cluster):
    """A person disabled on any instance, whose session this one has not ended yet, makes no change in their name."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    standing = cluster.server.edition.standing()
    with cluster.server.db.transaction() as conn:
        assert cluster.people.roles_in(conn, emily.id) == {Role.USER, Role.ADMIN}
        assert in_their_name(cluster.people, conn, standing, emily.id, "people.manage", "sso") is None
    cluster.server.identities.disable(emily.id)
    with cluster.server.db.transaction() as conn:
        assert cluster.people.roles_in(conn, emily.id) is None
        assert cluster.people.roles_in(conn, "u_nobody2345") is None
        refused = in_their_name(cluster.people, conn, standing, emily.id, "people.manage", "sso")
        assert refused is not None and refused.status == 403


def test_a_change_with_an_administration_token_revoked_meanwhile_is_refused_before_anything_is_weighed(
    cluster, monkeypatch
):
    """F11: the token's row is the change's first, held until it ends: a token revoked after the gate checked it
    changes nothing and learns nothing of the change (a name taken is not said)."""
    cluster.people.add_group("Payments", actor=BY_HAND)
    token_id = cluster.server.admin.check(cluster.admin_token)
    monkeypatch.setattr(cluster.server.admin, "checked", lambda secret: (token_id, False))  # as the gate found it
    assert cluster.server.admin.revoke(token_id)
    for name in ("Payments", "Billing"):
        answer = cluster.a.post(f"{ADMIN_EE}/groups", headers=as_admin(cluster), json={"name": name})
        assert answer.status_code == 401, answer.text
    assert [group.name for group in cluster.people.groups()] == ["Payments"]


def test_a_change_whose_entry_cannot_be_written_is_neither_made_nor_told(cluster, emily, monkeypatch):
    """F11: the change and its entries are one transaction; nothing is told the instances of a change not made."""
    record = cluster.server.audit.record

    def refusing(*args, conn=None, **kwargs):
        if conn is not None:  # an entry in a change's transaction
            raise RuntimeError("the administration log does not take it")
        return record(*args, **kwargs)  # the call's own entry, which the administration's gate writes after it

    monkeypatch.setattr(cluster.server.audit, "record", refusing)
    told = []
    monkeypatch.setattr(cluster.people, "committed", told.append)
    for answer in (
        cluster.a.post(f"{API}/ee/groups", headers=UI, json={"name": "Payments"}),
        cluster.a.post(f"{ADMIN_EE}/groups", headers=as_admin(cluster), json={"name": "Payments"}),
    ):
        assert answer.status_code == 500, answer.text
    assert cluster.people.groups() == [] and told == []


def test_what_a_changes_answer_says_is_read_in_its_transaction_and_a_change_is_answered_as_made_or_not_made(
    cluster, emily, monkeypatch
):
    """Fix round 1: a change committed whose answer then fails would be a 500 for a change made (a new code made, the
    one before dead, the log saying it was done): what the answer says is read in the change's transaction, so that it
    fails with the change, which is not made."""
    daniel = _daniel(cluster)
    first = cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI).json()["code"]

    def unreadable(settings):
        raise RuntimeError("the certificate's store does not answer")

    with monkeypatch.context() as patched:
        patched.setattr(people_api, "connect_command", unreadable)
        assert cluster.a.post(f"{ADMIN_EE}/people/{daniel.id}/code", headers=as_admin(cluster)).status_code == 500
    assert cluster.server.audit.query(action="person.code.made", actor=emily.id)  # the first code's alone
    assert len(cluster.server.audit.query(action="person.code.made")) == 1

    def unanswered(*, conn=None):
        raise RuntimeError("the tokens are not read")

    with monkeypatch.context() as patched:
        patched.setattr(cluster.server.edition.scim_tokens, "all", unanswered)
        disabled = cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI)
    assert disabled.status_code == 500, disabled.text
    assert not cluster.server.identities.current(daniel.id).disabled
    assert cluster.server.identities.enrol(first, "laptop") is not None  # the code before still the one in use


def test_every_answer_that_says_what_a_change_did_is_read_in_its_transaction(cluster, emily, monkeypatch):
    """Fix round 2: each reader of what a change's answer says (the connect command of a person added, read before the
    change since the end-of-plan wave; the roles a grant and a revoke leave, the SCIM tokens a group's removal and a
    member's leaving leave behind) fails with the change, which is then not made."""
    daniel = _daniel(cluster)
    leads = cluster.people.add_group("Payments leads", actor=BY_HAND)
    cluster.people.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    cluster.people.add_member(leads.id, daniel.id, actor=BY_HAND)
    people = cluster.people
    given = people.given

    def unread(*args, **kwargs):
        raise RuntimeError("not read")

    with monkeypatch.context() as patched:
        patched.setattr(people_api, "connect_command", unread)
        added = cluster.a.post(f"{API}/ee/people", headers=UI, json={"name": "Tom Becker"})
    assert added.status_code == 500 and [p.name for p in cluster.server.identities.all(kind="person")] == [
        "Daniel Okafor",
        "Emily Carter",
    ]
    with monkeypatch.context() as patched:
        patched.setattr(people, "given", unread)
        granted = cluster.a.post(f"{API}/ee/roles", headers=UI, json={"subject": daniel.id, "role": "auditor"})
        revoked = cluster.a.delete(f"{API}/ee/roles/{leads.id}/group-lead", headers=UI)
    assert (granted.status_code, revoked.status_code) == (500, 500)
    assert given(daniel.id) == frozenset() and given(leads.id) == {Role.GROUP_LEAD}
    with monkeypatch.context() as patched:
        patched.setattr(cluster.server.edition.scim_tokens, "all", unread)
        left = cluster.a.delete(f"{API}/ee/groups/{leads.id}/members/{daniel.id}", headers=UI)
        removed = cluster.a.delete(f"{API}/ee/groups/{leads.id}", headers=UI)
    assert (left.status_code, removed.status_code) == (500, 500)
    assert [ref.id for ref in people.members(leads.id)] == [daniel.id] and people.group(leads.id) is not None


def test_demoting_or_disabling_the_maker_of_live_scim_tokens_says_them_and_ends_none(cluster, emily):
    """O3: a SCIM token is the provider's credential; ending it would stop the leavers' removal too."""
    tokens = cluster.server.edition.scim_tokens
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.ADMIN)
    tokens.make(made_by=daniel.id, days=1)  # ended by the time he is demoted: nothing to say of it
    cluster.clock.advance(2 * 86400)
    sign_in_as(cluster, emily)  # her session of before is past its twelve hours
    made, secret = tokens.make(made_by=daniel.id)
    tokens.make(made_by=emily.id)  # an administrator's still
    revoked = cluster.a.delete(f"{API}/ee/roles/{daniel.id}/admin", headers=UI)
    assert revoked.json()["scim_tokens"] == [made.id]
    assert cluster.a.post(f"{API}/ee/people/{daniel.id}/disable", headers=UI).json() == {
        "ok": True,
        "person": {"id": daniel.id, "name": "Daniel Okafor"},
        "scim_tokens": [made.id],
    }
    assert tokens.check(secret) == made.id  # said, never ended
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    cluster.people.add_member(admins.id, priya.id, actor=BY_HAND)
    cluster.people.add_member(admins.id, emily.id, actor=BY_HAND)  # an administrator without the group too
    hers, _ = tokens.make(made_by=priya.id)
    assert cluster.a.delete(f"{API}/ee/groups/{admins.id}", headers=UI).json() == {"ok": True, "scim_tokens": [hers.id]}
    assert tokens.check(secret) == made.id and len(tokens.all()) == 4


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------


def _meanwhile(cluster, monkeypatch, action):
    """``action()`` done, and committed, as the next change's transaction on instance A begins (each reads the license
    there first: ``Licensing.standing_in``), so after anything its request read before the transaction."""
    licensing = cluster.server.edition.licensing
    standing_in, pending = licensing.standing_in, [action]

    def first(conn):
        while pending:
            pending.pop()()
        return standing_in(conn)

    monkeypatch.setattr(licensing, "standing_in", first)


def test_a_group_removed_meanwhile_is_answered_as_not_there_and_nothing_is_recorded(cluster, emily, monkeypatch):
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    _meanwhile(cluster, monkeypatch, lambda: cluster.people.remove_group(platform.id, actor=BY_HAND))
    removed = cluster.a.delete(f"{API}/ee/groups/{platform.id}", headers=UI)
    assert removed.status_code == 404 and removed.json() == {"error": "there is no such group on this server"}
    assert cluster.server.audit.query(action="group.removed", actor=emily.id) == []


def test_whom_a_change_takes_a_groups_role_from_is_read_in_its_transaction(cluster, emily, monkeypatch):
    """O3: the makers of live SCIM tokens a group's removal, or the admin role taken from it, demote are its members as
    the change's transaction finds them, someone who joined after the page asked and before the change included."""
    daniel = _daniel(cluster).id
    made, _ = cluster.server.edition.scim_tokens.make(made_by=daniel)
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    _meanwhile(cluster, monkeypatch, lambda: cluster.people.add_member(admins.id, daniel, actor=BY_HAND))
    revoked = cluster.a.delete(f"{API}/ee/roles/{admins.id}/admin", headers=UI)
    assert revoked.status_code == 200 and revoked.json()["scim_tokens"] == [made.id], revoked.text
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    cluster.people.remove_member(admins.id, daniel, actor=BY_HAND)
    _meanwhile(cluster, monkeypatch, lambda: cluster.people.add_member(admins.id, daniel, actor=BY_HAND))
    removed = cluster.a.delete(f"{API}/ee/groups/{admins.id}", headers=UI)
    assert removed.json() == {"ok": True, "scim_tokens": [made.id]}


def test_a_revokes_answer_says_the_scim_tokens_its_own_transaction_left(cluster, emily, monkeypatch):
    """O3, read before the commit (``_orphaned(within.conn, …)``): what a change made after it commits (here Daniel made
    administrator again, before the answer is written) never changes what the answer says the revoke left."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.ADMIN)
    made, _ = cluster.server.edition.scim_tokens.make(made_by=daniel.id)
    people, committed, again = cluster.people, cluster.people.committed, []

    def then(changing):
        committed(changing)
        if not again:  # the revoke's commit: the next change follows it at once
            again.append(changing)
            people.grant(daniel.id, Role.ADMIN, actor=BY_HAND)

    monkeypatch.setattr(people, "committed", then)
    revoked = cluster.a.delete(f"{API}/ee/roles/{daniel.id}/admin", headers=UI)
    assert revoked.status_code == 200 and revoked.json()["scim_tokens"] == [made.id], revoked.text
    assert people.admins() == {emily.id, daniel.id}  # the grant after it was made


def _row_held(db, identity_id):
    """Whether another transaction holds the person's row (a code's)."""
    try:
        with db.transaction() as conn:
            conn.execute("SELECT 1 FROM identities WHERE id = %s FOR UPDATE NOWAIT", (identity_id,))
    except psycopg.errors.LockNotAvailable:
        return True
    return False


def _email_held(db, email):
    """Whether another transaction holds the e-mail of a person it adds (E0b's ``_held_email``)."""
    with db.transaction() as conn:
        (free,) = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('latet-person-email:' || %s || ':' || lower(%s), 0))",
            (db.schema, email),
        ).fetchone()
    return not free


def test_the_connect_command_is_read_before_the_change_holds_anything(cluster, emily, monkeypatch):
    """The certificate's file is read, and its fingerprint made, before the change's transaction: never while the
    person's row, or the e-mail of a person added, is held there."""
    daniel, db, held = _daniel(cluster), cluster.server.db, []
    command = people_api.connect_command

    def read(settings):
        held.append((_row_held(db, daniel.id), _email_held(db, "tom.becker@acme.com")))
        return command(settings)

    monkeypatch.setattr(people_api, "connect_command", read)
    made = cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI)
    added = cluster.a.post(f"{API}/ee/people", headers=UI, json={"name": "Tom Becker", "email": "tom.becker@acme.com"})
    assert (made.status_code, added.status_code) == (200, 201)
    assert made.json()["command"] == added.json()["command"] == f"latet connect {URL}"
    assert held == [(False, False), (False, False)]
