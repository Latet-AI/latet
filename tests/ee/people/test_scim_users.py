"""SCIM 2.0 users, as Microsoft Entra ID and Okta send them, on a server of two instances (ee/scim.py).

SCIM sees and changes only the people it manages (M3): those it provisioned, and those an administrator handed to it
(``People.link_person(…, scim=True)``, `latet server people link … --scim`); a person added by hand is unknown to it.
Once a trial ended, SCIM still disables and removes people, and does nothing else (the user's decision O4)."""

import pytest
from starlette.testclient import TestClient

from latet.ee.roles import Role
from latet.ee.scim import RENAME_REFUSED, Caller, ScimError
from latet.server.operations import server_app

from .server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
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

SCIM = "/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
ESCAPE = chr(27)  # what starts a terminal's escape sequence
# What Entra ID's provisioning sends to create a person, with its default attribute mappings.
ENTRA_USER = {
    "schemas": [USER, ENTERPRISE],
    "externalId": "emily.carter",
    "userName": "emily.carter@acme.com",
    "active": True,
    "displayName": "Emily Carter",
    "title": "Payments engineer",
    "emails": [{"primary": True, "type": "work", "value": "emily.carter@acme.com"}],
    "name": {"formatted": "Emily Carter", "familyName": "Carter", "givenName": "Emily"},
    ENTERPRISE: {"department": "Payments"},
}


@pytest.fixture
def scim(cluster):
    """What the provider sends with every request: a SCIM token, made as on the Sign-in page."""
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    return {"authorization": f"Bearer {token}"}


def _replace(path, value):
    return {"schemas": [PATCH], "Operations": [{"op": "Replace", "path": path, "value": value}]}


def _created(cluster, scim, body=ENTRA_USER):
    r = cluster.a.post(f"{SCIM}/Users", json=body, headers=scim)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_without_a_token_nothing_is_answered(cluster):
    assert cluster.a.get(f"{SCIM}/Users").status_code == 401  # no token yet: SCIM is off
    cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    assert cluster.a.get(f"{SCIM}/Users").status_code == 401
    assert cluster.a.get(f"{SCIM}/Users", headers={"authorization": "Bearer latet_scim_wrong"}).status_code == 401
    assert cluster.a.get(f"{SCIM}/Users", headers={"authorization": "Basic latet_scim_wrong"}).status_code == 401


def test_entra_creates_a_person_who_counts_and_finds_them_by_user_name_case_aside(cluster, scim):
    r = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim)
    assert r.status_code == 201 and r.headers["content-type"].startswith("application/scim+json")
    created = r.json()
    assert created["userName"] == "emily.carter@acme.com" and created["active"] is True
    assert created["meta"]["location"] == f"{URL}/scim/v2/Users/{created['id']}"
    assert r.headers["location"] == created["meta"]["location"]  # RFC 7644, 3.3
    person = cluster.server.identities.get(created["id"])
    assert (person.name, person.email, person.source) == ("Emily Carter", "emily.carter@acme.com", "scim")
    account = cluster.people.account(created["id"])
    assert account.scim_managed and account.linked_by == "scim" and account.external_id == "emily.carter"
    assert cluster.server.identities.enabled_count() == 1
    found = cluster.b.get(f'{SCIM}/Users?filter=userName eq "Emily.Carter@ACME.com"', headers=scim).json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == created["id"]
    assert "Payments" not in cluster.a.get(f"{SCIM}/Users/{created['id']}", headers=scim).text  # not kept
    again = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim)
    assert again.status_code == 409 and again.json()["scimType"] == "uniqueness"


def test_a_person_who_signed_in_before_scim_is_taken_only_once_an_administrator_hands_them_over(cluster, scim):
    emily, _code = cluster.server.identities.add_person("Emily Carter", None, source="oidc")
    subject = "https://login.acme.test/realms/acme 0f6a"
    cluster.people.link(emily.id, subject=subject, user_name="EMILY.CARTER@acme.com")
    refused = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim)
    assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness"
    assert "emily" not in refused.text.lower()  # the answer quotes nothing of what was sent
    assert cluster.server.identities.enabled_count() == 1  # counted once: no second person made
    assert cluster.server.identities.get(emily.id).email is None  # and nothing of her changed
    cluster.people.link_person(emily.id, subject=subject, user_name="EMILY.CARTER@acme.com", scim=True)
    r = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim)
    assert r.status_code == 201 and r.json()["id"] == emily.id
    assert cluster.server.identities.enabled_count() == 1
    assert cluster.server.identities.get(emily.id).email == "emily.carter@acme.com"
    assert cluster.people.account(emily.id).subject == subject  # her sign-in stays hers


def test_a_person_added_by_hand_is_taken_only_once_handed_over_and_keeps_their_name(cluster, scim):
    daniel = add_person(cluster, "Daniel Okafor", "Daniel.Okafor@ACME.com")
    body = {"schemas": [USER], "userName": "daniel.okafor@acme.com", "emails": [{"value": "daniel.okafor@acme.com"}]}
    refused = cluster.a.post(f"{SCIM}/Users", json=body, headers=scim)  # his e-mail never hands him over (H1, M3)
    assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness"
    assert cluster.people.account(daniel.id) is None and cluster.server.identities.enabled_count() == 1
    cluster.people.link_person(daniel.id, user_name="Daniel.Okafor@ACME.com", scim=True, actor=BY_HAND)
    r = cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "daniel.okafor@acme.com"}, headers=scim)
    assert r.status_code == 201 and r.json()["id"] == daniel.id
    person = cluster.server.identities.get(daniel.id)
    assert (person.name, person.email) == ("Daniel Okafor", "Daniel.Okafor@ACME.com")  # F12: nothing said, kept
    assert cluster.server.identities.enabled_count() == 1


def test_scim_sees_and_changes_no_one_added_by_hand(cluster, scim):
    admin = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)
    cluster.people.link_person(admin.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # for sign-in, not SCIM
    emily = _created(cluster, scim)
    assert cluster.a.get(f"{SCIM}/Users/{admin.id}", headers=scim).status_code == 404
    patched = cluster.a.patch(f"{SCIM}/Users/{admin.id}", json=_replace("active", False), headers=scim)
    assert patched.status_code == 404
    renamed = cluster.a.patch(f"{SCIM}/Users/{admin.id}", json=_replace("userName", "x@acme.com"), headers=scim)
    assert renamed.status_code == 404
    assert cluster.a.put(f"{SCIM}/Users/{admin.id}", json=ENTRA_USER, headers=scim).status_code == 404
    assert cluster.a.delete(f"{SCIM}/Users/{admin.id}", headers=scim).status_code == 404
    found = cluster.a.get(f'{SCIM}/Users?filter=userName eq "priya.shah@acme.com"', headers=scim).json()
    assert found["totalResults"] == 0
    listed = cluster.a.get(f"{SCIM}/Users", headers=scim).json()
    assert [resource["id"] for resource in listed["Resources"]] == [emily] and listed["totalResults"] == 1
    person, account = cluster.server.identities.get(admin.id), cluster.people.account(admin.id)
    assert not person.disabled and account.user_name == "priya.shah@acme.com" and not account.scim_managed


def test_scim_never_renames_the_user_name_of_a_person_handed_to_it_who_never_signed_in(cluster, scim):
    admin = add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)
    cluster.people.link_person(admin.id, user_name="priya.shah@acme.com", scim=True, actor=BY_HAND)
    renamed = cluster.a.patch(f"{SCIM}/Users/{admin.id}", json=_replace("userName", "mallory@acme.com"), headers=scim)
    assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability"
    assert cluster.people.account(admin.id).user_name == "priya.shah@acme.com"
    whole = {"schemas": [USER], "userName": "mallory@acme.com", "displayName": "Priya Shah"}
    assert cluster.a.put(f"{SCIM}/Users/{admin.id}", json=whole, headers=scim).status_code == 400
    assert cluster.people.account(admin.id).user_name == "priya.shah@acme.com"
    same = cluster.a.patch(f"{SCIM}/Users/{admin.id}", json=_replace("userName", "Priya.Shah@ACME.com"), headers=scim)
    assert same.status_code == 200  # its case alone: the same user name
    # A person SCIM made is its own to rename.
    emily = _created(cluster, scim)
    moved = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "emily.jones@acme.com"), headers=scim)
    assert moved.status_code == 200 and cluster.people.account(emily).user_name == "emily.jones@acme.com"


def test_a_look_alike_user_name_renames_and_takes_over_no_one_handed_to_scim(cluster, scim):
    """The final review's F3: SCIM finds an account by its userName as a sign-in does, A to Z's case aside and nothing
    else folded (a Kelvin sign is no k)."""
    kate = add_person(cluster, "Kate King", "kate.king@acme.com", Role.ADMIN)
    cluster.people.link_person(kate.id, user_name="kate.king@acme.com", scim=True, actor=BY_HAND)
    look_alike = "\N{KELVIN SIGN}ate.king@acme.com"
    renamed = cluster.a.patch(f"{SCIM}/Users/{kate.id}", json=_replace("userName", look_alike), headers=scim)
    assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability"
    made = cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": look_alike}, headers=scim)
    assert made.status_code == 201 and made.json()["id"] != kate.id  # another person, beside her
    account = cluster.people.account(kate.id)
    assert account.user_name == "kate.king@acme.com" and account.scim is None  # nothing of hers written
    found = cluster.a.get(f'{SCIM}/Users?filter=userName eq "KATE.KING@acme.com"', headers=scim).json()
    assert [resource["id"] for resource in found["Resources"]] == [kate.id]


def test_scim_never_renames_a_person_with_a_role_who_never_signed_in_even_one_it_made(cluster, scim):
    """A leaked SCIM token must not point the first sign-in of someone holding a role at an account of its own."""
    emily = _created(cluster, scim)
    cluster.people.grant(emily, Role.ADMIN, actor=BY_HAND)
    renamed = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "mallory@acme.com"), headers=scim)
    assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability"
    daniel = _created(cluster, scim, {"schemas": [USER], "userName": "daniel.okafor@acme.com"})
    group = cluster.people.add_group("Security team", actor=BY_HAND)
    cluster.people.add_member(group.id, daniel, actor=BY_HAND)
    cluster.people.grant(group.id, Role.SECURITY, actor=BY_HAND)
    through = cluster.a.patch(f"{SCIM}/Users/{daniel}", json=_replace("userName", "mallory@acme.com"), headers=scim)
    assert through.status_code == 400 and through.json()["scimType"] == "mutability"
    assert cluster.people.account(emily).user_name == "emily.carter@acme.com"
    assert cluster.people.account(daniel).user_name == "daniel.okafor@acme.com"
    # Once they signed in, their subject names them: their user name no longer leads a sign-in anywhere.
    cluster.people.link(emily, subject="https://login.acme.test/realms/acme 0f6a")
    moved = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "emily.jones@acme.com"), headers=scim)
    assert moved.status_code == 200


def test_scim_never_renames_a_person_who_never_signed_in_while_their_account_holds_a_key_or_a_computer(cluster, scim):
    """The final security review's M1: a rename points the first sign-in of whoever has the new user name at the whole
    account, and a role is not all it hands over. An API key's control (given to the person, or to a group made by hand
    that they are in), a computer connected now, and one connected and used once (its space stays: history and vault)
    each keep SCIM from renaming someone who never signed in."""
    identities, people = cluster.server.identities, cluster.people
    key, _secret = identities.add_key("Billing CI")
    group = people.add_group("Billing", actor=BY_HAND)
    people.add_controller(key.id, group.id, actor=BY_HAND)

    def connected_once(person):
        identities.enrol(identities.code(person), "sam-laptop")
        identities.seen(person).result()  # its requests were noted
        assert identities.revoke(person) == 1  # then it was disconnected

    holds = {
        "emily.carter": lambda person: people.add_controller(key.id, person, actor=BY_HAND),
        "daniel.okafor": lambda person: people.add_member(group.id, person, actor=BY_HAND),
        "priya.shah": lambda person: identities.enrol(identities.code(person), "priya-laptop"),
        "sam.rivera": connected_once,
    }
    for name, hold in holds.items():
        person = _created(cluster, scim, {"schemas": [USER], "userName": f"{name}@acme.com"})
        hold(person)
        renamed = cluster.a.patch(f"{SCIM}/Users/{person}", json=_replace("userName", "mallory@acme.com"), headers=scim)
        assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability", (name, renamed.text)
        whole = {"schemas": [USER], "userName": "mallory@acme.com"}
        assert cluster.a.put(f"{SCIM}/Users/{person}", json=whole, headers=scim).status_code == 400, name
        assert people.account(person).user_name == f"{name}@acme.com", name
        assert people.unclaimed("mallory@acme.com", scim_only=False) is None, name
    # Someone SCIM made whose account holds nothing is SCIM's to rename, as before.
    noah = _created(cluster, scim, {"schemas": [USER], "userName": "noah.patel@acme.com"})
    moved = cluster.a.patch(f"{SCIM}/Users/{noah}", json=_replace("userName", "noah.p@acme.com"), headers=scim)
    assert moved.status_code == 200 and people.account(noah).user_name == "noah.p@acme.com"


def _codes(cluster, person):
    with cluster.server.db.connection() as conn:
        return conn.execute("SELECT count(*) FROM codes WHERE identity_id = %s", (person,)).fetchone()[0]


def test_scim_never_renames_someone_who_never_signed_in_while_a_code_made_for_their_computer_waits(cluster, scim):
    """The final security review's L2: an administrator makes Emily a code for her computer, and before she uses it a
    SCIM token renames her to Mallory's user name. Mallory's first sign-in would claim the empty account, a rename back
    would leave it as it was, and Emily's computer, connected with the code, would send her agents' values into the
    account Mallory signs in to. A waiting code is something the account holds; the code every new person is made
    with is not left behind by SCIM, which shows it to nobody, and a code that ended holds nothing."""
    identities, people = cluster.server.identities, cluster.people
    emily = _created(cluster, scim)
    made_with = _codes(cluster, emily)  # the code made with her account
    code = identities.code(emily)  # an administrator's, sent to Emily (the one before stops)
    renamed = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "mallory@acme.com"), headers=scim)
    assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability", renamed.text
    assert people.unclaimed("mallory@acme.com", scim_only=False) is None
    assert people.unclaimed("emily.carter@acme.com", scim_only=False) == emily
    enrolled = identities.enrol(code, "emily-laptop")  # the next morning, into her own account
    assert enrolled is not None and enrolled[0].id == emily
    assert made_with == 0  # SCIM deleted the code made with her account: it was never shown to anyone
    # Deleted at the provider with an administrator's code waiting, the account is not taken back by the user name.
    daniel = _created(cluster, scim, {"schemas": [USER], "userName": "daniel.okafor@acme.com"})
    identities.code(daniel)
    assert cluster.a.delete(f"{SCIM}/Users/{daniel}", headers=scim).status_code == 204
    again = cluster.a.post(
        f"{SCIM}/Users", json={"schemas": [USER], "userName": "daniel.okafor@acme.com"}, headers=scim
    )
    assert again.status_code == 409 and again.json()["scimType"] == "uniqueness", again.text
    # A code that ended lets nothing in, and holds nothing: SCIM renames as before.
    priya = _created(cluster, scim, {"schemas": [USER], "userName": "priya.shah@acme.com"})
    identities.code(priya)
    cluster.clock.advance(25 * 3600)
    moved = cluster.a.patch(f"{SCIM}/Users/{priya}", json=_replace("userName", "priya.s@acme.com"), headers=scim)
    assert moved.status_code == 200 and people.account(priya).user_name == "priya.s@acme.com"


def _view(cluster, person):
    """The person as `latet server people …` reads them from the administration API."""
    people = cluster.a.get(f"{ADMIN_EE}/people", headers=as_admin(cluster)).json()["people"]
    return next(view for view in people if view["id"] == person)


def test_a_scim_rename_of_someone_who_never_signed_in_leaves_their_account_to_an_administrators_link(cluster, scim):
    """The final security review's wider option (L2): a SCIM token renames Emily, who never signed in and whose account
    holds nothing yet, to Mallory's user name; Mallory's first sign-in would claim the account, the token would rename
    it back, and whatever the company gives Emily later would go to Mallory. A SCIM change of the user name of an
    account nobody signed in to drops the claim instead: no first sign-in takes it, by either name, until an
    administrator links it again on purpose. SCIM's later writes keep it so, the rename back included."""
    people = cluster.people
    emily = _created(cluster, scim)
    renamed = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "mallory.west@acme.com"), headers=scim)
    assert renamed.status_code == 200, renamed.text
    account = people.account(emily)
    assert (account.user_name, account.subject, account.linked_by, account.renamed) == (
        "mallory.west@acme.com",
        None,
        None,
        True,
    )
    for user_name in ("mallory.west@acme.com", "emily.carter@acme.com"):
        assert people.unclaimed(user_name, scim_only=False) is None, user_name
        assert people.unclaimed(user_name, scim_only=True) is None, user_name
    whole = {"schemas": [USER], "userName": "emily.carter@acme.com", "displayName": "Emily Carter"}
    for back in (
        cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("userName", "emily.carter@acme.com"), headers=scim),
        cluster.a.put(f"{SCIM}/Users/{emily}", json=whole, headers=scim),
        cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("displayName", "Emily J. Carter"), headers=scim),
    ):
        assert back.status_code == 200, back.text
        account = people.account(emily)
        assert (account.user_name, account.linked_by, account.renamed) == ("emily.carter@acme.com", None, True)
        assert people.unclaimed("emily.carter@acme.com", scim_only=False) is None
    view = _view(cluster, emily)
    assert (view["linked"], view["claimable"], view["renamed"], view["scim_managed"]) == (False, False, True, True)
    # The administration log: SCIM's renames, by its token, name the field and never a value.
    [token] = cluster.server.edition.scim_tokens.all()
    linked = [entry for entry in cluster.server.audit.query(action="person.linked") if entry.target == emily]
    assert all(entry.actor == f"scim:{token.id}" for entry in linked)
    renames = [entry.details for entry in linked if "user_name" in entry.details["fields"]]
    assert renames.count({"source": "scim", "fields": ["user_name"]}) == 2, renames  # there, and back
    assert all("mallory" not in repr(entry.details) and "emily" not in repr(entry.details) for entry in linked)
    # An administrator links her again on purpose: her first sign-in finds her account by its user name.
    link = {"user_name": "emily.carter@acme.com", "scim": True}
    again = cluster.a.post(f"{ADMIN_EE}/people/{emily}/link", json=link, headers=as_admin(cluster))
    assert again.status_code == 200, again.text
    account = people.account(emily)
    assert (account.linked_by, account.renamed, account.scim_managed) == ("scim", False, True)
    assert people.unclaimed("emily.carter@acme.com", scim_only=False) == emily
    assert (_view(cluster, emily)["claimable"], _view(cluster, emily)["renamed"]) == (True, False)
    # The link changed no user name, and the log says it gave the claim back (the claim fix's review, L2).
    [given] = [
        entry
        for entry in cluster.server.audit.query(action="person.linked")
        if entry.target == emily and entry.actor.startswith("admin:")
    ]
    assert (given.details["source"], given.details["fields"]) == ("scim", ["linked_by"])
    kept = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("displayName", "Emily Carter"), headers=scim)
    assert kept.status_code == 200 and people.unclaimed("emily.carter@acme.com", scim_only=True) == emily


def test_a_change_of_capitals_alone_is_no_rename_and_the_claim_stays(cluster, scim):
    """A user name is compared with A to Z's case aside (``user_name_key``): the same provider accounts claim it."""
    people = cluster.people
    daniel = _created(cluster, scim, {"schemas": [USER], "userName": "daniel.okafor@acme.com"})
    same = cluster.a.patch(f"{SCIM}/Users/{daniel}", json=_replace("userName", "Daniel.Okafor@ACME.com"), headers=scim)
    assert same.status_code == 200, same.text
    account = people.account(daniel)
    assert (account.user_name, account.linked_by, account.renamed) == ("Daniel.Okafor@ACME.com", "scim", False)
    assert people.unclaimed("daniel.okafor@acme.com", scim_only=False) == daniel
    # An administrator's claim too: a change of capitals made outside their link keeps it.
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    people.link_person(priya.id, user_name="priya.shah@acme.com", actor=BY_HAND)
    people.link(priya.id, user_name="Priya.Shah@acme.com")
    assert (people.account(priya.id).linked_by, people.account(priya.id).renamed) == ("admin", False)
    assert people.unclaimed("priya.shah@acme.com", scim_only=False) == priya.id
    # Anything else of the rename guard is as it was: an account that holds something is not SCIM's to rename.
    sam = _created(cluster, scim, {"schemas": [USER], "userName": "sam.rivera@acme.com"})
    people.grant(sam, Role.SECURITY, actor=BY_HAND)
    refused = cluster.a.patch(f"{SCIM}/Users/{sam}", json=_replace("userName", "mallory@acme.com"), headers=scim)
    assert refused.status_code == 400 and refused.json()["scimType"] == "mutability"
    assert refused.json()["detail"] == RENAME_REFUSED
    account = people.account(sam)
    assert (account.user_name, account.linked_by, account.renamed) == ("sam.rivera@acme.com", "scim", False)


def test_a_name_with_a_control_character_is_refused_and_nothing_changes(cluster, scim):
    bad = {**ENTRA_USER, "displayName": f"Emily{ESCAPE}[2J Carter"}
    refused = cluster.a.post(f"{SCIM}/Users", json=bad, headers=scim)
    assert refused.status_code == 400 and refused.json()["scimType"] == "invalidValue"
    assert ESCAPE not in refused.text and cluster.server.identities.enabled_count() == 0
    emily = _created(cluster, scim)
    for path, value in (
        ("displayName", f"Emily{ESCAPE}[2J"),
        ("name.givenName", "Emily\N{RIGHT-TO-LEFT OVERRIDE}"),
        ('emails[type eq "work"].value', f"emily{ESCAPE}@acme.com"),
        ("userName", f"emily{ESCAPE}@acme.com"),
        ("externalId", "x" * 5000),
    ):
        r = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace(path, value), headers=scim)
        assert r.status_code == 400 and r.json()["scimType"] == "invalidValue", (path, r.text)
    person, account = cluster.server.identities.get(emily), cluster.people.account(emily)
    assert (person.name, person.email) == ("Emily Carter", "emily.carter@acme.com")
    assert (account.user_name, account.external_id) == ("emily.carter@acme.com", "emily.carter")


# White space around a user name, as Python's strip() would take it off: a no-break space, an ideographic space, a tab.
SPACED = ("\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}", "\t", " ")


def test_a_user_name_with_white_space_around_it_is_refused_never_stripped(cluster, scim):
    """The final security review's L1, SCIM's side: a userName is kept as the provider sends it, or refused (400),
    never stripped into another one (Emily's, with a no-break space after it, would otherwise name her)."""
    emily = _created(cluster, scim)
    daniel = _created(cluster, scim, {"schemas": [USER], "userName": "daniel.okafor@acme.com"})
    for space in SPACED:
        for spaced in (f"emily.carter@acme.com{space}", f"{space}priya.shah@acme.com"):
            made = cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": spaced}, headers=scim)
            assert made.status_code == 400 and made.json()["scimType"] == "invalidValue", (repr(spaced), made.text)
            assert "emily" not in made.text and "priya" not in made.text
        renamed = cluster.a.patch(
            f"{SCIM}/Users/{daniel}", json=_replace("userName", f"daniel.jones@acme.com{space}"), headers=scim
        )
        assert renamed.status_code == 400 and renamed.json()["scimType"] == "invalidValue", repr(space)
    assert cluster.people.account(daniel).user_name == "daniel.okafor@acme.com"
    assert cluster.server.identities.enabled_count() == 2
    assert cluster.a.get(f"{SCIM}/Users/{emily}", headers=scim).json()["userName"] == "emily.carter@acme.com"


def test_entra_disables_with_the_string_false_and_the_sessions_end_on_every_instance(cluster, scim):
    user_id = _created(cluster, scim)
    identities = cluster.server.identities
    _identity, token, _key = identities.enrol(identities.code(user_id), "laptop")
    sign_in_as(cluster, identities.get(user_id))
    assert cluster.a.get(f"{API}/me").status_code == 200 and cluster.b.get(f"{API}/me").status_code == 200
    r = cluster.b.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", "False"), headers=scim)
    assert r.status_code == 200 and r.json()["active"] is False
    # Every instance ends the sessions on E0b's `identities` message (Server._ended), within moments.
    settle(lambda: cluster.a.get(f"{API}/me").status_code == 401 and cluster.b.get(f"{API}/me").status_code == 401)
    settle(lambda: identities.by_secret(token) is None)
    assert identities.enabled_count() == 0
    assert cluster.people.account(user_id).disabled_by == "scim"
    cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", "True"), headers=scim)
    assert not identities.get(user_id).disabled


def test_scim_never_enables_a_person_an_administrator_disabled(cluster, scim):
    from latet.ee.people import disable_person

    user_id = _created(cluster, scim)
    disable_person(cluster.people, user_id, by="local", actor=BY_HAND)
    r = cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", True), headers=scim)
    assert r.status_code == 200 and r.json()["active"] is False
    whole = cluster.a.put(f"{SCIM}/Users/{user_id}", json={**ENTRA_USER, "active": True}, headers=scim)
    assert whole.status_code == 200 and whole.json()["active"] is False
    again = cluster.a.post(f"{SCIM}/Users", json={**ENTRA_USER, "active": True}, headers=scim)
    assert again.status_code == 409  # not deleted: still SCIM's, and still disabled
    assert cluster.server.identities.get(user_id).disabled
    assert cluster.people.account(user_id).disabled_by == "local"


def test_scim_never_disables_the_last_administrator(cluster, scim):
    user_id = _created(cluster, scim)
    cluster.people.grant(user_id, Role.ADMIN, actor=BY_HAND)
    refused = cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", False), headers=scim)
    assert refused.status_code == 409
    assert cluster.a.delete(f"{SCIM}/Users/{user_id}", headers=scim).status_code == 409
    assert not cluster.server.identities.get(user_id).disabled
    add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)
    assert cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", False), headers=scim).status_code == 200
    assert cluster.server.identities.get(user_id).disabled


def test_entra_changes_the_name_and_the_work_e_mail_by_path_and_what_latet_does_not_keep_is_ignored(cluster, scim):
    user_id = _created(cluster, scim)
    operations = [
        {"op": "Replace", "path": "displayName", "value": "Emily Carter-Jones"},
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "emily.carterjones@acme.com"},
        {"op": "Add", "path": 'phoneNumbers[type eq "work"].value', "value": "+1 555 0100"},
        {"op": "Replace", "path": f"{ENTERPRISE}:department", "value": "Platform"},
    ]
    r = cluster.a.patch(f"{SCIM}/Users/{user_id}", json={"schemas": [PATCH], "Operations": operations}, headers=scim)
    assert r.status_code == 200
    person = cluster.server.identities.get(user_id)
    assert (person.name, person.email) == ("Emily Carter-Jones", "emily.carterjones@acme.com")
    entries = cluster.server.audit.query(action="person.changed")
    assert [entry.details for entry in entries] == [{"fields": ["email", "name"]}]  # the fields, never the values


def test_a_patch_that_says_nothing_of_the_name_leaves_it(cluster, scim):
    body = {"schemas": [USER], "userName": "daniel.okafor@acme.com", "name": {"givenName": "Daniel"}}
    user_id = _created(cluster, scim, body)
    assert cluster.server.identities.get(user_id).name == "Daniel"
    cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("externalId", "daniel"), headers=scim)
    assert cluster.server.identities.get(user_id).name == "Daniel"


def test_okta_disables_with_a_value_object_and_puts_a_person_whole(cluster, scim):
    okta = {
        "schemas": [USER],
        "userName": "daniel.okafor@acme.com",
        "name": {"givenName": "Daniel", "familyName": "Okafor"},
        "emails": [{"value": "daniel.okafor@acme.com", "primary": True}],
        "active": True,
    }
    user_id = _created(cluster, scim, okta)
    assert cluster.server.identities.get(user_id).name == "Daniel Okafor"
    off = {"schemas": [PATCH], "Operations": [{"op": "replace", "value": {"active": False}}]}
    cluster.a.patch(f"{SCIM}/Users/{user_id}", json=off, headers=scim)
    assert cluster.server.identities.get(user_id).disabled
    whole = {**okta, "name": {"givenName": "Dan", "familyName": "Okafor"}}
    assert cluster.a.put(f"{SCIM}/Users/{user_id}", json=whole, headers=scim).status_code == 200
    person = cluster.server.identities.get(user_id)
    assert person.name == "Dan Okafor" and not person.disabled


def test_an_account_unlinked_after_a_sign_in_is_claimed_by_no_sign_in_whatever_scim_writes_of_it_since(cluster, scim):
    """A claim takes the whole account (roles, history, values): SCIM makes an account claimable by its user name when
    it provisions it, and its later writes keep the claim as it is, so that one an administrator unlinked (the
    provider's issuer changed) waits for a link made on purpose (Task 8, fix round 2)."""
    user_id = _created(cluster, scim)
    patched = cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("externalId", "emily.c"), headers=scim)
    assert patched.status_code == 200
    assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=False) == user_id  # never signed in: SCIM's
    cluster.people.link(user_id, subject="https://old.acme.test 0f6a-emily", actor=user_id)  # she signed in
    assert cluster.a.delete(f"{ADMIN_EE}/people/{user_id}/link", headers=as_admin(cluster)).status_code == 200
    whole = {"schemas": [USER], "userName": "emily.carter@acme.com", "displayName": "Emily Carter"}
    for written in (
        cluster.a.patch(f"{SCIM}/Users/{user_id}", json=_replace("displayName", "Emily J. Carter"), headers=scim),
        cluster.a.put(f"{SCIM}/Users/{user_id}", json=whole, headers=scim),
    ):
        assert written.status_code == 200, written.text
        account = cluster.people.account(user_id)
        assert (account.subject, account.linked_by, account.scim_managed) == (None, None, True)
        assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=False) is None
        assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=True) is None


def test_a_put_that_leaves_out_the_external_id_and_the_e_mail_keeps_them(cluster, scim):
    user_id = _created(cluster, scim)
    whole = {"schemas": [USER], "userName": "emily.carter@acme.com", "displayName": "Emily Carter"}
    assert cluster.a.put(f"{SCIM}/Users/{user_id}", json=whole, headers=scim).status_code == 200
    assert cluster.people.account(user_id).external_id == "emily.carter"
    assert cluster.server.identities.get(user_id).email == "emily.carter@acme.com"
    found = cluster.a.get(f'{SCIM}/Users?filter=externalId eq "emily.carter"', headers=scim).json()
    assert found["totalResults"] == 1


def test_a_filter_by_external_id_never_finds_someone_scim_does_not_manage(cluster, scim):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link(daniel.id, external_id="daniel.okafor", actor=BY_HAND)
    found = cluster.a.get(f'{SCIM}/Users?filter=externalId eq "daniel.okafor"', headers=scim).json()
    assert found["totalResults"] == 0 and found["Resources"] == []


def test_an_e_mail_another_person_has_is_refused_in_neutral_words(cluster, scim):
    add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    taken = {"schemas": [USER], "userName": "dan@acme.com", "emails": [{"value": "daniel.okafor@acme.com"}]}
    refused = cluster.a.post(f"{SCIM}/Users", json=taken, headers=scim)
    assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness"
    assert "hands them to SCIM" not in refused.json()["detail"] and "e-mail" in refused.json()["detail"]
    emily = _created(cluster, scim)
    moved = cluster.a.patch(f"{SCIM}/Users/{emily}", json=_replace("emails", "daniel.okafor@acme.com"), headers=scim)
    assert moved.status_code == 409 and "hands them to SCIM" not in moved.json()["detail"]


def test_deleting_a_person_disables_them_and_a_new_provisioning_takes_back_only_an_account_never_used(cluster, scim):
    user_id = _created(cluster, scim)
    assert cluster.a.delete(f"{SCIM}/Users/{user_id}", headers=scim).status_code == 204
    assert cluster.server.identities.get(user_id).disabled
    assert cluster.a.get(f"{SCIM}/Users/{user_id}", headers=scim).status_code == 404
    assert cluster.a.delete(f"{SCIM}/Users/{user_id}", headers=scim).status_code == 404
    filtered = cluster.a.get(f'{SCIM}/Users?filter=userName eq "emily.carter@acme.com"', headers=scim).json()
    assert filtered["totalResults"] == 0
    assert cluster.a.get(f"{SCIM}/Users", headers=scim).json()["totalResults"] == 0
    # The provider provisioning the same userName again restores the account SCIM made when nobody used it: nobody
    # signed in to it, no computer was connected to it, and it holds no role given by hand and no API key's control.
    again = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim)
    assert again.status_code == 201 and again.json()["id"] == user_id
    assert not cluster.server.identities.get(user_id).disabled and not cluster.people.account(user_id).deleted


def test_a_deleted_persons_account_that_was_used_is_never_taken_back_by_the_next_with_their_user_name(cluster, scim):
    """The final security review's M2: a provider gives a user name again (to a namesake, or to a rehire with a new
    account), and Latet matches by the user name alone. A deleted person's account that someone signed in to, or that
    holds a computer, a role given by hand or an API key's control, is never brought back by it: the request is
    refused (409) until an administrator removes the old person, or hands them back to SCIM on purpose."""
    identities, people = cluster.server.identities, cluster.people
    add_person(cluster, "Priya Shah", "priya.shah@acme.com", Role.ADMIN)  # the server's administrator
    key, _secret = identities.add_key("Billing CI")
    tokens = {}

    def computer(person):
        _person, tokens[person], _key = identities.enrol(identities.code(person), "laptop")

    uses = {
        "signed in": lambda person: people.link(person, subject=f"https://login.acme.test/realms/acme {person}"),
        "a computer": computer,
        "a role": lambda person: people.grant(person, Role.ADMIN, actor=BY_HAND),
        "a key's control": lambda person: people.add_controller(key.id, person, actor=BY_HAND),
    }
    old = {}
    for n, (use, make_used) in enumerate(uses.items()):
        body = {"schemas": [USER], "userName": f"emily{n}@acme.com", "externalId": f"emily-{n}"}
        old[use] = _created(cluster, scim, body)
        make_used(old[use])
        assert cluster.a.delete(f"{SCIM}/Users/{old[use]}", headers=scim).status_code == 204, use
        new_hire = {**body, "externalId": f"new-hire-{n}", "displayName": "Emily Carter"}
        refused = cluster.a.post(f"{SCIM}/Users", json=new_hire, headers=scim)
        assert refused.status_code == 409 and refused.json()["scimType"] == "uniqueness", (use, refused.text)
        assert "emily" not in refused.text.lower()  # fixed words, quoting nothing sent
        assert identities.get(old[use]).disabled and people.account(old[use]).deleted, use
    assert identities.by_secret(tokens[old["a computer"]]) is None  # the old computer stays refused
    assert identities.enabled_count() == 2  # Priya and the key: nobody made, nobody enabled
    # An administrator removes the old person: the provider's next request makes a new one.
    assert identities.remove(old["a computer"])
    made = cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "emily1@acme.com"}, headers=scim)
    assert made.status_code == 201 and made.json()["id"] != old["a computer"]
    # Or hands the old person back to SCIM on purpose (unlinked first, since they signed in): SCIM finds them again,
    # and the provider enables them.
    assert people.unlink(old["signed in"], actor=BY_HAND)
    people.link_person(old["signed in"], user_name="emily0@acme.com", scim=True, actor=BY_HAND)
    linked = cluster.server.audit.query(action="person.linked", actor=BY_HAND)
    [handed] = [entry for entry in linked if not entry.details.get("unlinked")]  # beside the unlink's own entry
    assert (handed.target, handed.details) == (old["signed in"], {"source": "scim", "fields": ["deleted", "linked_by"]})
    found = cluster.a.get(f'{SCIM}/Users?filter=userName eq "emily0@acme.com"', headers=scim).json()
    assert [resource["id"] for resource in found["Resources"]] == [old["signed in"]]
    back = cluster.a.patch(f"{SCIM}/Users/{old['signed in']}", json=_replace("active", True), headers=scim)
    assert back.status_code == 200 and back.json()["active"] is True
    assert not people.account(old["signed in"]).deleted


def test_a_filter_latet_does_not_take_is_said_so(cluster, scim):
    r = cluster.a.get(f'{SCIM}/Users?filter=emails co "acme"', headers=scim)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidFilter"
    assert "acme" not in r.text  # the refusal quotes nothing of the filter


def test_a_body_that_is_not_a_json_object_is_refused(cluster, scim):
    for body in (b"[1]", b"{", b'{"userName": NaN}'):
        r = cluster.a.post(f"{SCIM}/Users", content=body, headers={**scim, "content-type": "application/scim+json"})
        assert r.status_code == 400 and r.json()["scimType"] == "invalidSyntax"
    too_large = b'{"userName": "' + b"x" * 1_100_000 + b'"}'
    assert cluster.a.post(f"{SCIM}/Users", content=too_large, headers=scim).status_code == 413
    assert cluster.server.identities.enabled_count() == 0


def test_people_come_page_by_page(cluster, scim, clock):
    for name in ("emily.carter", "daniel.okafor", "priya.shah"):
        cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": f"{name}@acme.com"}, headers=scim)
        clock.advance(1)
    page = cluster.a.get(f"{SCIM}/Users?startIndex=2&count=1", headers=scim).json()
    assert (page["totalResults"], page["startIndex"], page["itemsPerPage"]) == (3, 2, 1)
    assert page["Resources"][0]["userName"] == "daniel.okafor@acme.com"


def test_a_request_from_a_web_page_is_refused(cluster, scim):
    assert cluster.a.get(f"{SCIM}/Users", headers={**scim, "origin": URL}).status_code == 403
    assert cluster.a.get(f"{SCIM}/Users", headers={**scim, "sec-fetch-site": "none"}).status_code == 403


def test_a_request_in_clear_to_an_https_server_is_refused(cluster, scim):
    in_clear = TestClient(server_app(cluster.server), base_url="http://latet.acme.lan")
    assert in_clear.get(f"{SCIM}/Users", headers=scim).status_code == 403
    assert in_clear.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim).status_code == 403
    assert cluster.server.identities.enabled_count() == 0


def test_scims_changes_are_in_the_administration_log_by_its_token(cluster, scim):
    created = cluster.a.post(f"{SCIM}/Users", json=ENTRA_USER, headers=scim).json()
    cluster.a.patch(f"{SCIM}/Users/{created['id']}", json=_replace("active", False), headers=scim)
    [token] = cluster.server.edition.scim_tokens.all()
    entries = sorted(cluster.server.audit.query(actor=f"scim:{token.id}"), key=lambda entry: entry.id)
    assert [(entry.action, entry.target) for entry in entries] == [
        ("person.added", created["id"]),
        ("person.linked", created["id"]),
        ("person.disabled", created["id"]),
    ]
    assert entries[1].details == {"source": "scim", "fields": ["external_id", "scim_managed", "user_name"]}
    assert all("emily" not in repr(entry.details).lower() for entry in entries)  # never a value


def test_a_token_that_ended_is_refused(cluster, clock):
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND, days=1)
    headers = {"authorization": f"Bearer {token}"}
    assert cluster.a.get(f"{SCIM}/Users", headers=headers).status_code == 200
    clock.advance(2 * 86400)
    assert cluster.b.get(f"{SCIM}/Users", headers=headers).status_code == 401  # found valid before: looked up anyway


def test_once_a_trial_ended_scim_only_disables_and_removes(cluster):
    on_trial(cluster)
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    scim = {"authorization": f"Bearer {token}"}
    emily = _created(cluster, scim)
    daniel = _created(cluster, scim, {"schemas": [USER], "userName": "daniel.okafor@acme.com"})
    end_trial(cluster)
    made = cluster.b.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "priya.shah@acme.com"}, headers=scim)
    assert made.status_code == 403 and made.json()["code"] == "trial-ended"
    unread = cluster.b.post(f"{SCIM}/Users", content=b"x" * 1_100_000, headers=scim)
    assert unread.status_code == 403 and unread.json()["code"] == "trial-ended"  # refused before its body is read
    renamed = cluster.b.patch(f"{SCIM}/Users/{emily}", json=_replace("displayName", "Emily Jones"), headers=scim)
    assert renamed.status_code == 403 and renamed.json()["code"] == "trial-ended"
    assert cluster.server.identities.get(emily).name == "Emily Carter"
    # The provider finds whom to disable, then disables and removes: what takes access away still works.
    found = cluster.b.get(f'{SCIM}/Users?filter=userName eq "emily.carter@acme.com"', headers=scim)
    assert found.status_code == 200 and found.json()["totalResults"] == 1
    # A PATCH that disables and renames at once: the disable alone is written (O4), the rest left as it was.
    mixed = {
        "schemas": [PATCH],
        "Operations": [
            {"op": "Replace", "path": "active", "value": False},
            {"op": "Replace", "path": "displayName", "value": "Emily Jones"},
        ],
    }
    off = cluster.b.patch(f"{SCIM}/Users/{emily}", json=mixed, headers=scim)
    assert off.status_code == 200 and off.json()["active"] is False
    assert cluster.server.identities.get(emily).name == "Emily Carter"
    back = cluster.b.patch(f"{SCIM}/Users/{emily}", json=_replace("active", True), headers=scim)
    assert back.status_code == 403 and back.json()["code"] == "trial-ended"
    assert cluster.server.identities.get(emily).disabled
    assert cluster.b.delete(f"{SCIM}/Users/{daniel}", headers=scim).status_code == 204
    assert cluster.server.identities.enabled_count() == 0


def test_once_a_trial_ended_a_put_without_a_user_name_still_disables(cluster):
    on_trial(cluster)
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    scim = {"authorization": f"Bearer {token}"}
    emily = _created(cluster, scim)
    end_trial(cluster)
    off = cluster.b.put(f"{SCIM}/Users/{emily}", json={"schemas": [USER], "active": False}, headers=scim)
    assert off.status_code == 200 and off.json()["active"] is False
    assert cluster.people.account(emily).user_name == "emily.carter@acme.com"


def test_the_license_is_weighed_again_in_scims_own_transaction(cluster):
    on_trial(cluster)
    scim = cluster.server.edition.scim
    caller = Caller("scim:s_tests", "203.0.113.7")
    emily = scim._create({"user_name": "emily.carter@acme.com", "name": {}, "display_name": "Emily Carter"}, caller)
    end_trial(cluster)
    with pytest.raises(ScimError) as refused:  # past the gate, as an instance that has not heard yet would be
        scim._create({"user_name": "daniel.okafor@acme.com", "name": {}}, caller)
    assert refused.value.status == 403 and refused.value.more == {"code": "trial-ended"}
    with pytest.raises(ScimError):
        scim._change(emily, _replace("displayName", "Emily Jones"), False, caller)
    assert cluster.server.identities.get(emily).name == "Emily Carter"
    scim._delete(emily, caller)  # a removal still passes
    assert cluster.server.identities.get(emily).disabled


def test_without_sso_in_the_license_scims_own_transaction_changes_nothing(cluster):
    from .server_support import license

    scim = cluster.server.edition.scim
    caller = Caller("scim:s_tests", "203.0.113.7")
    emily = scim._create({"user_name": "emily.carter@acme.com", "name": {}}, caller)
    license(cluster, features=["roles"])
    for change in (
        lambda: scim._create({"user_name": "daniel.okafor@acme.com", "name": {}}, caller),
        lambda: scim._change(emily, _replace("active", False), False, caller),
        lambda: scim._delete(emily, caller),
    ):
        with pytest.raises(ScimError) as refused:
            change()
        assert refused.value.status == 403 and "sso" in refused.value.detail
    assert not cluster.server.identities.get(emily).disabled and not cluster.people.account(emily).deleted


def test_while_a_paid_license_is_paused_scim_still_creates_and_disables(cluster):
    paused(cluster)
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    scim = {"authorization": f"Bearer {token}"}
    user_id = _created(cluster, scim)
    disabled = cluster.b.patch(f"{SCIM}/Users/{user_id}", json=_replace("active", False), headers=scim)
    assert disabled.status_code == 200 and disabled.json()["active"] is False


def test_without_sso_in_the_license_scim_answers_nothing(cluster):
    from .server_support import license

    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    license(cluster, features=["roles"])
    refused = cluster.a.get(f"{SCIM}/Users", headers={"authorization": f"Bearer {token}"})
    assert refused.status_code == 403 and "sso" in refused.json()["detail"]


# --- E3's release gate 3: GET /Users pages in PostgreSQL ------------------------------------------------------------


def _made_by_scim(cluster, scim, *names, client=None):
    """People SCIM provisions, one a second, each with an externalId, a name and a work e-mail: their ids, in that order."""
    ids = []
    for name in names:
        given, family = name.split(".")
        body = {
            "schemas": [USER],
            "userName": f"{name}@acme.com",
            "externalId": f"ext-{name}",
            "name": {"givenName": given.title(), "familyName": family.title()},
            "emails": [{"primary": True, "type": "work", "value": f"{name}@acme.com"}],
        }
        ids.append(_created(cluster, scim, body))
        cluster.clock.advance(1)  # each made a second after the one before
    return ids


FIVE = ("emily.carter", "daniel.okafor", "priya.shah", "sam.rivera", "lena.fischer")


def _all_pages(client, scim, size):
    """Every page of GET /Users of ``size``, as a provider's import reads them: the totals said, and the people."""
    totals, people, start = [], [], 1
    while True:
        page = client.get(f"{SCIM}/Users?startIndex={start}&count={size}", headers=scim).json()
        totals.append(page["totalResults"])
        people.extend(page["Resources"])
        if not page["Resources"]:
            return totals, people
        start += page["itemsPerPage"]


def test_a_page_reads_only_its_own_people(cluster, scim, monkeypatch):
    """Gate 3: a page no longer reads every account and every person (each page of a provider's import read everyone,
    N squared over 200 rows for a whole listing): it pages their ids in SQL. A filter, one person at most, is
    answered as before."""
    emily, daniel, _priya = _made_by_scim(cluster, scim, *FIVE[:3])

    def everyone(*args, **kwargs):
        raise AssertionError("a page read everyone")

    monkeypatch.setattr(cluster.people, "accounts", everyone)
    monkeypatch.setattr(cluster.server.identities, "all", everyone)
    page = cluster.a.get(f"{SCIM}/Users?startIndex=2&count=1", headers=scim)
    assert page.status_code == 200, page.text
    assert [resource["id"] for resource in page.json()["Resources"]] == [daniel]
    assert page.json()["totalResults"] == 3
    found = cluster.a.get(f'{SCIM}/Users?filter=userName eq "emily.carter@acme.com"', headers=scim).json()
    assert found["totalResults"] == 1 and [resource["id"] for resource in found["Resources"]] == [emily]


def test_pages_follow_when_people_were_made_and_ties_by_id(cluster, scim):
    """By when each person was made, the same instant broken by their ids, on every page and every instance. The
    people's times are set in the reverse of the order they were stored in, so that the order PostgreSQL happens to
    keep its rows in never passes for it (N1); of the two made at the same instant, the one with the lower id was stored
    after the other and is named to sort after it, so that neither the rows' order nor their names pass for it."""
    ids = _made_by_scim(cluster, scim, *FIVE)
    pairs = ((one, other) for n, one in enumerate(ids) for other in ids[n + 1 :] if one > other)
    high, low = next(pairs, (max(ids[2:4]), min(ids[2:4])))  # ids rising in the order made: 1 run in 120
    others = [identity_id for identity_id in ids if identity_id not in (high, low)]  # in the order they were stored
    made = cluster.clock()
    times = {others[0]: made + 40, others[1]: made + 30, high: made + 20, low: made + 20, others[2]: made}
    with cluster.server.db.connection() as conn:
        for identity_id, at in times.items():
            conn.execute("UPDATE identities SET created = %s WHERE id = %s", (at, identity_id))
        for identity_id, name in ((high, "Aaron Abbott"), (low, "Zoe Zimmer")):
            conn.execute("UPDATE identities SET name = %s WHERE id = %s", (name, identity_id))
    expected = [others[2], low, high, others[1], others[0]]
    pages = []
    for start, client in ((1, cluster.a), (3, cluster.a), (5, cluster.b)):  # the third page from instance B
        page = client.get(f"{SCIM}/Users?startIndex={start}&count=2", headers=scim).json()
        assert (page["totalResults"], page["startIndex"]) == (5, start)
        pages.append([resource["id"] for resource in page["Resources"]])
    assert pages == [expected[0:2], expected[2:4], expected[4:]]


def test_only_the_people_scim_manages_are_listed_and_counted(cluster, scim):
    """The same people as one person's GET: SCIM-managed and not deleted at the provider, a person SCIM disabled
    listed ``active: false``; never someone added by hand, nor someone linked for sign-in and not handed to SCIM."""
    add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    linked = add_person(cluster, "Sam Rivera", "sam.rivera@acme.com")
    cluster.people.link_person(linked.id, user_name="sam.rivera@acme.com", actor=BY_HAND)  # for sign-in, not SCIM
    emily, daniel, lena = _made_by_scim(cluster, scim, "emily.carter", "daniel.okafor", "lena.fischer")
    assert cluster.a.delete(f"{SCIM}/Users/{lena}", headers=scim).status_code == 204  # deleted at the provider
    disabled = cluster.a.patch(f"{SCIM}/Users/{daniel}", json=_replace("active", False), headers=scim)
    assert disabled.status_code == 200
    listed = cluster.b.get(f"{SCIM}/Users", headers=scim).json()
    assert listed["totalResults"] == 2
    assert [(resource["id"], resource["active"]) for resource in listed["Resources"]] == [
        (emily, True),
        (daniel, False),
    ]


@pytest.mark.parametrize(
    ("query", "start", "listed"),
    [
        ("startIndex=0", 1, 5),
        ("startIndex=-5", 1, 5),
        ("startIndex=x", 1, 5),
        (f"startIndex={10**30}", 10**30, 0),
        ("startIndex=6", 6, 0),
        ("startIndex=5", 5, 1),
        ("count=0", 1, 0),
        ("count=-1", 1, 0),
        (f"count={10**9}", 1, 5),
        ("count=x", 1, 5),
        ("startIndex=2&count=3", 2, 3),
    ],
)
def test_a_start_past_the_end_or_a_count_out_of_range_answers_an_empty_page(cluster, scim, query, start, listed):
    """What a provider asks is read as before (``startIndex`` at least 1, ``count`` from 0 to 200, anything else the
    default), and never reaches PostgreSQL past the people: a start past them costs one count."""
    _made_by_scim(cluster, scim, *FIVE)
    page = cluster.a.get(f"{SCIM}/Users?{query}", headers=scim)
    assert page.status_code == 200, page.text
    body = page.json()
    assert (body["totalResults"], body["startIndex"], body["itemsPerPage"]) == (5, start, listed)
    assert len(body["Resources"]) == listed


def test_a_listed_person_is_the_same_as_their_own_get(cluster, scim):
    """L4: every person on every page is listed as their own GET answers them, field by field: what a page builds from
    its narrower read (``People.scim_page``) is what the rest of SCIM shows."""
    ids = _made_by_scim(cluster, scim, *FIVE[:4])
    plain = cluster.a.post(f"{SCIM}/Users", json={"schemas": [USER], "userName": "lena.fischer@acme.com"}, headers=scim)
    ids.append(plain.json()["id"])  # no externalId, no name, no e-mail
    assert cluster.a.patch(f"{SCIM}/Users/{ids[1]}", json=_replace("active", False), headers=scim).status_code == 200
    cluster.clock.advance(60)
    renamed = cluster.a.patch(f"{SCIM}/Users/{ids[2]}", json=_replace("name.givenName", "Priyanka"), headers=scim)
    assert renamed.status_code == 200
    totals, listed = _all_pages(cluster.b, scim, 2)
    assert set(totals) == {5} and sorted(resource["id"] for resource in listed) == sorted(ids)
    for resource in listed:
        assert resource == cluster.a.get(f"{SCIM}/Users/{resource['id']}", headers=scim).json(), resource["id"]
    fields = {key for resource in listed for key in resource}
    assert {"active", "externalId", "name", "emails", "meta", "userName", "displayName"} <= fields
