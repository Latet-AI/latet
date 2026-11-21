"""SCIM 2.0 groups, as Entra ID and Okta push them: Latet's groups, to which roles are given (ee/scim.py).

SCIM's groups take only the people SCIM manages (M3), never leave the server without an administrator, and once a trial
ended SCIM changes no group at all (the coordinator's ruling on O4, which names people only)."""

import threading

import pytest

from latet.ee.roles import Role
from latet.ee.scim import Caller, ScimError

from .fake_provider import SSO
from .server_support import BY_HAND, SERVER_TESTS, add_person, end_trial, license, on_trial, settle

pytestmark = SERVER_TESTS

SCIM = "/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ESCAPE = chr(27)  # what starts a terminal's escape sequence
RIGHT_TO_LEFT = chr(0x202E)  # a bidirectional override: a name shown otherwise than it is
SHARP_S = "\N{LATIN SMALL LETTER SHARP S}"  # lower() keeps it, casefold() makes it "ss"


def _token(cluster):
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    return {"authorization": f"Bearer {token}"}


@pytest.fixture
def scim(cluster):
    return _token(cluster)


def _person(cluster, scim, name):
    body = {"schemas": [USER], "userName": f"{name}@acme.com"}
    r = cluster.a.post(f"{SCIM}/Users", json=body, headers=scim)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _group(cluster, scim, name, *members):
    body = {"schemas": [GROUP], "displayName": name, "members": [{"value": member} for member in members]}
    r = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _patch(*operations):
    return {"schemas": [PATCH], "Operations": list(operations)}


def _members(cluster, group_id):
    return sorted(ref.id for ref in cluster.people.members(group_id))


def test_a_pushed_group_is_a_latet_group_whose_role_its_members_have_and_entra_takes_a_member_out_by_path(
    cluster, scim
):
    emily = _person(cluster, scim, "emily.carter")
    body = {
        "schemas": [GROUP],
        "displayName": "Payments leads",
        "externalId": "5d0e7a1c",
        "members": [{"value": emily}],
    }
    r = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim)
    assert r.status_code == 201 and r.json()["members"][0]["value"] == emily
    assert r.headers["location"] == r.json()["meta"]["location"]
    group = cluster.people.group(r.json()["id"])
    assert (group.name, group.source, group.external_id) == ("Payments leads", "scim", "5d0e7a1c")
    cluster.people.grant(group.id, Role.GROUP_LEAD, actor=BY_HAND)
    assert Role.GROUP_LEAD in cluster.people.roles_of(emily)
    out = _patch({"op": "Remove", "path": f'members[value eq "{emily}"]'})
    assert cluster.b.patch(f"{SCIM}/Groups/{group.id}", json=out, headers=scim).status_code == 204
    assert cluster.people.roles_of(emily) == {Role.USER}


def test_entra_finds_a_group_by_its_name_before_pushing_it(cluster, scim):
    _group(cluster, scim, "Latet-Admins")
    url = f'{SCIM}/Groups?filter=displayName eq "latet-admins"&excludedAttributes=members'
    found = cluster.a.get(url, headers=scim).json()
    assert found["totalResults"] == 1 and "members" not in found["Resources"][0]
    by_id = cluster.a.post(
        f"{SCIM}/Groups", json={"schemas": [GROUP], "displayName": "Sales", "externalId": "7f"}, headers=scim
    )
    found = cluster.a.get(f'{SCIM}/Groups?filter=externalId eq "7f"', headers=scim).json()
    assert [group["id"] for group in found["Resources"]] == [by_id.json()["id"]]
    refused = cluster.a.get(f'{SCIM}/Groups?filter=members eq "{by_id.json()["id"]}"', headers=scim)
    assert refused.status_code == 400 and refused.json()["scimType"] == "invalidFilter"


def test_okta_adds_replaces_and_renames_with_operations_of_its_own(cluster, scim):
    emily, daniel = _person(cluster, scim, "emily.carter"), _person(cluster, scim, "daniel.okafor")
    group = _group(cluster, scim, "sales")
    cluster.people.grant(group, Role.AUDITOR, actor=BY_HAND)
    both = _patch({"op": "add", "path": "members", "value": [{"value": emily}, {"value": daniel}]})
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=both, headers=scim).status_code == 204
    assert Role.AUDITOR in cluster.people.roles_of(emily) and Role.AUDITOR in cluster.people.roles_of(daniel)
    renamed = _patch({"op": "replace", "value": {"id": group, "displayName": "Sales and marketing"}})
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=renamed, headers=scim).status_code == 204
    assert cluster.people.group(group).name == "Sales and marketing"
    only = _patch({"op": "replace", "path": "members", "value": [{"value": daniel}]})
    cluster.a.patch(f"{SCIM}/Groups/{group}", json=only, headers=scim)
    assert cluster.people.roles_of(emily) == {Role.USER} and Role.AUDITOR in cluster.people.roles_of(daniel)
    read = cluster.b.get(f"{SCIM}/Groups/{group}", headers=scim).json()
    assert read["displayName"] == "Sales and marketing" and [m["value"] for m in read["members"]] == [daniel]


def test_a_name_another_source_holds_is_refused_and_scim_sees_only_its_own_groups(cluster, scim):
    payments = cluster.people.add_group("Payments", actor=BY_HAND)
    r = cluster.a.post(f"{SCIM}/Groups", json={"schemas": [GROUP], "displayName": "payments"}, headers=scim)
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"
    assert "payments" not in r.json()["detail"].lower()  # a refusal quotes nothing of what was sent
    assert cluster.a.get(f"{SCIM}/Groups", headers=scim).json()["totalResults"] == 0
    assert cluster.a.get(f"{SCIM}/Groups/{payments.id}", headers=scim).status_code == 404
    assert cluster.a.get(f"{SCIM}/Groups/Payments", headers=scim).status_code == 404  # an id, never a name
    renamed = _patch({"op": "replace", "path": "displayName", "value": "Renamed"})
    assert cluster.a.patch(f"{SCIM}/Groups/{payments.id}", json=renamed, headers=scim).status_code == 404
    assert cluster.a.delete(f"{SCIM}/Groups/{payments.id}", headers=scim).status_code == 404
    assert cluster.people.group(payments.id).name == "Payments"


def test_a_member_by_hand_stays_when_scim_puts_the_group_whole(cluster, scim):
    emily, daniel = _person(cluster, scim, "emily.carter"), _person(cluster, scim, "daniel.okafor")
    group = _group(cluster, scim, "Platform", emily)
    cluster.people.add_member(group, daniel, actor=BY_HAND)
    whole = {"schemas": [GROUP], "displayName": "Platform", "members": []}
    assert cluster.a.put(f"{SCIM}/Groups/{group}", json=whole, headers=scim).status_code == 200
    assert [ref.id for ref in cluster.people.members(group)] == [daniel]


def test_deleting_a_group_takes_away_what_it_gave(cluster, scim):
    add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)  # the server keeps an administrator
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Latet admins", emily)
    cluster.people.grant(group, Role.ADMIN, actor=BY_HAND)
    assert cluster.a.delete(f"{SCIM}/Groups/{group}", headers=scim).status_code == 204
    assert cluster.a.get(f"{SCIM}/Groups/{group}", headers=scim).status_code == 404
    assert cluster.people.roles_of(emily) == {Role.USER} and cluster.people.group(group) is None


def test_the_discovery_documents_say_what_latet_supports(cluster, scim):
    config = cluster.a.get(f"{SCIM}/ServiceProviderConfig", headers=scim).json()
    assert config["patch"]["supported"] and not config["bulk"]["supported"] and config["filter"]["maxResults"] == 200
    kinds = cluster.a.get(f"{SCIM}/ResourceTypes", headers=scim).json()["Resources"]
    assert [kind["name"] for kind in kinds] == ["User", "Group"]
    schemas = cluster.a.get(f"{SCIM}/Schemas", headers=scim).json()["Resources"]
    assert [schema["id"] for schema in schemas] == [USER, GROUP]
    assert cluster.a.get(f"{SCIM}/ServiceProviderConfig").status_code == 401  # with SCIM's token only


# --- the rulings ------------------------------------------------------------------------------------------------------


def test_scims_groups_take_only_the_people_scim_manages(cluster, scim):
    """M3: a person added by hand (here the administrator) never joins a SCIM group, which would hand them its roles,
    nor does an id that is no one's; the refusal names no value, and nothing changes."""
    admin = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)
    cluster.people.link_person(admin.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # for sign-in, not SCIM's
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Latet admins", emily)
    cluster.people.grant(group, Role.ADMIN, actor=BY_HAND)
    for member in (admin.id, "u_nobody00000", cluster.people.add_group("Local", actor=BY_HAND).id):
        added = _patch({"op": "Add", "path": "members", "value": [{"value": member}]})
        refused = cluster.a.patch(f"{SCIM}/Groups/{group}", json=added, headers=scim)
        assert refused.status_code == 400 and refused.json()["scimType"] == "invalidValue"
        assert member not in refused.json()["detail"]
    assert _members(cluster, group) == [emily]
    whole = {"schemas": [GROUP], "displayName": "Latet admins", "members": [{"value": emily}, {"value": admin.id}]}
    assert cluster.a.put(f"{SCIM}/Groups/{group}", json=whole, headers=scim).status_code == 400
    made = {"schemas": [GROUP], "displayName": "Security", "members": [{"value": admin.id}]}
    assert cluster.a.post(f"{SCIM}/Groups", json=made, headers=scim).status_code == 400
    assert cluster.people.group("Security") is None and _members(cluster, group) == [emily]


def test_a_person_scim_removed_never_joins_again_and_one_it_does_not_manage_is_neither_shown_nor_taken_out(
    cluster, scim
):
    emily, daniel = _person(cluster, scim, "emily.carter"), _person(cluster, scim, "daniel.okafor")
    group = _group(cluster, scim, "Platform", emily)
    assert cluster.a.delete(f"{SCIM}/Users/{daniel}", headers=scim).status_code == 204
    added = _patch({"op": "add", "path": "members", "value": [{"value": daniel}]})
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=added, headers=scim).status_code == 400
    # A membership of SCIM's held by someone SCIM does not manage (a person handed back): never shown to SCIM, and a
    # whole PUT leaves it.
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # an account, not SCIM's
    cluster.people.add_member(group, priya.id, source="scim", actor=BY_HAND)
    shown = cluster.a.get(f"{SCIM}/Groups/{group}", headers=scim).json()
    assert [member["value"] for member in shown["members"]] == [emily]
    whole = {"schemas": [GROUP], "displayName": "Platform", "members": []}
    assert cluster.a.put(f"{SCIM}/Groups/{group}", json=whole, headers=scim).status_code == 200
    assert _members(cluster, group) == [priya.id]


@pytest.mark.parametrize("name", [f"Payments{ESCAPE}[31m", f"{RIGHT_TO_LEFT}Payments", "g_abcdefghij", "x" * 81, " "])
def test_a_group_name_with_a_control_character_or_shaped_like_an_id_is_refused(cluster, scim, name):
    """L4: a name People would keep otherwise than it was sent, or that would stand for another group's id."""
    made = cluster.a.post(f"{SCIM}/Groups", json={"schemas": [GROUP], "displayName": name}, headers=scim)
    assert made.status_code == 400 and made.json()["scimType"] == "invalidValue"
    assert "displayName" in made.json()["detail"]  # said for what it is, quoting nothing of it
    assert cluster.people.groups() == []
    group = _group(cluster, scim, "Payments")
    renamed = _patch({"op": "Replace", "path": "displayName", "value": name})
    refused = cluster.a.patch(f"{SCIM}/Groups/{group}", json=renamed, headers=scim)
    assert refused.status_code == 400 and cluster.people.group(group).name == "Payments"
    put = cluster.a.put(f"{SCIM}/Groups/{group}", json={"schemas": [GROUP], "displayName": name}, headers=scim)
    assert put.status_code == 400 and cluster.people.group(group).name == "Payments"


def test_an_external_id_with_a_control_character_is_refused(cluster, scim):
    body = {"schemas": [GROUP], "displayName": "Payments", "externalId": f"5d0e{ESCAPE}"}
    made = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim)
    assert made.status_code == 400 and cluster.people.groups() == []


def test_a_group_change_never_leaves_the_server_without_an_administrator(cluster, scim):
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Latet admins", emily)
    cluster.people.grant(group, Role.ADMIN, actor=BY_HAND)
    out = _patch({"op": "Remove", "path": f'members[value eq "{emily}"]'})
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=out, headers=scim).status_code == 409
    whole = {"schemas": [GROUP], "displayName": "Latet admins", "members": []}
    assert cluster.a.put(f"{SCIM}/Groups/{group}", json=whole, headers=scim).status_code == 409
    assert cluster.a.delete(f"{SCIM}/Groups/{group}", headers=scim).status_code == 409
    assert _members(cluster, group) == [emily] and cluster.people.admins() == {emily}
    add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=out, headers=scim).status_code == 204
    assert _members(cluster, group) == []


def test_a_put_that_leaves_out_the_external_id_and_the_members_keeps_them(cluster, scim):
    """As SCIM's users (Task 6): an attribute a PUT does not name is left as it is."""
    emily = _person(cluster, scim, "emily.carter")
    body = {"schemas": [GROUP], "displayName": "Payments", "externalId": "5d0e7a1c", "members": [{"value": emily}]}
    group = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim).json()["id"]
    put = cluster.a.put(
        f"{SCIM}/Groups/{group}", json={"schemas": [GROUP], "displayName": "Payments team"}, headers=scim
    )
    assert put.status_code == 200 and put.json()["externalId"] == "5d0e7a1c"
    assert _members(cluster, group) == [emily] and cluster.people.group(group).name == "Payments team"
    nameless = cluster.a.put(f"{SCIM}/Groups/{group}", json={"schemas": [GROUP], "members": []}, headers=scim)
    assert nameless.status_code == 400 and _members(cluster, group) == [emily]


def test_a_group_change_is_recorded_by_the_token_that_made_it_naming_no_value(cluster, scim):
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Payments", emily)
    found = sorted(cluster.server.audit.query(), key=lambda entry: entry.id)
    entries = [entry for entry in found if entry.target == group]
    assert [entry.action for entry in entries] == ["group.added", "member.added"]
    assert all(entry.actor.startswith("scim:s_") for entry in entries)
    assert "Payments" not in repr([entry.details for entry in entries])


def _mapped(cluster):
    """The sign-in settings of the fake provider: their mapping names the Latet groups "Latet admins" and "Payments
    leads", which no sign-in has made yet."""
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)


def test_a_group_is_never_renamed_to_a_group_the_sign_in_mapping_names(cluster, scim):
    """Fix round 1: a SCIM group of a name the mapping names would be the group sign-ins join, its role then everyone's
    whom the ID token puts in the mapped group; refused as a name taken (compared as the names' unique index compares
    them), quoting nothing, nothing changed."""
    _mapped(cluster)
    group = _group(cluster, scim, "Auditors")
    cluster.people.grant(group, Role.AUDITOR, actor=BY_HAND)
    for name in ("Latet admins", "PAYMENTS LEADS"):
        by_path = _patch({"op": "Replace", "path": "displayName", "value": name})
        by_value = _patch({"op": "replace", "value": {"id": group, "displayName": name}})  # as Okta sends it
        for refused in (
            cluster.a.patch(f"{SCIM}/Groups/{group}", json=by_path, headers=scim),
            cluster.a.patch(f"{SCIM}/Groups/{group}", json=by_value, headers=scim),
            cluster.b.put(f"{SCIM}/Groups/{group}", json={"schemas": [GROUP], "displayName": name}, headers=scim),
        ):
            assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness"
            assert name.lower() not in refused.json()["detail"].lower()
    assert cluster.people.group(group).name == "Auditors"
    assert cluster.people.group("Latet admins") is None and cluster.people.group("Payments leads") is None
    renamed = _patch({"op": "Replace", "path": "displayName", "value": "Security auditors"})  # any other name
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=renamed, headers=scim).status_code == 204
    assert cluster.people.group(group).name == "Security auditors"


def test_a_group_is_never_pushed_under_a_name_the_sign_in_mapping_names(cluster, scim):
    _mapped(cluster)
    emily = _person(cluster, scim, "emily.carter")
    for name in ("Latet admins", "payments LEADS"):
        body = {"schemas": [GROUP], "displayName": name, "members": [{"value": emily}]}
        refused = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim)
        assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness"
        assert name.lower() not in refused.json()["detail"].lower()
    assert cluster.people.groups() == [] and cluster.people.groups_of(emily) == []


def test_once_a_trial_ended_scim_changes_no_group(cluster):
    """The coordinator's ruling on O4 (which names people only): every group change is refused, a member taken out
    included, at the gate before its body is read; the provider still reads."""
    on_trial(cluster)
    scim = _token(cluster)
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Payments", emily)
    end_trial(cluster)
    out = _patch({"op": "Remove", "path": f'members[value eq "{emily}"]'})
    whole = {"schemas": [GROUP], "displayName": "Payments", "members": []}
    for refused in (
        cluster.b.post(f"{SCIM}/Groups", json={"schemas": [GROUP], "displayName": "Sales"}, headers=scim),
        cluster.b.patch(f"{SCIM}/Groups/{group}", json=out, headers=scim),
        cluster.b.put(f"{SCIM}/Groups/{group}", json=whole, headers=scim),
        cluster.b.delete(f"{SCIM}/Groups/{group}", headers=scim),
        cluster.b.patch(f"{SCIM}/Groups/{group}", content=b"x" * 1_100_000, headers=scim),  # before its body is read
    ):
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    assert _members(cluster, group) == [emily] and cluster.people.group("Sales") is None
    found = cluster.b.get(f'{SCIM}/Groups?filter=displayName eq "Payments"', headers=scim)
    assert found.status_code == 200 and found.json()["totalResults"] == 1
    assert cluster.b.get(f"{SCIM}/ServiceProviderConfig", headers=scim).status_code == 200


def test_the_license_is_weighed_again_in_each_group_changes_own_transaction(cluster):
    """F18: past the gate, as an instance that has not heard yet would be."""
    on_trial(cluster)
    scim, caller = cluster.server.edition.scim, Caller("scim:s_tests", "203.0.113.7")
    emily = scim._create({"user_name": "emily.carter@acme.com", "name": {}}, caller)
    group = scim._create_group({"schemas": [GROUP], "displayName": "Payments", "members": [{"value": emily}]}, caller)
    end_trial(cluster)
    out = _patch({"op": "Remove", "path": f'members[value eq "{emily}"]'})
    for change in (
        lambda: scim._create_group({"schemas": [GROUP], "displayName": "Sales"}, caller),
        lambda: scim._change_group(group, out, False, caller),
        lambda: scim._delete_group(group, caller),
    ):
        with pytest.raises(ScimError) as refused:
            change()
        assert refused.value.status == 403 and refused.value.more == {"code": "trial-ended"}
    assert _members(cluster, group) == [emily]
    license(cluster, features=["roles"])  # a license without sso
    for change in (
        lambda: scim._create_group({"schemas": [GROUP], "displayName": "Sales"}, caller),
        lambda: scim._change_group(group, out, False, caller),
        lambda: scim._delete_group(group, caller),
    ):
        with pytest.raises(ScimError) as refused:
            change()
        assert refused.value.status == 403 and "sso" in refused.value.detail
    assert _members(cluster, group) == [emily] and cluster.people.group("Sales") is None


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [[{"value": 42}], [{"display": "Emily Carter"}], [], {"value": ""}, "everyone"])
def test_a_members_value_that_names_no_one_is_refused_and_takes_no_one_out(cluster, scim, value):
    """A removal with no value takes every member out (RFC 7644, 3.5.2.2); a value given that names no id is refused in
    fixed words, never read as no value, and a replace or an add with it changes nothing either."""
    emily, daniel = _person(cluster, scim, "emily.carter"), _person(cluster, scim, "daniel.okafor")
    group = _group(cluster, scim, "Payments", emily, daniel)
    for op in ("remove", "replace", "add"):
        if value == [] and op != "remove":  # an empty list replaces the members with no one, and adds no one
            continue
        operation = _patch({"op": op, "path": "members", "value": value})
        refused = cluster.a.patch(f"{SCIM}/Groups/{group}", json=operation, headers=scim)
        assert refused.status_code == 400 and refused.json()["scimType"] == "invalidValue", (op, refused.text)
        assert "Emily" not in refused.json()["detail"] and "everyone" not in refused.json()["detail"]
    assert _members(cluster, group) == sorted([emily, daniel])
    every = _patch({"op": "remove", "path": "members"})  # no value: every member
    assert cluster.a.patch(f"{SCIM}/Groups/{group}", json=every, headers=scim).status_code == 204
    assert _members(cluster, group) == []


def test_a_group_is_never_renamed_onto_the_name_of_a_group_made_by_hand(cluster, scim):
    cluster.people.add_group("Payments", actor=BY_HAND)
    group = _group(cluster, scim, "Sales")
    renamed = _patch({"op": "Replace", "path": "displayName", "value": "PAYMENTS"})
    for refused in (
        cluster.a.patch(f"{SCIM}/Groups/{group}", json=renamed, headers=scim),
        cluster.b.put(f"{SCIM}/Groups/{group}", json={"schemas": [GROUP], "displayName": "payments"}, headers=scim),
    ):
        assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness", refused.text
        assert "payments" not in refused.json()["detail"].lower()
    assert cluster.people.group(group).name == "Sales" and len(cluster.people.groups()) == 2


def test_an_external_id_with_a_control_character_is_refused_on_a_change(cluster, scim):
    body = {"schemas": [GROUP], "displayName": "Payments", "externalId": "5d0e7a1c"}
    group = cluster.a.post(f"{SCIM}/Groups", json=body, headers=scim).json()["id"]
    by_patch = _patch({"op": "replace", "path": "externalId", "value": f"5d0e{ESCAPE}[31m"})
    for refused in (
        cluster.a.patch(f"{SCIM}/Groups/{group}", json=by_patch, headers=scim),
        cluster.b.put(f"{SCIM}/Groups/{group}", json={**body, "externalId": f"5d0e{ESCAPE}[31m"}, headers=scim),
    ):
        assert refused.status_code == 400 and refused.json()["scimType"] == "invalidValue", refused.text
        assert ESCAPE not in refused.text
    assert cluster.people.group(group).external_id == "5d0e7a1c"


def test_head_reads_a_person_or_a_group_as_get_without_a_body_and_changes_nothing(cluster, scim):
    """Starlette answers HEAD on a route that has GET: it reads, never takes a change's path."""
    emily = _person(cluster, scim, "emily.carter")
    group = _group(cluster, scim, "Payments", emily)
    entries = len(cluster.server.audit.query())
    for path in (f"{SCIM}/Users/{emily}", f"{SCIM}/Groups/{group}"):
        read = cluster.a.get(path, headers=scim)
        head = cluster.b.head(path, headers=scim)
        assert (head.status_code, head.content) == (200, b""), head.text
        assert head.headers["content-type"] == read.headers["content-type"] == "application/scim+json"
    assert cluster.a.head(f"{SCIM}/Users/u_nobody00000", headers=scim).status_code == 404
    assert cluster.a.head(f"{SCIM}/Groups/g_nobodyhere", headers=scim).status_code == 404
    assert len(cluster.server.audit.query()) == entries and _members(cluster, group) == [emily]


def test_a_filter_by_name_compares_case_aside_as_the_names_index_does(cluster, scim):
    """PostgreSQL's lower() keeps "Strasse" written with a sharp s and "STRASSE" two names, which Python's casefold()
    makes one: a filter that found the one by the other would have the provider take a group it never pushed, and push
    none of its own."""
    strasse = _group(cluster, scim, f"Stra{SHARP_S}e")
    by_name = cluster.a.get(f'{SCIM}/Groups?filter=displayName eq "STRASSE"', headers=scim).json()
    assert by_name["totalResults"] == 0 and by_name["Resources"] == []
    found = cluster.a.get(f'{SCIM}/Groups?filter=displayName eq "STRA{SHARP_S}E"', headers=scim).json()
    assert [resource["id"] for resource in found["Resources"]] == [strasse]  # case aside, as the index compares
    other = _group(cluster, scim, "STRASSE")  # another name to the index
    found = cluster.b.get(f'{SCIM}/Groups?filter=displayName eq "strasse"', headers=scim).json()
    assert [resource["id"] for resource in found["Resources"]] == [other]


# The lock every change keeping the last administrator takes (People.hold_admins), and how pg_locks shows a wait for
# it: an advisory lock's 64-bit key is its classid (the high half) and its objid.
LAST_ADMIN = "SELECT pg_advisory_xact_lock(hashtextextended('latet-last-admin:' || %s, 0))"


def _waiting_for_the_last_administrator(cluster) -> bool:
    db = cluster.server.db
    with db.connection() as conn:
        (key,) = conn.execute("SELECT hashtextextended('latet-last-admin:' || %s, 0)", (db.schema,)).fetchone()
        rows = conn.execute(
            "SELECT classid::bigint, objid::bigint FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchall()
    return any((high << 32 | low) == key % 2**64 for high, low in rows)


def _while_the_page_holds_the_last_administrator(cluster, change, *rows):
    """``change()`` (a SCIM request) made while another transaction holds the lock of the last administrator, as a
    disable from the page holds it before it takes the person's rows (``disable_person``). Once SCIM's change waits for
    the lock, that transaction takes ``rows`` (each a query FOR UPDATE NOWAIT and its arguments) at once: it could not
    while SCIM's change held them, and the two would wait on each other until PostgreSQL ended one. SCIM's answer once
    the lock is let go."""
    answer: list = []
    worker = threading.Thread(target=lambda: answer.append(change()))
    try:
        with cluster.server.db.transaction() as conn:
            conn.execute(LAST_ADMIN, (cluster.server.db.schema,))
            worker.start()
            settle(lambda: _waiting_for_the_last_administrator(cluster))
            for query, arguments in rows:
                conn.execute(query, arguments)  # LockNotAvailable if SCIM's change holds the row
    finally:
        if worker.ident is not None:  # started: its change goes on once the lock is let go
            worker.join(30)
    return answer[0]


def test_a_scim_change_takes_the_lock_of_the_last_administrator_before_any_row_of_the_person(cluster, scim):
    """Item 4 of the end-of-plan wave: SCIM's disable and the page's would otherwise deadlock (PostgreSQL: a 503)."""
    emily = _person(cluster, scim, "emily.carter")
    off = _patch({"op": "replace", "path": "active", "value": False})
    answer = _while_the_page_holds_the_last_administrator(
        cluster,
        lambda: cluster.b.patch(f"{SCIM}/Users/{emily}", json=off, headers=scim),
        ("SELECT 1 FROM identities WHERE id = %s FOR UPDATE NOWAIT", (emily,)),
        ("SELECT 1 FROM accounts WHERE identity_id = %s FOR UPDATE NOWAIT", (emily,)),
    )
    assert answer.status_code == 200 and answer.json()["active"] is False, answer.text


def test_a_scim_group_change_takes_the_lock_of_the_last_administrator_before_any_row_it_holds(cluster, scim):
    emily, daniel = _person(cluster, scim, "emily.carter"), _person(cluster, scim, "daniel.okafor")
    group = _group(cluster, scim, "Payments", emily)
    added = _patch({"op": "add", "path": "members", "value": [{"value": daniel}]})
    answer = _while_the_page_holds_the_last_administrator(
        cluster,
        lambda: cluster.b.patch(f"{SCIM}/Groups/{group}", json=added, headers=scim),
        ("SELECT 1 FROM identities WHERE id = %s FOR UPDATE NOWAIT", (daniel,)),  # as a disable of his takes it
        ("SELECT 1 FROM groups WHERE id = %s FOR UPDATE NOWAIT", (group,)),
    )
    assert answer.status_code == 204, answer.text
    assert _members(cluster, group) == sorted([emily, daniel])
