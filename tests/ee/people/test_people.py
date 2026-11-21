"""Groups, members, roles, the control of API keys and accounts, in PostgreSQL, the same on every instance
(ee/people.py)."""

import ast
import json
import logging
import threading
from pathlib import Path

import pytest

from latet.ee import people as people_module
from latet.ee.people import (
    CHANNEL,
    GENERATION,
    Changing,
    LastAdmin,
    NotYours,
    Taken,
    disable_person,
    enable_person,
)
from latet.ee.roles import Role
from latet.ee.server import publish
from latet.events import make_event

from ..servers import MASTER_KEY, Instances, purge
from ..servers import left as keys_left
from .server_support import BY_HAND, SERVER_TESTS, add_person, settle

pytestmark = SERVER_TESTS

ESC, BEL, NEWLINE, RLO = chr(0x1B), chr(0x07), chr(0x0A), "\N{RIGHT-TO-LEFT OVERRIDE}"


def _emily(cluster):
    return add_person(cluster, "Emily Carter", "emily.carter@acme.com")


def _daniel(cluster):
    return add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")


def _deaf(server):
    """The instance hears no Enterprise message any more (its listener stopped): what it learns of another instance's
    change is the generation in Valkey alone."""
    listener, server.edition._listener = server.edition._listener, None
    listener.stop()
    return server.edition.people


def test_everyone_is_a_user_and_a_role_given_to_a_person_adds_to_it(cluster):
    people, emily = cluster.people, _emily(cluster)
    assert people.roles_of(emily.id) == {Role.USER}
    assert people.grant(emily.id, Role.SECURITY, actor=BY_HAND)
    assert not people.grant(emily.id, Role.SECURITY, actor=BY_HAND)  # once is enough
    assert people.roles_of(emily.id) == {Role.USER, Role.SECURITY}
    assert people.given(emily.id) == {Role.SECURITY}


def test_a_role_given_to_a_group_is_its_members_and_goes_with_the_membership(cluster):
    people, priya = cluster.people, add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    leads = people.add_group("Payments leads", actor=BY_HAND)
    people.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    people.add_member(leads.id, priya.id, actor=BY_HAND)
    assert people.roles_of(priya.id) == {Role.USER, Role.GROUP_LEAD}
    assert people.groups_of(priya.id) == [leads]
    people.remove_member(leads.id, priya.id, actor=BY_HAND)
    assert people.roles_of(priya.id) == {Role.USER} and people.groups_of(priya.id) == []


def test_the_user_role_is_never_given_and_an_api_key_takes_none(cluster):
    people = cluster.people
    key, _secret = cluster.server.identities.add_key("Billing CI")
    with pytest.raises(ValueError, match="everyone has the user role"):
        people.grant(_emily(cluster).id, Role.USER, actor=BY_HAND)
    with pytest.raises(ValueError, match="an API key takes no role"):
        people.grant(key.id, Role.ADMIN, actor=BY_HAND)
    with pytest.raises(KeyError):
        people.grant("u_nobody0000", Role.ADMIN, actor=BY_HAND)
    assert people.roles_of(key.id) == frozenset()


def test_group_names_are_unique_case_aside_and_a_group_is_found_by_id_or_name(cluster):
    people = cluster.people
    payments = people.add_group(" Payments ", actor=BY_HAND)
    assert payments.name == "Payments" and payments.id.startswith("g_") and len(payments.id) == 12
    assert payments.source == "local"
    with pytest.raises(Taken):
        people.add_group("payments", source="scim", actor=BY_HAND)
    with pytest.raises(ValueError, match="1 to 80"):
        people.add_group("", actor=BY_HAND)
    assert people.group("PAYMENTS") == payments == people.group(payments.id)


def test_a_group_is_never_named_as_another_groups_id_and_a_name_taken_is_not_quoted(cluster):
    people = cluster.people
    payments = people.add_group("Payments", actor=BY_HAND)
    with pytest.raises(ValueError, match="shaped like a group's id"):
        people.add_group(payments.id, actor=BY_HAND)
    with pytest.raises(ValueError, match="shaped like a group's id"):
        people.update_group(payments.id, "g_abcdefghij", None, actor=BY_HAND)
    platform = people.add_group("Platform", actor=BY_HAND)
    with pytest.raises(Taken) as taken:
        people.update_group(platform.id, "payments", None, actor=BY_HAND)
    assert "ayments" not in str(taken.value)
    assert people.group(payments.id) == payments and people.group("g_abcdefghij") is None


def test_a_member_is_a_person_of_a_group_of_this_server(cluster):
    people, emily = cluster.people, _emily(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    payments = people.add_group("Payments", actor=BY_HAND)
    with pytest.raises(ValueError, match="an API key"):
        people.add_member(payments.id, key.id, actor=BY_HAND)
    with pytest.raises(ValueError, match="an API key"):
        people.set_members(payments.id, [emily.id, key.id], source="scim", actor="scim:s_tests")
    with pytest.raises(KeyError):
        people.add_member(payments.id, "u_nobody0000", actor=BY_HAND)
    with pytest.raises(KeyError):
        people.add_member("g_nogroup000", emily.id, actor=BY_HAND)
    with pytest.raises(KeyError):
        people.set_members("g_nogroup000", [emily.id], source="scim", actor="scim:s_tests")
    assert people.members(payments.id) == []
    people.set_members(payments.id, [emily.id], source="scim", actor="scim:s_tests")
    assert people.members(payments.id, source="scim") == [emily.ref()]


def test_group_names_and_account_fields_hold_no_control_or_bidirectional_character_and_are_bounded(cluster):
    people, emily = cluster.people, _emily(cluster)
    for name in (f"Payments{ESC}]8;;https://pay.example{BEL}Pay", f"Pay{RLO}stnemy", "P" * 81):
        with pytest.raises(ValueError, match="1 to 80"):
            people.add_group(name, actor=BY_HAND)
    payments = people.add_group("P" * 80, actor=BY_HAND)  # the longest name
    with pytest.raises(ValueError, match="1 to 80"):
        people.update_group(payments.id, f"Pay{ESC}[2Jments", None, actor=BY_HAND)
    refused = [
        {"user_name": "e" * 312 + "@acme.com"},  # 321 characters
        {"user_name": f"emily{ESC}[2J@acme.com"},
        {"subject": f"https://login.acme.com 0f6a{NEWLINE}forged"},
        {"external_id": f"ec{RLO}"},
        {"external_id": "x" * 256},
        {"scim": {"displayName": f"Emily{ESC}]0;title{BEL}"}},
        {"scim": {"name": {"givenName": "E" * 201}}},
        {"claimed": [f"group-{n}" for n in range(501)]},
        {"claimed": ["g" * 257]},
        {"claimed": [f"Pay{RLO}ments"]},
    ]
    for fields in refused:
        with pytest.raises(ValueError, match="at most"):
            people.link(emily.id, **fields)
    assert people.account(emily.id) is None  # nothing was kept
    people.link(emily.id, user_name="e" * 311 + "@acme.com", claimed=[f"group-{n}" for n in range(500)])  # 320
    assert len(people.account(emily.id).claimed) == 500
    assert [group.name for group in people.groups()] == ["P" * 80]


def test_each_source_changes_only_its_own_memberships(cluster):
    people, emily = cluster.people, _emily(cluster)
    payments = people.add_group("Payments", source="scim", external_id="5d0e7a1c", actor="scim:s_tests")
    people.set_members(payments.id, [emily.id], source="scim", actor="scim:s_tests")
    people.add_member(payments.id, emily.id, actor=BY_HAND)  # by hand too
    people.set_members(payments.id, [], source="scim", actor="scim:s_tests")
    assert people.members(payments.id) == [emily.ref()]  # the membership by hand stays
    assert people.members(payments.id, source="scim") == []
    assert not people.remove_member(payments.id, emily.id, source="scim", actor=BY_HAND)


def test_removing_a_group_takes_its_roles_and_its_control_of_keys_with_it(cluster):
    people, daniel = cluster.people, _daniel(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = people.add_group("Platform", actor=BY_HAND)
    people.add_member(platform.id, daniel.id, actor=BY_HAND)
    people.grant(platform.id, Role.AUDITOR, actor=BY_HAND)
    people.add_controller(key.id, platform.id, actor=BY_HAND)
    assert people.remove_group(platform.id, actor=BY_HAND)
    assert people.roles_of(daniel.id) == {Role.USER} and people.controlled_by(daniel.id) == []
    assert people.controllers(key.id) == [] and people.group("Platform") is None


def test_the_control_of_a_key_is_given_to_a_person_or_a_group(cluster):
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = people.add_group("Platform", actor=BY_HAND)
    people.add_member(platform.id, daniel.id, actor=BY_HAND)
    people.add_controller(key.id, emily.id, actor=BY_HAND)
    people.add_controller(key.id, platform.id, actor=BY_HAND)
    assert people.controlled_by(emily.id) == [key.ref()] == people.controlled_by(daniel.id)
    assert sorted(people.controllers(key.id)) == sorted([emily.id, platform.id])
    with pytest.raises(ValueError, match="an API key"):
        people.add_controller(key.id, key.id, actor=BY_HAND)
    people.remove_member(platform.id, daniel.id, actor=BY_HAND)
    assert people.controlled_by(daniel.id) == []


def test_the_groups_of_a_sign_in_give_memberships_through_the_mapping_and_a_new_mapping_applies_to_everyone(cluster):
    people, emily = cluster.people, _emily(cluster)
    people.apply_claims(emily.id, ["/latet-users", "5d0e7a1c", "unrelated"], {"5D0E7A1C": "Payments"})
    payments = people.group("Payments")
    assert payments.source == "oidc" and people.members(payments.id) == [emily.ref()]
    assert people.account(emily.id).claimed == ("/latet-users", "5d0e7a1c", "unrelated")
    people.apply_claims(emily.id, [], {"5d0e7a1c": "Payments"})  # out of every group at the provider
    assert people.members(payments.id) == []
    people.apply_claims(emily.id, ["latet-users"], {})
    people.regroup({"latet-users": "Everyone"}, actor=BY_HAND)
    assert [group.name for group in people.groups_of(emily.id)] == ["Everyone"]


def test_a_new_mapping_joins_no_one_to_a_group_scim_pushed_and_a_group_made_by_hand_still_works(cluster, caplog):
    """Task 7's fix round 1, as at a sign-in (test_sign_in.py): the line of the mapping that names a SCIM group is
    skipped, said in the log naming no group and no person, and a membership a sign-in made there before goes; the other
    lines apply, onto a group made by hand too (the administrator's choice)."""
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    scims = people.add_group("Latet admins", source="scim", actor="scim:s_tests")
    people.grant(scims.id, Role.AUDITOR, actor=BY_HAND)
    people.set_members(scims.id, [daniel.id], source="scim", actor="scim:s_tests")
    leads = people.add_group("Payments leads", actor=BY_HAND)
    people.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    people.apply_claims(emily.id, ["latet-admins", "payments-leads"], {})  # the groups of her last sign-in
    people.add_member(scims.id, emily.id, source="oidc", actor=BY_HAND)  # as a sign-in made it before the fix
    with caplog.at_level(logging.WARNING, logger="latet.ee.people"):
        people.regroup({"latet-admins": "LATET ADMINS", "payments-leads": "Payments leads"}, actor=BY_HAND)
    assert people.groups_of(emily.id) == [leads] and people.roles_of(emily.id) == {Role.USER, Role.GROUP_LEAD}
    assert people.members(scims.id) == [daniel.ref()]
    assert "a group SCIM pushed" in caplog.text
    for value in ("Latet admins", "LATET ADMINS", "latet-admins", "Emily", emily.id, scims.id):
        assert value not in caplog.text


def test_a_new_mapping_finds_each_group_once_and_changes_everyones_memberships_together(cluster, monkeypatch):
    """The final review's Important: a regroup took 1 + 2k statements per person; each group the mapping names is found
    (or made) once now, and every membership from sign-ins changes in one write of them, each noted."""
    people = cluster.people
    names = ["Emily Carter", "Daniel Okafor", "Priya Shah", "Tom Becker", "Laura Chen", "Sam Rivera"]
    everyone = [add_person(cluster, name, f"{name.lower().replace(' ', '.')}@acme.com") for name in names]
    for person in everyone:
        people.apply_claims(person.id, ["latet-users", "payments-leads"], {})
    found: list[str] = []
    written: list[int] = []
    group_named, oidc_members = people._group_named, people._oidc_members

    def named(changing, name):
        found.append(name)
        return group_named(changing, name)

    def wrote(changing, before, wanted):
        written.append(len(before ^ wanted))
        return oidc_members(changing, before, wanted)

    monkeypatch.setattr(people, "_group_named", named)
    monkeypatch.setattr(people, "_oidc_members", wrote)
    people.regroup({"latet-users": "Everyone", "payments-leads": "Payments leads"}, actor=BY_HAND)
    assert sorted(found) == ["Everyone", "Payments leads"] and written == [12]
    for person in everyone:
        assert [group.name for group in people.groups_of(person.id)] == ["Everyone", "Payments leads"]
    people.regroup({"latet-users": "Everyone"}, actor=BY_HAND)
    assert sorted(found) == ["Everyone", "Everyone", "Payments leads"] and written == [12, 6]
    assert people.members(people.group("Payments leads").id) == []
    added = cluster.server.audit.query(action="member.added", actor=BY_HAND)
    removed = cluster.server.audit.query(action="member.removed", actor=BY_HAND)
    assert len(added) == 12 and len(removed) == 6
    assert {entry.details["identity"] for entry in removed} == {person.id for person in everyone}


def test_the_sign_in_page_learns_which_groups_of_the_mapping_scim_pushed(cluster):
    """Fix round 1: what the Sign-in page's check says (Task 10), compared as the names' unique index compares them."""
    people = cluster.people
    people.add_group("Latet admins", source="scim", actor="scim:s_tests")
    people.add_group("Payments leads", actor=BY_HAND)
    people.apply_claims(_emily(cluster).id, ["everyone"], {"everyone": "Everyone"})  # a group made for sign-ins
    names = ["Payments leads", "LATET ADMINS", "Everyone", "Nowhere"]
    assert people.mapping_taken(names) == ["LATET ADMINS"]
    assert people.mapping_taken([]) == [] and people.mapping_taken(["Payments leads"]) == []


def test_accounts_are_found_by_subject_user_name_and_external_id_and_each_is_unique(cluster):
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    people.link(emily.id, subject="https://login.acme.com 0f6a", user_name="Emily.Carter@acme.com", external_id="ec")
    assert people.find(subject="https://login.acme.com 0f6a") == emily.id
    assert people.find(user_name="emily.carter@ACME.com") == emily.id
    assert people.find(external_id="ec") == emily.id
    with pytest.raises(Taken):
        people.link(daniel.id, user_name="EMILY.CARTER@acme.com")
    people.link(emily.id, deleted=True)
    assert people.find(user_name="emily.carter@acme.com", deleted=False) is None


@pytest.mark.parametrize(
    ("name", "real", "look_alike"),
    [
        ("Kate King", "kate.king@acme.com", "\N{KELVIN SIGN}ate.king@acme.com"),
        ("Ian Lee", "ian.lee@acme.com", "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}an.lee@acme.com"),
    ],
)
def test_a_user_name_written_with_a_look_alike_names_nobody_else(cluster, name, real, look_alike):
    """The final review's F3: PostgreSQL's ``lower`` under the database's locale, and Python's on a non-ASCII text,
    fold a look-alike onto an ASCII letter; a provider's user named with one claimed the account an administrator linked
    for someone who never signed in. User names match with A to Z's case aside and nothing else folded now: the
    look-alike claims nothing, links nothing, names nobody, and is a user name of its own beside the real one."""
    people = cluster.people
    person = add_person(cluster, name, real, Role.ADMIN)
    people.link_person(person.id, user_name=real, actor=BY_HAND)  # an administrator who never signed in: claimable
    assert people.unclaimed(look_alike, scim_only=False) is None
    assert people.find(user_name=look_alike) is None
    with pytest.raises(Taken):  # the claim weighed again with the account's row held
        people.link(person.id, subject="https://login.acme.com 7d7d", claim=look_alike)
    with pytest.raises(NotYours), people.changing(person.id) as within:  # one's own link from their session
        people.link_own(
            person.id, subject="https://login.acme.com 7d7d", user_name=look_alike, email=None, within=within
        )
    assert people.account(person.id).subject is None
    mallory = add_person(cluster, "Mallory West", "mallory.west@acme.com")
    people.link(mallory.id, user_name=look_alike)  # exactly another user name: kept beside the real one
    assert people.find(user_name=look_alike) == mallory.id and people.find(user_name=real.upper()) == person.id
    sam = add_person(cluster, "Sam Rivera", "sam.rivera@acme.com")
    for taken in (look_alike, real.upper()):  # the same user name, A to Z's case aside
        with pytest.raises(Taken):
            people.link(sam.id, user_name=taken)
    assert people.unclaimed(real.upper(), scim_only=False) == person.id


def test_whether_an_account_waits_for_its_first_sign_in_by_its_user_name(cluster):
    """The final review's F5: what makes a new user-name claim `latet server`'s change alone, beside a sign-in."""
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)

    def waits():
        with cluster.server.db.connection() as conn:
            return people.claimable(conn=conn)

    assert not waits()
    people.link(daniel.id, user_name="daniel.okafor@acme.com")  # a user name nobody named for a first sign-in
    assert not waits()
    people.link_person(emily.id, user_name="emily.carter@acme.com", actor=BY_HAND)
    assert waits()
    people.link(emily.id, deleted=True)  # deleted at the provider: no sign-in claims it
    assert not waits()
    people.link(emily.id, deleted=False)
    assert waits()
    people.link(emily.id, subject="https://login.acme.com 0f6a", claim="emily.carter@acme.com")  # she signed in
    assert not waits()


def test_user_names_still_match_with_the_case_of_a_to_z_aside(cluster):
    people, emily = cluster.people, _emily(cluster)
    people.link_person(emily.id, user_name="Emily.Carter", actor=BY_HAND)
    assert people.unclaimed("emily.carter", scim_only=False) == emily.id
    assert people.find(user_name="EMILY.CARTER") == emily.id
    with people.changing(emily.id) as within:
        people.link_own(
            emily.id, subject="https://login.acme.com 0f6a", user_name="emily.carter", email=None, within=within
        )
    assert people.account(emily.id).subject == "https://login.acme.com 0f6a"


def test_a_first_sign_in_finds_an_account_scim_made_never_a_person_added_by_hand_by_their_e_mail(cluster):
    people = cluster.people
    daniel = add_person(cluster, "Daniel Okafor", "Daniel.Okafor@acme.com")
    assert people.unclaimed("daniel.okafor@acme.com", scim_only=False) is None  # added by hand: no one (H1)
    people.apply_claims(daniel.id, ["latet-users"], {})  # an account of theirs that names no user
    people.link(daniel.id, user_name="daniel.okafor@acme.com")  # a user name no one claimable gave
    assert people.unclaimed("daniel.okafor@acme.com", scim_only=False) is None
    priya, _code = cluster.server.identities.add_person("Priya Shah", "priya.shah@acme.com", source="scim")
    people.link(priya.id, user_name="Priya.Shah@acme.com", scim_managed=True, source="scim", actor="scim:s_tests")
    assert people.account(priya.id).scim_managed and people.account(priya.id).linked_by == "scim"
    assert people.unclaimed("priya.shah@ACME.com", scim_only=False) == priya.id
    assert people.unclaimed("priya.shah@ACME.com", scim_only=True) == priya.id
    people.link(priya.id, subject="https://login.acme.com 1c2b")
    assert people.unclaimed("priya.shah@acme.com", scim_only=False) is None  # theirs already
    assert people.account(priya.id).linked_by is None


def test_an_account_is_claimable_by_whom_named_it_while_no_one_signed_in_and_never_again(cluster):
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    people.link_person(emily.id, user_name="emily.carter@acme.com", actor=BY_HAND)  # an administrator's link
    assert people.account(emily.id).linked_by == "admin"
    assert people.unclaimed("emily.carter@acme.com", scim_only=False) == emily.id  # a first sign-in finds her
    assert people.unclaimed("emily.carter@acme.com", scim_only=True) is None  # SCIM does not (M3)
    people.link(emily.id, user_name="emily.c@acme.com")  # renamed outside link_person: no longer claimable
    assert people.unclaimed("emily.c@acme.com", scim_only=False) is None
    assert people.unclaimed("emily.carter@acme.com", scim_only=False) is None
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    people.link(priya.id, user_name="priya.shah@acme.com", scim_managed=True, source="scim")
    people.link(priya.id, deleted=True)  # deleted at the provider
    assert people.unclaimed("priya.shah@acme.com", scim_only=False) is None
    assert people.unclaimed("priya.shah@acme.com", scim_only=True) is None
    people.link(daniel.id, user_name="daniel.okafor@acme.com", scim_managed=True, source="scim")
    people.link(daniel.id, subject="https://login.acme.com 1c2b")  # signed in
    with pytest.raises(Taken):  # a subject stays: its account is never claimable again
        people.link(daniel.id, subject=None)
    with pytest.raises(Taken):
        people.link(daniel.id, subject="https://login.acme.com 9e9e")  # nor taken by another sign-in
    people.link(daniel.id, scim_managed=True, user_name="d.okafor@acme.com", source="scim")
    assert people.unclaimed("d.okafor@acme.com", scim_only=True) is None
    assert people.account(daniel.id).subject == "https://login.acme.com 1c2b"
    assert people.account(daniel.id).linked_by is None


def test_a_person_added_by_hand_is_linked_from_their_own_session_or_by_an_administrator_and_it_is_recorded(cluster):
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    # Link my company account: the subject of the sign-in begun from Emily's own session.
    people.link_person(emily.id, subject="https://login.acme.com 0f6a", actor=emily.id, address="203.0.113.7")
    assert people.find(subject="https://login.acme.com 0f6a") == emily.id
    with pytest.raises(Taken):  # that subject is Emily's
        people.link_person(daniel.id, subject="https://login.acme.com 0f6a", actor=daniel.id)
    with pytest.raises(Taken):  # and Emily's account is linked to the provider already
        people.link_person(emily.id, subject="https://login.acme.com 9e9e", actor=emily.id)
    # `latet server people link`: a user name, and with --scim the person handed to SCIM.
    people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    assert people.unclaimed("Daniel.Okafor@acme.com", scim_only=False) == daniel.id
    assert not people.account(daniel.id).scim_managed
    people.link_person(priya.id, user_name="priya.shah@acme.com", scim=True, actor=BY_HAND)
    assert people.account(priya.id).scim_managed
    assert people.unclaimed("priya.shah@acme.com", scim_only=True) == priya.id  # handed to SCIM
    entries = sorted(cluster.server.audit.query(action="person.linked"), key=lambda entry: entry.id)
    assert [(e.actor, e.target, e.details, e.address) for e in entries] == [
        (emily.id, emily.id, {"source": "oidc", "fields": ["subject"]}, "203.0.113.7"),
        (BY_HAND, daniel.id, {"source": "oidc", "fields": ["linked_by", "user_name"]}, None),  # a claim given
        (BY_HAND, priya.id, {"source": "scim", "fields": ["linked_by", "scim_managed", "user_name"]}, None),
    ]
    assert people.find(subject="https://login.acme.com 9e9e") is None


def test_an_api_key_has_no_account_at_the_provider(cluster):
    people = cluster.people
    key, _secret = cluster.server.identities.add_key("Billing CI")
    with pytest.raises(ValueError, match="an API key"):
        people.link(key.id, subject="https://login.acme.com 0f6a")
    with pytest.raises(ValueError, match="an API key"):
        people.link_person(key.id, user_name="billing-ci@acme.com", actor=BY_HAND)
    with pytest.raises(KeyError):
        people.link("u_nobody0000", user_name="nobody@acme.com")
    assert people.account(key.id) is None and people.find(subject="https://login.acme.com 0f6a") is None


def test_linking_an_account_is_in_the_administration_log_by_its_fields_never_their_values(cluster):
    people, emily = cluster.people, _emily(cluster)
    people.link(emily.id, subject="https://login.acme.com 0f6a", user_name="emily.carter@acme.com", claimed=["Pay"])
    people.link(emily.id, subject="https://login.acme.com 0f6a", claimed=["Platform"])  # nothing naming them changed
    people.link(emily.id, external_id="ec-0f6a", source="scim", actor="scim:s_tests")
    entries = sorted(cluster.server.audit.query(action="person.linked"), key=lambda entry: entry.id)
    assert [(e.actor, e.target, e.details) for e in entries] == [
        ("server", emily.id, {"source": "oidc", "fields": ["subject", "user_name"]}),
        ("scim:s_tests", emily.id, {"source": "scim", "fields": ["external_id"]}),
    ]
    text = json.dumps([e.details for e in entries])
    assert "acme" not in text and "0f6a" not in text


def test_the_last_enabled_administrator_is_kept_when_asked_however_they_are_one(cluster):
    people, emily = cluster.people, _emily(cluster)
    admins = people.add_group("Latet admins", actor=BY_HAND)
    people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    people.add_member(admins.id, emily.id, actor=BY_HAND)
    assert people.admins() == {emily.id}
    changes = [
        lambda: people.remove_member(admins.id, emily.id, actor=BY_HAND, keep_admin=True),
        lambda: people.revoke(admins.id, Role.ADMIN, actor=BY_HAND, keep_admin=True),
        lambda: people.remove_group(admins.id, actor=BY_HAND, keep_admin=True),
        lambda: disable_person(people, emily.id, actor=BY_HAND, keep_admin=True),
    ]
    for change in changes:
        with pytest.raises(LastAdmin):
            change()
    assert people.roles_of(emily.id) == {Role.USER, Role.ADMIN}
    assert not cluster.server.identities.current(emily.id).disabled
    assert people.revoke(admins.id, Role.ADMIN, actor=BY_HAND)  # the server's command line can
    assert people.admins() == set()


def test_a_new_mapping_keeps_the_last_administrator_when_asked(cluster):
    people, emily = cluster.people, _emily(cluster)
    people.apply_claims(emily.id, ["latet-admins"], {"latet-admins": "Latet admins"})
    people.grant(people.group("Latet admins").id, Role.ADMIN, actor=BY_HAND)
    assert people.admins() == {emily.id}
    with pytest.raises(LastAdmin):
        people.regroup({}, actor=emily.id, keep_admin=True)
    assert people.admins() == {emily.id}
    people.regroup({}, actor=BY_HAND)  # the server's command line can
    assert people.admins() == set()


def test_two_administrators_taking_the_role_from_each_other_at_once_on_two_instances_leave_one(cluster):
    a, b = (server.edition.people for server in cluster.servers)
    emily, daniel = _emily(cluster), _daniel(cluster)
    for person in (emily, daniel):
        a.grant(person.id, Role.ADMIN, actor=BY_HAND)
    counted, release, refused = threading.Event(), threading.Event(), []

    def emily_takes_daniels():  # on A: counted, and not committed until released
        with a.changing(emily.id) as within:
            a.revoke(daniel.id, Role.ADMIN, within=within, keep_admin=True)
            counted.set()
            release.wait(10)

    def daniel_takes_emilys():  # on B, meanwhile
        try:
            b.revoke(emily.id, Role.ADMIN, actor=daniel.id, keep_admin=True)
        except LastAdmin:
            refused.append(daniel.id)

    first = threading.Thread(target=emily_takes_daniels)
    first.start()
    assert counted.wait(10)
    second = threading.Thread(target=daniel_takes_emilys)
    second.start()
    second.join(1.0)  # without the lock it is done by now, having counted Emily still an administrator
    release.set()
    first.join(10)
    second.join(10)
    assert refused == [daniel.id] and a.admins() == {emily.id}


def test_a_disabled_person_is_no_administrator(cluster):
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    for person in (emily, daniel):
        people.grant(person.id, Role.ADMIN, actor=BY_HAND)
    cluster.server.identities.disable(daniel.id)
    assert people.admins() == {emily.id}


def test_who_holds_a_role_is_read_through_groups_too(cluster):
    """E5: who decides for the security team, read in one query, past every copy: the enabled people given the role,
    themselves or through a group; given ``conn``, as its transaction finds them."""
    people = cluster.people
    mark = add_person(cluster, "Mark Ellis", "mark.ellis@acme.com")
    laura = add_person(cluster, "Laura Bennett", "laura.bennett@acme.com")
    daniel = _daniel(cluster)
    _emily(cluster)  # a user alone
    security = people.add_group("Security", actor=BY_HAND)
    people.grant(security.id, Role.SECURITY, actor=BY_HAND)
    people.add_member(security.id, laura.id, actor=BY_HAND)
    for person in (mark, daniel):
        people.grant(person.id, Role.SECURITY, actor=BY_HAND)
    cluster.server.identities.disable(daniel.id)
    assert people.holding(Role.SECURITY) == {mark.id, laura.id}
    with cluster.server.db.transaction() as conn:
        assert people.holding(Role.SECURITY, conn=conn) == {mark.id, laura.id}
    assert people.holding(Role.AUDITOR) == set()


def test_who_controls_a_key_is_read_through_groups_too(cluster):
    """E5: who decides on an API key's change, read in one query with no row held: the enabled people who control it,
    themselves or through a group."""
    people, emily, daniel = cluster.people, _emily(cluster), _daniel(cluster)
    paul = add_person(cluster, "Paul Ferris", "paul.ferris@acme.com")
    key, _secret = cluster.server.identities.add_key("Billing CI")
    other, _secret = cluster.server.identities.add_key("Release CI")
    platform = people.add_group("Platform", actor=BY_HAND)
    people.add_member(platform.id, daniel.id, actor=BY_HAND)
    for subject in (emily.id, platform.id, paul.id):
        people.add_controller(key.id, subject, actor=BY_HAND)
    cluster.server.identities.disable(paul.id)
    assert people.controlling(key.id) == {emily.id, daniel.id}
    with cluster.server.db.transaction() as conn:
        assert people.controlling(key.id, conn=conn) == {emily.id, daniel.id}
    assert people.controlling(other.id) == set()


def test_a_change_through_one_instance_applies_at_the_next_read_on_the_other(cluster):
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily = _emily(cluster)
    assert b.roles_of(emily.id) == {Role.USER}  # read, and kept, on B
    a.grant(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}
    a.revoke(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER}


def test_a_read_begun_before_a_change_keeps_nothing_once_another_request_knows_the_change(cluster, monkeypatch):
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily = _emily(cluster)
    read = b._read

    def read_then_changed(identity_id, now):
        seen = read(identity_id, now)  # what PostgreSQL held before the change
        monkeypatch.setattr(b, "_read", read)
        a.grant(emily.id, Role.ADMIN, actor=BY_HAND)
        assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}  # another request on B, meanwhile, finds the change
        return seen

    monkeypatch.setattr(b, "_read", read_then_changed)
    assert b.roles_of(emily.id) == {Role.USER}  # begun before the change: answered as it read
    assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}  # the next read: never the stale copy (L1)


def test_without_valkey_the_roles_are_read_from_the_database_and_it_is_said_once_a_minute(cluster, monkeypatch, caplog):
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily = _emily(cluster)
    a.grant(emily.id, Role.AUDITOR, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.AUDITOR}  # kept on B

    def down(*args, **kwargs):
        raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")

    monkeypatch.setattr(b, "_step", down)  # the step that reads both generations, and the plain read after it
    monkeypatch.setattr(b.bus.client, "mget", down)
    with caplog.at_level(logging.WARNING, logger="latet.ee.people"):
        a.revoke(emily.id, Role.AUDITOR, actor=BY_HAND)
        for _ in range(3):
            assert b.roles_of(emily.id) == {Role.USER}  # nothing kept is trusted
    said = [record.getMessage() for record in caplog.records if record.name == "latet.ee.people"]
    assert len(said) == 1 and "ConnectionError" in said[0] and "valkey.acme.lan" not in said[0]


def test_a_message_on_ee_people_drops_what_another_instance_keeps(cluster):
    """An older instance's message, ``changed`` alone, drops every copy (gate 4 keeps that for a rolling upgrade)."""
    b, emily = cluster.servers[1].edition.people, _emily(cluster)
    settle(lambda: b.roles_of(emily.id) and bool(b._seen))  # Emily's arrival, heard meanwhile, may forget a first read
    publish(cluster.server.bus, CHANNEL, {"changed": [emily.id]})
    settle(lambda: not b._seen)


def test_only_a_change_that_names_a_person_or_a_group_tells_the_instances(cluster):
    """M4: a change of E4's settings, an export or a test names neither a person nor a group: the generation in Valkey
    stays as it was and what every instance read is kept; a change that names a person, a group or the sign-in's
    settings changes it. The instance is made deaf first (its listener stopped, waited for five seconds at most): the
    message of Emily's arrival, heard late, would drop what it read whatever the change (the review of E5's Task 7)."""
    people, emily = _deaf(cluster.server), _emily(cluster)
    people.roles_of(emily.id)
    before, done = cluster.server.bus.get(GENERATION), []
    own = _own_generation(cluster, emily)
    for note in (("siem.changed", "siem", {}), ("reports.exported", None, {}), ("siem.tested", "siem", {})):
        people.committed(Changing(None, notes=[note], after=[lambda: done.append(True)]))  # type: ignore[arg-type]
    assert cluster.server.bus.get(GENERATION) == before and people._seen and done == [True] * 3  # what follows, still
    assert _own_generation(cluster, emily) == own
    # Gate 4: a role given to Emily moves her own generation alone, and her copy goes; a group's change or the sign-in's
    # settings move the server's, and every copy goes.
    people.committed(Changing(None, notes=[("role.granted", emily.id, {"role": "auditor"})]))  # type: ignore[arg-type]
    assert cluster.server.bus.get(GENERATION) == before and _own_generation(cluster, emily) not in (own, None)
    assert emily.id not in people._seen
    people.roles_of(emily.id)
    named = before
    people.committed(Changing(None, notes=[("group.changed", "g_abcdefghij", None)]))  # type: ignore[arg-type]
    grouped = cluster.server.bus.get(GENERATION)
    assert grouped != named and not people._seen
    people.roles_of(emily.id)  # the sign-in's settings: whose session opens a key's space, which every stream asks
    people.committed(Changing(None, notes=[("sign-in.changed", "sso", {"section": "sso"})]))  # type: ignore[arg-type]
    assert cluster.server.bus.get(GENERATION) != grouped and not people._seen


def test_an_identity_removed_on_one_instance_leaves_nothing_kept_of_it_on_the_other(cluster):
    b, daniel = cluster.servers[1].edition.people, _daniel(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    cluster.people.add_controller(key.id, daniel.id, actor=BY_HAND)
    assert b.controlled_by(daniel.id) == [key.ref()]  # kept on B
    cluster.server.identities.remove(key.id)  # E0b's removal: no change of People's, told on `identities`
    settle(lambda: b.controlled_by(daniel.id) == [])


def test_an_account_printed_names_nothing_of_the_person(cluster):
    people, emily = cluster.people, _emily(cluster)
    people.link(emily.id, subject="https://login.acme.com 0f6a", user_name="emily.carter@acme.com", external_id="ec")
    people.link(emily.id, scim={"displayName": "Emily Carter"}, claimed=["Payments"])
    printed = repr(people.account(emily.id))
    assert emily.id in printed
    assert not any(word in printed for word in ("acme", "0f6a", "'ec'", "Emily", "Payments"))


def test_each_change_is_in_the_administration_log_by_who_made_it_and_holds_no_name(cluster):
    people, emily = cluster.people, _emily(cluster)
    payments = people.add_group("Payments", actor="u_admintests", address="203.0.113.7")
    people.grant(emily.id, Role.AUDITOR, actor="u_admintests", address="203.0.113.7")
    entries = sorted(cluster.server.audit.query(actor="u_admintests"), key=lambda entry: entry.id)
    assert [(e.action, e.target, e.details, e.address) for e in entries] == [
        ("group.added", payments.id, {"source": "local"}, "203.0.113.7"),
        ("role.granted", emily.id, {"role": "auditor"}, "203.0.113.7"),
    ]
    text = json.dumps([e.details for e in entries])
    assert "Payments" not in text and "Emily" not in text


def test_what_belongs_to_a_person_removed_from_the_server_goes_with_them(cluster):
    people, emily = cluster.people, _emily(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    payments = people.add_group("Payments", actor=BY_HAND)
    people.add_member(payments.id, emily.id, actor=BY_HAND)
    people.grant(emily.id, Role.SECURITY, actor=BY_HAND)
    people.add_controller(key.id, emily.id, actor=BY_HAND)
    people.link(emily.id, user_name="emily.carter@acme.com")
    cluster.server.identities.remove(emily.id)
    assert people.members(payments.id) == [] and people.account(emily.id) is None
    assert people.given(emily.id) == frozenset() and people.controllers(key.id) == []  # F19: no row left behind
    assert people.directory().roles == {}


def test_a_change_whose_entry_cannot_be_written_is_not_made(cluster, monkeypatch):
    people, emily = cluster.people, _emily(cluster)
    record = cluster.server.audit.record

    def failing(actor, action, *args, **kwargs):
        if action == "role.granted":
            raise RuntimeError("the administration log does not answer")
        return record(actor, action, *args, **kwargs)

    monkeypatch.setattr(cluster.server.audit, "record", failing)
    with pytest.raises(RuntimeError):
        people.grant(emily.id, Role.ADMIN, actor=BY_HAND)
    assert people.given(emily.id) == frozenset() and people.roles_of(emily.id) == {Role.USER}


def _two_changes_then_a_failure(people, emily):
    with people.changing(BY_HAND, "203.0.113.7") as within:
        payments = people.add_group("Payments", within=within)
        people.add_member(payments.id, emily.id, within=within)
        raise RuntimeError("something after them failed")


def test_the_changes_of_one_transaction_are_kept_together_with_their_entries_or_not_at_all(cluster):
    people, emily = cluster.people, _emily(cluster)
    with pytest.raises(RuntimeError):
        _two_changes_then_a_failure(people, emily)
    assert people.group("Payments") is None and cluster.server.audit.query(action="group.added") == []
    with people.changing(BY_HAND, "203.0.113.7") as within:
        payments = people.add_group("Payments", within=within)
        people.add_member(payments.id, emily.id, within=within)
    entries = sorted(cluster.server.audit.query(actor=BY_HAND), key=lambda entry: entry.id)
    assert [(e.action, e.address) for e in entries if e.action in ("group.added", "member.added")] == [
        ("group.added", "203.0.113.7"),
        ("member.added", "203.0.113.7"),
    ]
    assert people.groups_of(emily.id) == [payments]


def test_a_disabled_persons_sessions_end_on_every_instance_through_the_identities_message(cluster):
    emily = _emily(cluster)
    session = cluster.server.sign_ins.open(emily.id)
    assert disable_person(cluster.people, emily.id, actor=BY_HAND)
    assert cluster.server.identities.get(emily.id).disabled
    settle(lambda: all(server.sign_ins.subject_of(session) is None for server in cluster.servers))
    [entry] = cluster.server.audit.query(action="person.disabled")
    assert (entry.actor, entry.target) == (BY_HAND, emily.id)


def test_scim_lifts_only_a_disable_of_its_own_never_an_administrators(cluster):
    people, identities = cluster.people, cluster.server.identities
    emily, daniel, priya = _emily(cluster), _daniel(cluster), add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    assert disable_person(people, emily.id, by="local", actor=BY_HAND)
    assert people.account(emily.id).disabled_by == "local"
    assert not enable_person(people, emily.id, by="scim", actor="scim:s_tests")  # L7: SCIM does not undo it
    assert identities.current(emily.id).disabled
    assert disable_person(people, daniel.id, by="scim", actor="scim:s_tests")
    assert people.account(daniel.id).disabled_by == "scim"
    assert disable_person(people, daniel.id, by="local", actor=BY_HAND)  # disabled by an administrator now
    assert not enable_person(people, daniel.id, by="scim", actor="scim:s_tests")
    assert identities.current(daniel.id).disabled and people.account(daniel.id).disabled_by == "local"
    assert enable_person(people, daniel.id, actor=BY_HAND)  # an administrator lifts any
    assert not identities.current(daniel.id).disabled and people.account(daniel.id).disabled_by is None
    identities.disable(priya.id)  # disabled outside E3 (E0c's `latet server people disable`): no one says by whom
    assert not enable_person(people, priya.id, by="scim", actor="scim:s_tests")
    assert identities.current(priya.id).disabled
    assert disable_person(people, emily.id, by="scim", actor="scim:s_tests") is False  # disabled already: stays local
    assert people.account(emily.id).disabled_by == "local"
    assert enable_person(people, emily.id, actor=BY_HAND)
    assert disable_person(people, emily.id, by="scim", actor="scim:s_tests")
    assert enable_person(people, emily.id, by="scim", actor="scim:s_tests")  # its own disable, SCIM lifts
    entries = sorted(cluster.server.audit.query(action="person.*"), key=lambda entry: entry.id)
    assert [(e.action, e.target, e.details) for e in entries if e.action != "person.linked"][:3] == [
        ("person.disabled", emily.id, {"by": "local"}),
        ("person.disabled", daniel.id, {"by": "scim"}),
        ("person.disabled", daniel.id, {"by": "local"}),
    ]


def test_a_first_sign_in_claims_an_account_only_while_it_is_still_claimable(cluster):
    """Task 2's carry: ``link(claim=)`` checks again, the account's row held, what ``unclaimed`` found."""
    people = cluster.people
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    with pytest.raises(Taken):  # another user name than the account's
        people.link(daniel.id, subject="https://login.acme.com x", claim="sam.rivera@acme.com")
    people.link(daniel.id, deleted=True)
    with pytest.raises(Taken):  # deleted at the provider meanwhile
        people.link(daniel.id, subject="https://login.acme.com x", claim="Daniel.Okafor@acme.com")
    people.link(daniel.id, deleted=False)
    people.link(daniel.id, subject="https://login.acme.com x", claim="Daniel.Okafor@acme.com")
    assert people.account(daniel.id).subject == "https://login.acme.com x"
    with pytest.raises(Taken):  # claimed once
        people.link(daniel.id, subject="https://login.acme.com x", claim="daniel.okafor@acme.com")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    people.link(emily.id, user_name="emily.carter@acme.com")  # a user name no one made claimable
    with pytest.raises(Taken):
        people.link(emily.id, subject="https://login.acme.com e", claim="emily.carter@acme.com")


@pytest.mark.parametrize("heard", ["drop", "forget"])
def test_a_read_during_which_another_instance_changed_people_keeps_nothing(cluster, monkeypatch, heard):
    """Task 2's carry: ``drop()`` (a message on ee:people) or ``forget()`` (one naming the person, or the identities
    message) during a read: what was read answers, and is not kept."""
    people = cluster.people
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    read = people._read

    def read_then_dropped(identity_id, now):
        found = read(identity_id, now)
        if heard == "drop":
            people.drop()  # another instance's change heard while this read was under way
        else:
            people.forget(emily.id)
        return found

    monkeypatch.setattr(people, "_read", read_then_dropped)
    assert people.roles_of(emily.id) == {Role.USER}
    assert emily.id not in people._seen


# --- E3's release gate 4: a copy kept under the server's generation and the person's own ------------------------------


def _reads(monkeypatch, people):
    """Whose roles ``people`` reads from PostgreSQL from now on, in order."""
    read, asked = people._read, []

    def spied(identity_id, now):
        asked.append(identity_id)
        return read(identity_id, now)

    monkeypatch.setattr(people, "_read", spied)
    return asked


def _own_generation(cluster, person):
    """The person's own generation as Valkey holds it (None: absent)."""
    bus = cluster.server.bus
    return bus.get(cluster.people.generation_name(person.id))


def test_one_persons_role_keeps_everyone_elses_copy_on_every_instance(cluster, monkeypatch):
    """Gate 4: one person's change used to empty every instance's copy, and every person active anywhere was read
    again. Now B reads again only the person it touched: at her next read she has it, and Daniel is served as kept."""
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily, daniel = _emily(cluster), _daniel(cluster)
    assert b.roles_of(emily.id) == {Role.USER} and b.roles_of(daniel.id) == {Role.USER}
    asked = _reads(monkeypatch, b)
    a.grant(emily.id, Role.AUDITOR, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.AUDITOR}
    assert b.roles_of(daniel.id) == {Role.USER}
    assert asked == [emily.id]
    assert a.roles_of(emily.id) == {Role.USER, Role.AUDITOR}  # and on the instance that made it


def test_a_role_taken_from_a_person_is_gone_at_their_next_request_on_another_instance(cluster):
    """L8.1: the revocation, the direction that matters, through the person's generation alone."""
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily = _emily(cluster)
    a.grant(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}
    a.revoke(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER}


def test_leaving_a_group_that_carries_a_role_takes_the_role_at_the_next_request_on_another_instance(cluster):
    """L8.1, ``member.removed``: the entry names the group, and the member in its details."""
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily = _emily(cluster)
    security = a.add_group("Security team", actor=BY_HAND)
    a.grant(security.id, Role.SECURITY, actor=BY_HAND)
    a.add_member(security.id, emily.id, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.SECURITY}
    a.remove_member(security.id, emily.id, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER} and b.groups_of(emily.id) == []


def test_a_member_added_drops_that_members_copy_alone(cluster, monkeypatch):
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily, daniel = _emily(cluster), _daniel(cluster)
    payments = a.add_group("Payments", actor=BY_HAND)
    b.groups_of(emily.id), b.groups_of(daniel.id)
    asked = _reads(monkeypatch, b)
    a.add_member(payments.id, emily.id, actor=BY_HAND)
    assert b.groups_of(emily.id) == [payments] and b.groups_of(daniel.id) == []
    assert asked == [emily.id]


def test_a_role_given_to_a_group_reaches_each_member_at_their_next_request_on_every_instance(cluster):
    """A group's change touches everyone: its members are not in its entry."""
    a, b = cluster.people, cluster.servers[1].edition.people
    emily, daniel = _emily(cluster), _daniel(cluster)
    leads = a.add_group("Payments leads", actor=BY_HAND)
    for person in (emily, daniel):
        a.add_member(leads.id, person.id, actor=BY_HAND)
    for people in (a, b):
        assert people.roles_of(emily.id) == {Role.USER} and people.roles_of(daniel.id) == {Role.USER}
    before = cluster.server.bus.get(GENERATION)
    a.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    assert cluster.server.bus.get(GENERATION) != before  # the server's: everyone's copy
    for people in (a, b):
        assert people.roles_of(emily.id) == {Role.USER, Role.GROUP_LEAD}
        assert people.roles_of(daniel.id) == {Role.USER, Role.GROUP_LEAD}


def test_a_key_controlled_by_a_person_drops_theirs_and_by_a_group_everyones(cluster, monkeypatch):
    a, b = cluster.people, _deaf(cluster.servers[1])
    emily, daniel = _emily(cluster), _daniel(cluster)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = a.add_group("Platform", actor=BY_HAND)
    a.add_member(platform.id, daniel.id, actor=BY_HAND)
    b.controlled_by(emily.id), b.controlled_by(daniel.id)
    asked = _reads(monkeypatch, b)
    a.add_controller(key.id, emily.id, actor=BY_HAND)
    assert b.controlled_by(emily.id) == [key.ref()] and b.controlled_by(daniel.id) == []
    assert asked == [emily.id]
    a.add_controller(key.id, platform.id, actor=BY_HAND)
    assert b.controlled_by(daniel.id) == [key.ref()] and b.controlled_by(emily.id) == [key.ref()]
    assert sorted(asked) == sorted([emily.id, daniel.id, emily.id])
    a.remove_controller(key.id, emily.id, actor=BY_HAND)  # her own control: she is not in the group
    assert b.controlled_by(emily.id) == [] and b.controlled_by(daniel.id) == [key.ref()]


def _notes():
    """Every ``.note(`` call in Enterprise's code: where it is, and the call."""
    import latet.ee

    root = Path(latet.ee.__file__).parent
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "note":
                yield f"{path.relative_to(root).as_posix()}:{node.lineno}", node


# The one action that is no literal: a decision's (E5), whose change says it touches nobody (``people = False``).
_NOT_LITERAL = {"approvals_api.py": "action_of(row, capacity)"}


def test_every_note_is_a_literal_or_allowed_and_the_narrowed_ones_carry_their_person():
    """M2: what a change touches is read from its entries' actions, so each is one the code can be read for: a string
    literal (or the one allowed exception); every action that narrows to one person is noted somewhere under its own
    name, so a rename fails here rather than losing its narrowing; and each ``member.*`` and ``key.controller.*``
    passes the person in a details literal where the classifier looks for them."""
    noted: set[str] = set()
    for where, call in _notes():
        action = call.args[0] if call.args else None
        # One literal or another (``'key.enabled' if enabled else 'key.disabled'``): both are read.
        actions = [action.body, action.orelse] if isinstance(action, ast.IfExp) else [action]
        if not all(isinstance(one, ast.Constant) and isinstance(one.value, str) for one in actions):
            assert _NOT_LITERAL.get(where.split(":")[0]) == ast.unparse(action), where
            continue
        literals = [one.value for one in actions if isinstance(one, ast.Constant)]
        noted.update(literals)
        places = {people_module.NARROWED.get(literal) for literal in literals}
        assert len(places) == 1, where  # one call, one way of naming its person
        place = places.pop()
        if place in ("identity", "subject"):
            details = call.args[2] if len(call.args) > 2 else None
            assert isinstance(details, ast.Dict), where
            keys = {key.value for key in details.keys if isinstance(key, ast.Constant)}
            assert place in keys, where
    assert set(people_module.NARROWED) <= noted, set(people_module.NARROWED) - noted


def test_what_people_false_changes_touches_nobody(cluster, monkeypatch):
    """L8.2, E5's R38: a change that says it touches nobody moves no generation and tells no instance, whatever its
    entries name."""
    people, emily = _deaf(cluster.server), _emily(cluster)
    people.roles_of(emily.id)
    told = []
    monkeypatch.setattr(people_module, "publish", lambda bus, channel, message: told.append(message))
    before = (cluster.server.bus.get(GENERATION), _own_generation(cluster, emily))
    notes = [("role.granted", emily.id, {"role": "auditor"}), ("group.changed", "g_abcdefghij", None)]
    people.committed(Changing(None, notes=notes, people=False))  # type: ignore[arg-type]
    assert (cluster.server.bus.get(GENERATION), _own_generation(cluster, emily)) == before
    assert told == [] and emily.id in people._seen


@pytest.mark.parametrize(
    "note",
    [
        ("member.added", "g_abcdefghij", {"source": "local"}),  # no identity
        ("member.removed", "g_abcdefghij", {"identity": "Emily Carter"}),  # not an id
        ("member.added", "g_abcdefghij", {"identity": "u_ABCDEFGHIJ"}),  # not an id as the server makes one
        ("member.added", "g_abcdefghij", ["u_abcdefghij"]),  # details not an object
        ("member.added", "g_abcdefghij", None),
        ("key.controller.added", "u_abcdefghij", {"subject": "g_abcdefghij"}),  # a group
        ("key.controller.removed", "u_abcdefghij", {"subject": 7}),
        ("role.granted", "g_abcdefghij", {"role": "auditor"}),  # a group's role
        ("person.changed", "sso", {"fields": []}),  # a listed action that names no person
        ("group.added", "g_abcdefghij", {"source": "local"}),  # not listed
        ("person.removed", "u_abcdefghij", None),  # not listed, now or later
    ],
)
def test_a_narrowed_entry_without_its_person_touches_everyone(cluster, monkeypatch, note):
    """L8.2, M2: an entry the classifier cannot narrow to one well-formed person touches everyone, and nothing raises
    after the commit."""
    people, emily = _deaf(cluster.server), _emily(cluster)
    people.roles_of(emily.id)
    told = []
    monkeypatch.setattr(people_module, "publish", lambda bus, channel, message: told.append(message))
    before = cluster.server.bus.get(GENERATION)
    people.committed(Changing(None, notes=[note]))  # type: ignore[arg-type]
    assert cluster.server.bus.get(GENERATION) != before and not people._seen
    assert [message.get("everyone") for message in told] == [True]


def test_a_narrowed_change_tells_the_people_it_touched_and_a_wide_one_everyone(cluster, monkeypatch):
    people, emily, daniel = _deaf(cluster.server), _emily(cluster), _daniel(cluster)
    told = []
    monkeypatch.setattr(people_module, "publish", lambda bus, channel, message: told.append((channel, message)))
    before = cluster.server.bus.get(GENERATION)
    notes = [("role.granted", emily.id, {"role": "auditor"}), ("member.added", "g_abcdefghij", {"identity": daniel.id})]
    people.committed(Changing(None, notes=notes))  # type: ignore[arg-type]
    assert cluster.server.bus.get(GENERATION) == before
    assert told == [(CHANNEL, {"changed": sorted([emily.id, "g_abcdefghij"]), "people": sorted([emily.id, daniel.id])})]
    monkeypatch.setattr(people_module, "TOUCHED_MOST", 1)
    people.committed(Changing(None, notes=notes))  # type: ignore[arg-type]
    assert told[-1][1]["everyone"] is True and "people" not in told[-1][1]
    assert cluster.server.bus.get(GENERATION) != before


def test_an_ee_people_message_forgets_the_people_it_names_and_an_older_one_drops_all(cluster):
    """M1: a newer instance's message names the people it touched, and B forgets them alone; an older instance's
    names none (``changed`` alone), and B drops all, as one that says everyone or is malformed."""
    b = cluster.servers[1].edition.people
    emily, daniel = _emily(cluster), _daniel(cluster)

    def kept():
        return {emily.id, daniel.id} <= set(b._seen)

    def read_both():
        settle(lambda: (b.roles_of(emily.id), b.roles_of(daniel.id)) and kept())

    for message, left in (
        ({"changed": [emily.id], "people": [emily.id]}, {daniel.id}),
        ({"changed": [emily.id]}, set()),  # an older instance's
        ({"changed": [], "everyone": True}, set()),
        ({"changed": [], "people": [emily.id, "Emily Carter"]}, set()),  # malformed: everyone
        ({"changed": [], "people": "u_abcdefghij"}, set()),
    ):
        read_both()
        publish(cluster.server.bus, CHANNEL, message)
        settle(lambda left=left: set(b._seen) & {emily.id, daniel.id} == left)


def test_a_generation_valkey_lost_brings_no_old_copy_back(cluster):
    """L1: a person's generation is a key with a life, which Valkey may evict before it ends. A read finds none and
    sets a new one at once, in the same step, so a copy is never kept under "none", and none comes back."""
    a, b = cluster.people, _deaf(cluster.servers[1])
    bus, emily = cluster.server.bus, _emily(cluster)
    a.grant(emily.id, Role.AUDITOR, actor=BY_HAND)
    bus.delete(a.generation_name(emily.id))  # evicted
    assert b.roles_of(emily.id) == {Role.USER, Role.AUDITOR}
    assert _own_generation(cluster, emily) is not None  # set by the read
    a.revoke(emily.id, Role.AUDITOR, actor=BY_HAND)
    bus.delete(a.generation_name(emily.id))  # evicted again, before B's next read
    assert b.roles_of(emily.id) == {Role.USER}
    bus.delete(GENERATION)  # the server's too
    a.grant(emily.id, Role.AUDITOR, actor=BY_HAND)
    bus.delete(GENERATION)
    bus.delete(a.generation_name(emily.id))
    assert b.roles_of(emily.id) == {Role.USER, Role.AUDITOR}
    ttl = bus.client.pttl(bus.key(a.generation_name(emily.id)))
    assert people_module.KEEP * 1000 < ttl <= people_module.PERSON_LIFE  # it ends by itself, long past a copy's life


def test_a_persons_generation_valkey_did_not_take_drops_this_instances_copy_and_still_publishes(
    cluster, monkeypatch, caplog
):
    a = cluster.people
    emily, daniel = _emily(cluster), _daniel(cluster)
    a.roles_of(emily.id), a.roles_of(daniel.id)
    told = []
    monkeypatch.setattr(people_module, "publish", lambda bus, channel, message: told.append(message))

    class Refusing:
        def set(self, *args, **kwargs):
            return self

        def execute(self):
            raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")

    monkeypatch.setattr(a.bus.client, "pipeline", lambda *args, **kwargs: Refusing())
    with caplog.at_level(logging.WARNING, logger="latet.ee.people"):
        a.grant(emily.id, Role.AUDITOR, actor=BY_HAND)
    assert not a._seen  # Daniel's too: this instance trusts nothing it kept
    assert [message.get("everyone") for message in told] == [True]
    said = [record.getMessage() for record in caplog.records if record.name == "latet.ee.people"]
    assert said and "ConnectionError" in said[0] and "valkey.acme.lan" not in said[0]


def test_a_valkey_that_refuses_the_step_is_read_with_a_plain_get_and_keeps_a_copy_only_under_both(
    cluster, monkeypatch, caplog
):
    """N3: a Valkey full under ``noeviction`` refuses any script that may write, even one whose writes would change
    nothing. Then both generations are read with a plain MGET: a copy is kept while both are there, and none while
    either is missing (read from PostgreSQL at each request)."""
    import redis

    b, emily = _deaf(cluster.servers[1]), _emily(cluster)
    b.roles_of(emily.id)  # both generations set by the step

    def full(*args, **kwargs):
        raise redis.exceptions.ResponseError("OOM command not allowed when used memory > 'maxmemory'.")

    monkeypatch.setattr(b, "_step", full)
    asked = _reads(monkeypatch, b)
    with caplog.at_level(logging.WARNING, logger="latet.ee.people"):
        for _ in range(3):
            assert b.roles_of(emily.id) == {Role.USER}
        assert asked == []  # both there: the copy is used
        cluster.server.bus.delete(b.generation_name(emily.id))
        for _ in range(3):
            assert b.roles_of(emily.id) == {Role.USER}
    assert asked == [emily.id] * 3  # one missing: nothing kept
    said = [record.getMessage() for record in caplog.records if record.name == "latet.ee.people"]
    assert len(said) == 1 and "ResponseError" in said[0]


class _Refusing:
    """A pipeline whose writes Valkey does not take."""

    def set(self, *args, **kwargs):
        return self

    def execute(self):
        raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")


def test_a_copy_read_before_valkey_stopped_answering_is_not_served_after_it(cluster, monkeypatch):
    """Fix round 1, L1: B keeps Emily's admin role; Valkey stops answering B, and B serves a request of hers meanwhile;
    on A her admin role is revoked, and Valkey takes neither her new generation nor the message (A cannot reach it
    either); Valkey answers again with its keys as they were. B's copy from before the outage must not come back: a read
    Valkey does not answer drops every copy, as one did before gate 4, and the next answered read finds none."""
    a, b, emily = cluster.people, _deaf(cluster.servers[1]), _emily(cluster)
    a.grant(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}  # kept on B

    def down(*args, **kwargs):
        raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")

    with monkeypatch.context() as outage:
        outage.setattr(b, "_step", down)
        outage.setattr(b.bus.client, "mget", down)
        assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}  # read from PostgreSQL while Valkey is away
        outage.setattr(a.bus.client, "pipeline", lambda *args, **kwargs: _Refusing())
        outage.setattr(people_module, "publish", lambda bus, channel, message: None)  # no message gets through
        a.revoke(emily.id, Role.ADMIN, actor=BY_HAND)
    assert b.roles_of(emily.id) == {Role.USER}  # Valkey back: read afresh, never the copy from before


def test_a_read_under_way_while_valkey_stopped_answering_keeps_nothing(cluster, monkeypatch):
    """Fix round 1, L1: B's read of Emily is under way, its generations read and PostgreSQL still giving her admin role;
    meanwhile another request on B finds Valkey not answering, her role is revoked on A while Valkey takes nothing, and
    Valkey answers the next request again. What that read under way read answers it, and is not kept."""
    a, b, emily = cluster.people, _deaf(cluster.servers[1]), _emily(cluster)
    a.grant(emily.id, Role.ADMIN, actor=BY_HAND)
    read = b._read

    def down(*args, **kwargs):
        raise ConnectionError("Error 111 connecting to valkey.acme.lan:6379")

    def read_through_an_outage(identity_id, now):
        found = read(identity_id, now)  # her admin role, as PostgreSQL has it before the revocation
        with monkeypatch.context() as outage:
            outage.setattr(b, "_step", down)
            outage.setattr(b.bus.client, "mget", down)
            assert b._generations(emily.id) is None  # another request, meanwhile: Valkey does not answer
            outage.setattr(a.bus.client, "pipeline", lambda *args, **kwargs: _Refusing())
            outage.setattr(people_module, "publish", lambda bus, channel, message: None)
            a.revoke(emily.id, Role.ADMIN, actor=BY_HAND)
        assert b._generations(emily.id) is not None  # and another, once Valkey answers again
        return found

    monkeypatch.setattr(b, "_read", read_through_an_outage)
    assert b.roles_of(emily.id) == {Role.USER, Role.ADMIN}  # answered as it read
    monkeypatch.setattr(b, "_read", read)
    assert b.roles_of(emily.id) == {Role.USER}


class _Woken:
    """A key space whose wake records whether B's People still keeps the person's copy at that moment."""

    def __init__(self, people, person):
        self.people, self.person, self.woken = people, person, []

    def heard(self):
        self.woken.append(self.person in self.people._seen)


def test_each_listener_forgets_before_it_wakes_the_streams(cluster, monkeypatch):
    """Fix round 1, L2 (M1): ee:people and identities, as B's listener thread calls them: each wakes the streams open in
    API keys' spaces, and only once the copy the message names is gone, so that a woken stream reads afresh."""
    edition = cluster.servers[1].edition
    _deaf(cluster.servers[1])  # only the calls below reach B
    b, emily = edition.people, _emily(cluster)
    woken = _Woken(b, emily.id)
    monkeypatch.setattr(edition, "key_space", woken)
    b.roles_of(emily.id)
    assert emily.id in b._seen
    edition._people_heard({"changed": [emily.id], "people": [emily.id]})
    b.roles_of(emily.id)
    assert emily.id in b._seen
    edition._identity_heard({"id": emily.id, "event": "updated"})
    assert woken.woken == [False, False]  # woken once by each, each after the copy went


# --- fix round 2: what an instance writes as it stops ----------------------------------------------------------------


def _sign_in_event(clock, person):
    """A sign-in of ``person`` in no group, as E4 records one: its first group is looked up when it is written."""
    return make_event("sign-in", clock=clock, identity=person.id, kind="person", outcome="signed-in")


def test_a_closing_instance_reads_people_from_the_database_and_leaves_valkey_alone(cluster, monkeypatch):
    """Fix round 2: an edition that closes writes E4's events still waiting, and each names its person's first group,
    read through People. Read so, gate 4's get-or-set gave the person a generation in Valkey, after the instance had
    begun to stop. A closing instance's People reads PostgreSQL alone, keeps nothing and asks nothing of Valkey."""
    server = cluster.servers[1]
    edition, b, emily = server.edition, server.edition.people, _emily(cluster)
    asked, read = [], []
    generations, read_db = b._generations, b._read

    def asking(identity_id):
        asked.append(identity_id)
        return generations(identity_id)

    def reading(identity_id, now):
        read.append(identity_id)
        return read_db(identity_id, now)

    monkeypatch.setattr(b, "_generations", asking)
    monkeypatch.setattr(b, "_read", reading)
    edition.record(_sign_in_event(cluster.clock, emily))
    edition.close()  # E4's events written as it closes: her first group read then
    assert emily.id in read  # the event was written, its group read through People
    assert asked == []  # and Valkey never asked
    assert b.roles_of(emily.id) == {Role.USER} and asked == [] and not b._seen


def test_a_test_leaves_nothing_in_valkey_once_its_instances_close(licensed, clock, monkeypatch):
    """Fix round 2: the tests' instances (tests/ee/servers.py) purged the test's keys before their last instance had
    stopped, and what that instance wrote as it closed (E4's events, and the generations their groups read) stayed in
    the test Valkey. Every instance stops first now, then the keys go."""
    monkeypatch.setenv("LATET_MASTER_KEY", MASTER_KEY)
    instances = Instances(clock)
    try:
        server = instances.open()
        server.edition.install_license(licensed.make(), actor=BY_HAND)
        server.edition.refresh()
        emily, _code = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
        assert server.edition.events is not None
        server.edition.events.pipeline.every = 5.0  # it waits for more until the instance closes
        server.edition.record(_sign_in_event(clock, emily))
    finally:
        instances.close()
    found = keys_left(instances.prefix)
    purge(instances.prefix)  # whatever a failure left
    assert found == []


def test_a_confirming_sign_in_takes_the_persons_row_before_the_accounts_as_scim_does(cluster):
    """The claim fix's re-review, R1: SCIM's write of a person (``Scim._held``) holds their identity's row, then their
    account's, the order every change of People takes them in. A sign-in that confirms the user name of a renamed
    account (``People.confirmed``), then keeps the groups of its ID token (``apply_claims``, with `accounts: group` or
    `open`), took them the other way round, and with SCIM writing the same person at that moment PostgreSQL aborted one
    of the two as a deadlock. Here SCIM's transaction holds Emily's identity; the sign-in waits for it before it touches
    her account; SCIM's then takes her account and commits, and the sign-in goes on and ends the mark."""
    import queue

    people, db = cluster.people, cluster.server.db
    emily, _code = cluster.server.identities.add_person("Emily Carter", None, source="scim")
    people.link(emily.id, source="scim", user_name="emily.carter@acme.com", scim_managed=True, actor="scim:s_tests")
    people.link(emily.id, subject="https://login.acme.com 0f6a")  # she signed in
    for user_name in ("emily.c@acme.com", "emily.carter@acme.com"):  # renamed by SCIM, then back
        people.link(emily.id, source="scim", user_name=user_name, scim_managed=True, actor="scim:s_tests")
    assert people.account(emily.id).renamed
    pids: queue.Queue[int] = queue.Queue()
    errors: list[BaseException] = []

    def sign_in() -> None:  # the sign-in's transaction, as Oidc._person makes it for a person found by their subject
        try:
            with people.changing(emily.id) as within:
                pids.put(within.conn.execute("SELECT pg_backend_pid()").fetchone()[0])
                people.confirmed(emily.id, "Emily.Carter@acme.com", within=within)
                people.apply_claims(emily.id, ["latet-users"], {}, within=within)
        except BaseException as exc:  # the thread's failure is the test's
            errors.append(exc)

    def waiting_for(pid: int) -> list[int]:
        with db.connection() as conn:
            return list(conn.execute("SELECT pg_blocking_pids(%s)", (pid,)).fetchone()[0])

    thread = threading.Thread(target=sign_in)
    with db.transaction() as scim:  # SCIM's write of Emily, as Scim._held begins it
        scim.execute("SELECT source FROM identities WHERE id = %s FOR UPDATE", (emily.id,))
        mine = scim.execute("SELECT pg_backend_pid()").fetchone()[0]
        thread.start()
        signing_in = pids.get(timeout=10)
        settle(lambda: mine in waiting_for(signing_in))  # the sign-in waits for this transaction
        scim.execute("SELECT user_name FROM accounts WHERE identity_id = %s FOR UPDATE", (emily.id,))
    thread.join(timeout=30)
    assert not thread.is_alive() and errors == []
    assert not people.account(emily.id).renamed  # the sign-in brought her user name: confirmed
