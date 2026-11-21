"""`latet server groups|roles|controllers|sign-in` and `latet server people link|unlink`, through the administration
API of a server (ee/server_cli.py)."""

import pytest
import yaml
from typer.testing import CliRunner

from latet.cli.agent import MARKERS
from latet.ee import server_cli
from latet.ee.roles import Role
from latet.server.client import ServerError
from latet.server.connection import NotConnected

from .fake_provider import CLIENT_SECRET, ISSUER, SSO
from .server_support import BY_HAND, SERVER_TESTS, URL, add_person, as_admin, end_trial, on_trial

pytestmark = SERVER_TESTS

ESC = chr(0x1B)
BIDI = chr(0x202E)  # right-to-left override


def _run(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, ["server", *args], input=input)


@pytest.fixture
def you(monkeypatch):
    """The commands run by you, not by an agent."""
    for markers in MARKERS.values():
        for name in markers:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT", raising=False)


@pytest.fixture
def cli(cluster, monkeypatch, you):
    """`latet server …` in-process, its calls to the administration API answered by the server under test."""

    def admin_call(method, path, body=None):
        r = cluster.a.request(method, f"/_latet/server/v1/admin/{path}", json=body, headers=as_admin(cluster))
        if r.status_code >= 400:
            found = r.json()
            details = tuple(item["msg"] for item in found.get("details", []) if isinstance(item, dict))
            raise ServerError(r.status_code, found.get("error", r.text), details)
        return r.json() if r.content else None

    monkeypatch.setattr(server_cli, "admin_call", admin_call)
    return _run


def test_the_first_administrator_is_made_with_latet_server(cli, cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    result = cli("roles", "grant", "emily.carter@acme.com", "admin")
    assert result.exit_code == 0, result.output
    assert f"Emily Carter (person, {emily.id}) has: admin." in result.output  # whom the name was taken for
    assert Role.ADMIN in cluster.people.roles_of(emily.id)
    assert f"admin: Emily Carter (person, {emily.id})" in cli("roles", "list").output


def test_a_name_written_with_a_look_alike_names_nobody_on_the_command_line_either(cli, cluster):
    """The final review's F3: the command resolves a name as the server does, A to Z's case aside and nothing else
    folded."""
    kate = add_person(cluster, "Kate King", "kate.king@acme.com")
    cluster.people.link_person(kate.id, user_name="kate.king@acme.com", actor=BY_HAND)
    refused = cli("roles", "grant", "\N{KELVIN SIGN}ate.king@acme.com", "admin")
    assert refused.exit_code != 0 and cluster.people.given(kate.id) == frozenset()
    granted = cli("roles", "grant", "KATE.KING@acme.com", "auditor")
    assert granted.exit_code == 0 and kate.id in granted.output


def test_a_group_is_made_given_a_role_and_people(cli, cluster):
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    assert cli("groups", "add", "Payments leads").exit_code == 0
    joined = cli("groups", "join", "Payments leads", "priya.shah@acme.com")
    assert joined.exit_code == 0 and f"Priya Shah ({priya.id}) is in Payments leads." in joined.output
    assert "group-lead" in cli("roles", "grant", "Payments leads", "group-lead").output
    assert "Payments leads (by hand): group-lead; Priya Shah" in cli("groups", "list").output
    assert Role.GROUP_LEAD in cluster.people.roles_of(priya.id)
    assert cli("groups", "leave", "Payments leads", priya.id).exit_code == 0
    assert cluster.people.roles_of(priya.id) == {Role.USER}
    assert cli("groups", "rename", "payments LEADS", "Billing leads").exit_code == 0
    assert cli("groups", "remove", "Billing leads").exit_code == 0 and cluster.people.groups() == []


def test_the_control_of_a_key_is_given_listed_and_taken_back(cli, cluster):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    added = cli("controllers", "add", "billing ci", "daniel.okafor@acme.com")
    assert added.exit_code == 0 and f"Daniel Okafor (person, {daniel.id}) controls Billing CI" in added.output
    assert f"{key.id}  Billing CI: Daniel Okafor (person)" in cli("controllers", "list").output
    assert cli("controllers", "remove", key.id, "daniel.okafor@acme.com").exit_code == 0
    assert "administrators only" in cli("controllers", "list").output


def test_someone_the_server_does_not_know_is_said_so(cli):
    result = cli("roles", "grant", "nobody@acme.com", "admin")
    assert result.exit_code == 2 and "no person or group on this server is nobody@acme.com" in result.output


def test_a_name_is_taken_as_the_server_takes_one_in_a_body(cli, cluster):
    """H1: an e-mail an ID token brought names nobody; a user name that reads as another's e-mail neither."""
    sam, _code = cluster.server.identities.add_person("Sam Rivera", "sam.rivera@acme.com", source="oidc")
    refused = cli("roles", "grant", "sam.rivera@acme.com", "auditor")
    assert refused.exit_code == 2 and "by their id" in refused.output
    assert cli("roles", "grant", sam.id, "auditor").exit_code == 0
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="emily.carter@acme.com", actor=BY_HAND)
    ambiguous = cli("roles", "grant", "Emily.Carter@acme.com", "security")
    assert ambiguous.exit_code == 2 and "by their id" in ambiguous.output
    assert cluster.people.given(emily.id) == frozenset() and cluster.people.given(priya.id) == frozenset()
    cluster.people.add_group("Emily Carter", actor=BY_HAND)
    cluster.people.link(sam.id, user_name="Emily Carter", actor=BY_HAND)
    both = cli("roles", "grant", "emily carter", "security")
    assert both.exit_code == 2 and "a person and a group" in both.output


def test_sign_in_is_set_from_a_file_and_the_secret_is_read_from_standard_input_and_never_printed(cli, tmp_path):
    file = tmp_path / "sso.yaml"
    file.write_text(yaml.safe_dump({"sso": SSO}), "utf-8")
    set_up = cli("sign-in", "set", str(file))
    assert set_up.exit_code == 0 and f"{URL}/_latet/sso/callback" in set_up.output
    assert "latet server sign-in secret" in set_up.output  # not saved yet
    saved = cli("sign-in", "secret", input=CLIENT_SECRET + "\n")
    assert saved.exit_code == 0 and CLIENT_SECRET not in saved.output
    shown = cli("sign-in", "show").output
    assert "Client secret: saved" in shown and "Latet's group Latet admins" in shown and "SCIM:" in shown
    assert "the company sign-in" in shown and "the members of latet-users" in shown
    assert cli("sign-in", "secret", "--remove").exit_code == 0
    assert "Client secret: not saved" in cli("sign-in", "show").output


def test_a_scim_token_is_printed_once_ends_and_is_removed_by_its_id(cli, cluster):
    made = cli("sign-in", "scim-token")
    assert made.exit_code == 0 and "latet_scim_" in made.output and f"{URL}/scim/v2" in made.output
    assert "ends on" in made.output  # the server's default, as an administration token's
    [token] = cluster.server.edition.scim_tokens.all()
    shown = cli("sign-in", "show").output
    assert f"on, 1 token ({token.id})" in shown and "made with `latet server`" in shown
    assert "latet_scim_" not in shown
    assert cli("sign-in", "scim-token", "--remove", token.id).exit_code == 0
    assert "off (no token)" in cli("sign-in", "show").output
    never = cli("sign-in", "scim-token", "--never")
    assert never.exit_code == 0 and "never ends" in never.output
    pasted = cli("sign-in", "scim-token", "--remove", "latet_scim_" + "x" * 43)
    assert (
        pasted.exit_code == 2
        and "shell's history" in pasted.output
        and len(cluster.server.edition.scim_tokens.all()) == 1
    )


def test_once_a_trial_ended_nothing_changes_and_the_server_says_why(cli, cluster):
    on_trial(cluster)
    add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    end_trial(cluster)
    result = cli("roles", "grant", "emily.carter@acme.com", "admin")
    assert result.exit_code == 2 and "trial ended" in result.output
    assert cli("roles", "list").exit_code == 0  # read as it stood at the end
    assert cli("sign-in", "show").exit_code == 0
    assert cli("sign-in", "scim-token").exit_code == 2


def test_whether_company_wide_changes_take_the_company_sign_in_is_set_here(cli, cluster, tmp_path):
    """Decision (c): the setting is `latet server`'s; a server without single sign-on takes any administrator's
    session, which `sign-in show` says."""
    assert "any administrator's session (no single sign-on)" in cli("sign-in", "show").output
    assert cli("sign-in", "set", "--company-changes", "any-session").exit_code == 2  # nothing set up to change
    file = tmp_path / "sso.yaml"
    file.write_text(yaml.safe_dump(SSO), "utf-8")  # the section's keys at the top
    assert cli("sign-in", "set", str(file)).exit_code == 0
    changed = cli("sign-in", "set", "--company-changes", "any-session")
    assert changed.exit_code == 0 and "any administrator's session" in changed.output
    assert cluster.server.edition.settings().sso.company_changes == "any-session"
    assert cluster.server.edition.settings().sso.client_id == SSO["client_id"]  # the rest kept
    [entry, *_] = cluster.server.audit.query(action="sign-in.changed")
    assert entry.actor.startswith("admin:") and entry.details["company_changes"] == "any-session"
    assert cli("sign-in", "set", str(file)).exit_code == 0  # a file that names none keeps it
    assert cluster.server.edition.settings().sso.company_changes == "any-session"
    assert cli("sign-in", "set", "--company-changes", "always").exit_code == 2
    assert cli("sign-in", "off").exit_code == 0 and cluster.server.edition.settings().sso is None


def test_who_gets_an_account_and_googles_domains_are_set_with_options(cli, cluster, tmp_path):
    """M2: --accounts and --allowed-domain (repeated)."""
    file = tmp_path / "google.yaml"
    google = {"issuer": "https://accounts.google.com", "client_id": "latet.apps.googleusercontent.com"}
    file.write_text(yaml.safe_dump({"sso": google}), "utf-8")
    refused = cli("sign-in", "set", str(file), "--accounts", "open")
    assert refused.exit_code == 2 and "allowed_domains" in refused.output  # what is wrong, and where
    args = ("--accounts", "open", "--allowed-domain", "acme.com", "--allowed-domain", "acme.de")
    assert cli("sign-in", "set", str(file), *args).exit_code == 0
    sso = cluster.server.edition.settings().sso
    assert (sso.accounts, sso.allowed_domains) == ("open", ["acme.com", "acme.de"])
    assert "Allowed domains: acme.com, acme.de" in cli("sign-in", "show").output
    assert cli("sign-in", "set", "--accounts", "everyone").exit_code == 2


def test_a_person_is_linked_to_the_provider_and_unlinked_and_whom_it_named_is_said(cli, cluster):
    """H1: an administrator links a person added by hand by their user name at the provider, --scim handing them
    to SCIM (M3)."""
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    linked = cli("people", "link", "priya.shah@acme.com", "--user-name", "priya.shah")
    assert linked.exit_code == 0 and f"Priya Shah ({priya.id}) is linked" in linked.output
    assert cluster.people.unclaimed("priya.shah", scim_only=False) == priya.id
    cluster.people.link(priya.id, subject=f"{ISSUER} 0f6a-priya", actor=BY_HAND)  # she signed in
    unlinked = cli("people", "unlink", "priya.shah")  # by her user name at the provider
    assert unlinked.exit_code == 0 and f"Priya Shah ({priya.id}) is unlinked" in unlinked.output
    assert cluster.people.account(priya.id).subject is None
    assert cli("people", "unlink", priya.id).exit_code == 2  # nothing left to unlink: the server says so
    handed = cli("people", "link", priya.id, "--user-name", "priya.shah", "--scim")
    assert handed.exit_code == 0 and cluster.people.account(priya.id).scim_managed
    assert cli("people", "link", priya.id).exit_code == 2  # no user name


def test_after_an_issuer_changed_everyone_is_unlinked_and_scims_accounts_linked_again_at_once(cli, cluster):
    """The bulk of Task 8's ruling: a new issuer leaves every subject to no sign-in; `unlink --all`, then `link
    --all --scim`, re-open each account to its own user name, on purpose."""
    people = []
    for name, user_name in (("Sam Rivera", "sam.rivera"), ("Mia Chen", "mia.chen")):
        person, _code = cluster.server.identities.add_person(name, None, source="scim")
        cluster.people.link(person.id, source="scim", user_name=user_name, scim_managed=True, actor="scim:s_t")
        cluster.people.link(person.id, subject=f"{ISSUER} {user_name}", actor=person.id)  # signed in
        people.append((person, user_name))
    add_person(cluster, "Tom Becker", "tom.becker@acme.com")  # no account at the provider: left alone
    assert "Nobody to link" in cli("people", "link", "--all", "--scim", "--yes").output  # each names a sign-in
    assert cli("people", "unlink", "--all").exit_code == 2  # a script says --yes
    assert cluster.people.account(people[0][0].id).subject is not None  # nothing changed
    assert cli("people", "unlink", "--all", "--yes").exit_code == 0
    assert all(cluster.people.account(person.id).subject is None for person, _ in people)
    assert all(cluster.people.unclaimed(name, scim_only=True) is None for _, name in people)
    assert cli("people", "link", "--all", "u_aaaaaaaaaa", "--yes").exit_code == 2  # --all or a person
    relinked = cli("people", "link", "--all", "--scim", "--yes")
    assert relinked.exit_code == 0, relinked.output
    assert all(cluster.people.unclaimed(name, scim_only=True) == person.id for person, name in people)
    entries = len(cluster.server.audit.query(action="person.linked"))
    assert cli("people", "link", "--all", "--scim", "--yes").exit_code == 0  # again: nothing more to change
    assert len(cluster.server.audit.query(action="person.linked")) == entries


def test_the_bulk_link_keeps_whom_scim_managed_and_never_hands_it_someone_holding_a_role(cli, cluster):
    """Fix round 1: `link --all` keeps each account's SCIM as it was; `--scim` hands SCIM the others, marked so in the
    list it asks about, but not someone who holds a role given to them or to a group made by hand (a SCIM token could
    then change who they are): they are listed, to hand over one by one."""
    sam, _code = cluster.server.identities.add_person("Sam Rivera", None, source="scim")
    cluster.people.link(sam.id, source="scim", user_name="sam.rivera", scim_managed=True, actor="scim:s_t")
    mia, _code = cluster.server.identities.add_person("Mia Chen", None, source="oidc")  # an earlier sign-in's
    cluster.people.link(mia.id, user_name="mia.chen", actor=mia.id)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    cluster.people.link_person(emily.id, user_name="emily.carter", actor=BY_HAND)
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    leads = cluster.people.add_group("Payments leads", actor=BY_HAND)
    cluster.people.grant(leads.id, Role.GROUP_LEAD, actor=BY_HAND)
    cluster.people.add_member(leads.id, priya.id, actor=BY_HAND)
    cluster.people.link_person(priya.id, user_name="priya.shah", actor=BY_HAND)
    everyone = (sam, mia, emily, priya)
    for person in everyone:  # they signed in; then the provider's issuer changed
        cluster.people.link(person.id, subject=f"{ISSUER} {person.id}", actor=person.id)
    assert cli("people", "unlink", "--all", "--yes").exit_code == 0
    kept = cli("people", "link", "--all", "--yes")
    assert kept.exit_code == 0, kept.output
    assert [cluster.people.account(p.id).scim_managed for p in everyone] == [True, False, False, False]
    assert cluster.people.unclaimed("mia.chen", scim_only=False) == mia.id  # linked again by its user name
    handed = cli("people", "link", "--all", "--scim", "--yes")
    assert handed.exit_code == 0, handed.output
    assert f"Mia Chen ({mia.id}): mia.chen (newly handed to SCIM)" in handed.output
    for person in (emily, priya):
        assert (
            f"({person.id})" in handed.output and f"people link {person.id} --user-name <name> --scim" in handed.output
        )
    assert [cluster.people.account(p.id).scim_managed for p in everyone] == [True, True, False, False]


def test_a_second_bulk_link_leaves_out_whom_the_first_linked_and_says_how_many(cli, cluster):
    """End-of-plan wave: whom a run linked already, waiting for their first sign-in, is neither listed nor asked about
    again (a script needs no --yes for nothing), only counted; whoever is unlinked since is linked again."""
    people = []
    for name, user_name in (("Sam Rivera", "sam.rivera"), ("Mia Chen", "mia.chen")):
        person, _code = cluster.server.identities.add_person(name, None, source="scim")
        cluster.people.link(person.id, source="scim", user_name=user_name, scim_managed=True, actor="scim:s_t")
        cluster.people.link(person.id, subject=f"{ISSUER} {user_name}", actor=person.id)  # signed in
        people.append(person)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)  # a role: handed over by hand
    cluster.people.link_person(emily.id, user_name="emily.carter", actor=BY_HAND)
    cluster.people.link(emily.id, subject=f"{ISSUER} emily", actor=emily.id)
    assert cli("people", "unlink", "--all", "--yes").exit_code == 0
    first = cli("people", "link", "--all", "--scim", "--yes")
    assert first.exit_code == 0 and "Left out" not in first.output, first.output
    entries = len(cluster.server.audit.query(action="person.linked"))
    again = cli("people", "link", "--all", "--scim")  # no --yes: nothing to ask
    assert again.exit_code == 0, again.output
    assert "Left out: 2 people are linked already and waiting for their first sign-in." in again.output
    assert "Nobody else to link." in again.output and "Sam Rivera" not in again.output
    # Emily's link is made: said with how she is handed to SCIM, one by one, and nothing asked of her.
    assert f"Emily Carter ({emily.id}): emily.carter (linked already; holds a role" in again.output
    assert len(cluster.server.audit.query(action="person.linked")) == entries  # nothing sent
    sam, mia = people
    cluster.people.link(mia.id, subject=f"{ISSUER} mia.chen", actor=mia.id)  # Mia signs in, then is unlinked
    cluster.people.unlink(mia.id, actor=BY_HAND)
    third = cli("people", "link", "--all", "--scim", "--yes")
    assert third.exit_code == 0 and "Left out: 1 person is linked already" in third.output, third.output
    assert f"Mia Chen ({mia.id}) is linked again." in third.output and sam.id not in third.output
    assert cluster.people.unclaimed("mia.chen", scim_only=True) == mia.id


def test_the_bulk_link_never_links_an_account_whose_user_name_scim_changed_before_anyone_signed_in(cli, cluster):
    """The final security review's wider option: a SCIM token renamed Emily, who never signed in, to Mallory's user
    name, and her account waits for an administrator's link. `link --all` would link it again by Mallory's user name,
    undoing that in bulk: it lists such an account apart, with the command that links it one by one, and links the
    others as before (Sam, unlinked after the issuer changed). An unlinked account that SCIM renamed since (Mia: her
    sign-in left nothing that SCIM's rename guard counts) is listed apart the same way."""
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    scim = {"authorization": f"Bearer {token}"}

    def provisioned(user_name):
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": user_name}
        made = cluster.a.post("/scim/v2/Users", json=body, headers=scim)
        assert made.status_code == 201, made.text
        return made.json()["id"]

    def renamed(person, user_name):
        operation = {"op": "Replace", "path": "userName", "value": user_name}
        patch = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [operation]}
        answer = cluster.a.patch(f"/scim/v2/Users/{person}", json=patch, headers=scim)
        assert answer.status_code == 200, answer.text

    emily, sam, mia = (provisioned(f"{name}@acme.com") for name in ("emily.carter", "sam.rivera", "mia.chen"))
    renamed(emily, "mallory.west@acme.com")
    for person in (sam, mia):
        cluster.people.link(person, subject=f"{ISSUER} {person}", actor=person)  # they signed in
    assert cli("people", "unlink", "--all", "--yes").exit_code == 0  # the provider's issuer changed
    renamed(mia, "mallory.chen@acme.com")
    one_by_one = f"latet server people link {emily} --user-name <name> --scim"
    for args in (("--yes",), ("--scim", "--yes")):
        bulk = cli("people", "link", "--all", *args)
        assert bulk.exit_code == 0, bulk.output
        assert f"({emily}): mallory.west@acme.com (" in bulk.output and one_by_one in bulk.output
        assert f"({mia}): mallory.chen@acme.com (" in bulk.output
        assert f"latet server people link {mia} --user-name <name> --scim" in bulk.output
        assert f"({emily}) is linked again" not in bulk.output and f"({mia}) is linked again" not in bulk.output
        for person, user_name in ((emily, "mallory.west@acme.com"), (mia, "mallory.chen@acme.com")):
            account = cluster.people.account(person)
            assert (account.linked_by, account.renamed) == (None, True), person
            assert cluster.people.unclaimed(user_name, scim_only=False) is None
    assert cluster.people.unclaimed("sam.rivera@acme.com", scim_only=True) == sam  # linked again, in the first run
    # Nobody else waits: the renamed are listed, nothing is asked (no --yes), nothing is sent.
    entries = len(cluster.server.audit.query(action="person.linked"))
    alone = cli("people", "link", "--all")
    assert alone.exit_code == 0 and "Nobody else to link." in alone.output, alone.output
    assert one_by_one in alone.output and "Left out: 1 person is linked already" in alone.output
    assert len(cluster.server.audit.query(action="person.linked")) == entries
    # An administrator who checked her user name with the provider links her by it, one by one.
    linked = cli("people", "link", emily, "--user-name", "emily.carter@acme.com", "--scim")
    assert linked.exit_code == 0, linked.output
    assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=True) == emily
    assert emily not in cli("people", "link", "--all", "--scim").output  # waiting for her first sign-in: counted


def test_after_an_issuer_change_the_bulk_link_lists_apart_whom_scim_renamed_since_their_sign_in(cli, cluster):
    """The claim fix's review, L3: Noah signed in, then a SCIM token renamed him to Mallory's user name. His subject
    names him, so his sign-ins go on, and the user name SCIM wrote stays on his account. After an issuer change,
    `unlink --all` then `link --all` would link his account, his roles with it, by Mallory's user name: it lists him
    apart, to link one by one, and links Sam, whom nobody renamed, as before."""
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    scim = {"authorization": f"Bearer {token}"}
    people = []
    for name in ("noah.patel", "sam.rivera"):
        body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": f"{name}@acme.com"}
        made = cluster.a.post("/scim/v2/Users", json=body, headers=scim)
        assert made.status_code == 201, made.text
        people.append(made.json()["id"])
    noah, sam = people
    for person in people:
        cluster.people.link(person, subject=f"{ISSUER} {person}", actor=person)  # they signed in
    cluster.people.grant(noah, Role.ADMIN, actor=BY_HAND)
    operation = {"op": "Replace", "path": "userName", "value": "mallory.west@acme.com"}
    patch = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [operation]}
    assert cluster.a.patch(f"/scim/v2/Users/{noah}", json=patch, headers=scim).status_code == 200  # by his subject
    assert cluster.people.account(noah).renamed and not cluster.people.account(sam).renamed
    assert cli("people", "unlink", "--all", "--yes").exit_code == 0  # the provider's issuer changed
    bulk = cli("people", "link", "--all", "--yes")
    assert bulk.exit_code == 0, bulk.output
    assert f"({noah}): mallory.west@acme.com (" in bulk.output and f"({noah}) is linked again" not in bulk.output
    assert f"latet server people link {noah} --user-name <name> --scim" in bulk.output
    assert f"({sam}) is linked again" in bulk.output
    assert cluster.people.unclaimed("mallory.west@acme.com", scim_only=False) is None
    assert cluster.people.unclaimed("sam.rivera@acme.com", scim_only=True) == sam


def test_sign_in_set_with_options_alone_never_overwrites_a_change_made_meanwhile(cli, cluster, tmp_path, monkeypatch):
    """`sign-in set --accounts …` reads the settings in use, changes them and sends them back with their etag: a change
    made on any instance between the two is refused in fixed words, exit 2, and stays; run again, it is made."""
    file = tmp_path / "sso.yaml"
    file.write_text(yaml.safe_dump(SSO), "utf-8")
    assert cli("sign-in", "set", str(file)).exit_code == 0
    forward, meanwhile = server_cli.admin_call, [{**SSO, "client_id": "latet-other"}]

    def call(method, path, body=None):
        if method == "PUT" and meanwhile:  # someone else's change, after this command read the settings
            other = cluster.servers[1].edition.settings_store
            other.set_section("sso", meanwhile.pop(), actor=BY_HAND)
        return forward(method, path, body)

    monkeypatch.setattr(server_cli, "admin_call", call)
    refused = cli("sign-in", "set", "--accounts", "scim")
    assert refused.exit_code == 2, refused.output
    assert "the sign-in settings were changed since they were read here" in refused.output
    stored = cluster.servers[1].edition.settings_store
    stored.drop()
    assert (stored.load().sso.client_id, stored.load().sso.accounts) == ("latet-other", SSO["accounts"])
    made = cli("sign-in", "set", "--accounts", "scim")
    assert made.exit_code == 0, made.output
    stored.drop()
    assert (stored.load().sso.client_id, stored.load().sso.accounts) == ("latet-other", "scim")


def test_what_the_server_said_is_printed_escaped_and_every_refusal_exits_2(monkeypatch, you):
    """F10, L4: a name, a message or a detail the server sent is printed with what a terminal would act on escaped;
    a refusal, a silence and a server that cannot be named all exit 2."""
    answers = {
        "ee/groups": {"groups": [{"id": "g_aaaaaaaaaa", "name": f"Pay{ESC}[31mments", "source": "local"}]},
        "ee/keys": {"keys": [{"id": "u_aaaaaaaaaa", "name": f"CI{ESC}]0;x", "controllers": []}]},
        "ee/roles": {"grants": [{"role": "admin", "subject": {"kind": "person", "id": "u_b", "name": f"E{ESC}c"}}]},
        "ee/sign-in": {
            "sso": None,
            "scim": {"url": f"https://x/{ESC}[2J", "tokens": [{"id": f"s_{ESC}", "expires": None, "maker": {}}]},
        },
    }

    def call(method, path, body=None):
        return answers[path]

    monkeypatch.setattr(server_cli, "admin_call", call)
    for args in (("groups", "list"), ("controllers", "list"), ("roles", "list"), ("sign-in", "show")):
        result = _run(*args)
        assert result.exit_code == 0 and ESC not in result.output and "\\x1b" in result.output, args
    # Sequences a terminal acts on that click does not strip from what it prints: ESC c (a reset), a bidirectional
    # override.
    for failure, escaped in (
        (ServerError(400, f"invalid {BIDI}x", (f"issuer: {ESC}c",)), ("\\u202e", "\\x1b")),  # what is wrong, and where
        (ServerError(0, "the server did not answer"), ()),
        (NotConnected(f"no server is named {BIDI}"), ("\\u202e",)),
    ):

        def refused(method, path, body=None, failure=failure):
            raise failure

        monkeypatch.setattr(server_cli, "admin_call", refused)
        for args in (("roles", "list"), ("sign-in", "off")):
            result = _run(*args)
            assert result.exit_code == 2 and ESC not in result.output and BIDI not in result.output, (failure, args)
            assert all(said in result.output for said in escaped), (failure, args)


def test_a_sign_in_settings_file_is_read_with_no_alias_no_link_and_only_values_json_holds(monkeypatch, tmp_path, you):
    """The final security review's L3: the file is read as the SIEM's and term sync's are, before anything is sent. An
    alias is refused where it stands (a few hundred bytes of nested ones would ask gigabytes of the computer that
    writes the body), and so is a value JSON does not hold, a date, binary data or a key that is not text, in words,
    exit 2, never a traceback; a symbolic link is not followed. A file written in UTF-16, as Windows PowerShell writes
    one, is read as before."""
    import json

    sent = []

    def call(method, path, body=None):  # as E0c's call: the body is written as JSON before it leaves
        if method == "PUT":
            sent.append(json.dumps(body, allow_nan=False))
            return {"sso": body["sso"], "redirect_uri": f"{URL}/_latet/sso/callback"}
        return {"sso": None}

    monkeypatch.setattr(server_cli, "admin_call", call)
    levels = ["a0: &a0 [x]", *(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]" for i in range(1, 6))]
    head = f"issuer: {ISSUER}\nclient_id: latet\naccounts: scim\n"
    for name, text, said in (
        ("aliases.yaml", head + "\n".join(levels) + "\nextra: *a5\n", "aliases (*name) are not allowed"),
        ("date.yaml", head + "groups: {admins: 2026-11-09}\n", "a date"),
        ("binary.yaml", head + "client_secret: !!binary c3NvLWNsaWVudC1zZWNyZXQ=\n", "binary data"),
        ("keys.yaml", head + "groups: {1: Latet admins}\n", "a key that is not text"),
        ("nan.yaml", head + "groups: {admins: .nan}\n", "a number JSON does not hold"),
    ):
        file = tmp_path / name
        file.write_text(text, "utf-8")
        refused = _run("sign-in", "set", str(file))
        assert refused.exit_code == 2 and said in refused.output, (name, refused.output)
        assert isinstance(refused.exception, SystemExit), (name, refused.exception)
    assert sent == []
    seen = []
    reads = server_cli.read_regular_bytes
    monkeypatch.setattr(
        server_cli, "read_regular_bytes", lambda path, **given: seen.append(given) or reads(path, **given)
    )
    file = tmp_path / "sso.yaml"
    file.write_bytes(yaml.safe_dump({"sso": SSO}).encode("utf-16"))  # with its byte order mark
    done = _run("sign-in", "set", str(file))
    assert done.exit_code == 0 and json.loads(sent[0])["sso"]["issuer"] == ISSUER, done.output
    assert seen and all(given.get("follow") is False for given in seen), seen
    target, link = tmp_path / "target.yaml", tmp_path / "link.yaml"
    target.write_text(yaml.safe_dump({"sso": SSO}), "utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        return  # Windows may refuse to make one without the privilege: the read's own `follow=False` is checked above
    linked = _run("sign-in", "set", str(link))
    assert linked.exit_code == 2 and len(sent) == 1, linked.output


def test_a_change_is_refused_to_an_agent_before_anything_is_sent(monkeypatch):
    """Decision (c): the setting, as every change, is yours to make: `admin_call` refuses an agent before it reads a
    token or reaches a server."""
    monkeypatch.setenv("CLAUDECODE", "1")
    for args in (("sign-in", "off"), ("sign-in", "scim-token", "--never"), ("sign-in", "secret", "--remove")):
        refused = _run(*args)
        assert refused.exit_code == 2 and "run it yourself" in refused.output, args
