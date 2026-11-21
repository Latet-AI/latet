"""A person's own name under the company's exceptions (E3's step 6b, decision (b) of 2026-10-25), on a server of two
instances. Under `exceptions: company`, a person's identity of a type the company allows is cut around what the
masking masks without it, on a server without single sign-on too; and a person whose account is linked to the
identity provider (SCIM, or the subject of their sign-in) is masked under the provider's name where the company and
their settings leave names (PERSON) in clear, a rename or a link applying at once on every instance. Where names are
masked, their own name stays in use, masked (a computer's name fills an empty one for that). While the provider names
them, their settings never change their name."""

import pytest

from latet.config import ProjectEntry
from latet.ee.policy_routes import ADMIN_POLICY
from latet.engine.build import build_masker
from latet.engine.types import Policy
from latet.engine.vault import Vault
from latet.floor import ACCOUNT_UNAVAILABLE, PROVIDER_NAME, BelowFloor

from ...conftest import ROOT_KEY
from .server_support import API, BY_HAND, SERVER_TESTS, UI, add_person, as_admin, settle, sign_in_as

pytestmark = SERVER_TESTS

SCIM = "/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
IDENTITY = "/_latet/server/v1/identity"  # a computer's call: its person's names
IBAN = "DE89 3704 0044 0532 0130 00"
TEXT = f"Pay {IBAN} to the supplier"
COMPANY = "latet_rules: 1\nexceptions: company\npolicies: {PERSON: allow}\n"
LOCKING = COMPANY + "terms: [{value: Project Falcon, type: PROJECT}]\n"  # and a term the company locks
AUTHOR = "commit 1a2b Author: Emily Jane Carter"  # her git author, as her computers send it
LET_THROUGH = {"PERSON": "allow"}  # a person's own settings letting names through
NAMES_MASKED = "latet_rules: 1\nexceptions: company\npolicies: {HOSTNAME: allow}\n"  # a company letting no name through


@pytest.fixture
def scim(cluster):
    """What the provider sends with every request: a SCIM token, made as on the Sign-in page."""
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    return {"authorization": f"Bearer {token}"}


def _policy(cluster, text=COMPANY):
    answer = cluster.a.put(ADMIN_POLICY, json={"policy": text}, headers=as_admin(cluster))
    assert answer.status_code == 200, answer.text


def _provisioned(cluster, scim, user_name, display_name):
    """A person SCIM made, as the provider provisions one."""
    body = {"schemas": [USER], "userName": user_name, "displayName": display_name}
    created = cluster.a.post(f"{SCIM}/Users", json=body, headers=scim)
    assert created.status_code == 201, created.text
    return cluster.server.identities.get(created.json()["id"])


def _spaces(cluster, person):
    """The person's space on each instance (built at its first use, as a request builds it)."""
    return [
        client.portal.call(server.spaces.runtime, person)
        for client, server in zip(cluster.clients, cluster.servers, strict=True)
    ]


def _named(cluster, person, name):
    """Waits until the person's space on each instance is masked under ``name``."""
    for runtime in _spaces(cluster, person):
        settle(lambda runtime=runtime: runtime.in_use().identity.full_name == name)


def _own_settings(cluster, person, *, policies=LET_THROUGH, **identity):
    """The person's own settings from their dashboard: their identity as given, and their ``policies`` (by default
    PERSON let through; under `exceptions: company` the raise keeps `allow` only for a type the company allows; None:
    left as they are)."""
    sign_in_as(cluster, person)
    view = cluster.a.get(f"{API}/config", headers=UI).json()
    body = {"etag": view["etag"], "identity": identity, **({"policies": policies} if policies is not None else {})}
    return cluster.a.put(f"{API}/config", json=body, headers=UI)


def _computer(cluster, person):
    """The headers of a computer of the person's, enrolled with a one-time code (`latet connect`)."""
    identities = cluster.server.identities
    _identity, token, key = identities.enrol(identities.code(person.id), "laptop")
    return {"x-latet-token": token, "x-latet-computer-key": key}


def _masked_at(masker, text):
    """The characters of ``text`` masked (a replacement that is not let through), by their places."""
    return {i for r in masker.mask(text).replacements for i in range(r.start, r.end)}


def test_under_company_exceptions_a_name_set_to_an_iban_leaves_nothing_in_clear_on_a_server_without_sso(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    assert _own_settings(cluster, emily, full_name=IBAN).status_code == 200
    _policy(cluster)
    assert cluster.people.account(emily.id) is None  # added by hand: no account at a provider
    space = _spaces(cluster, emily)[1]  # instance b
    settle(lambda: space.in_use().carves_terms)
    assert space.named_by_provider is None and space.in_use().identity.full_name == IBAN  # her own: never linked
    assert "DE89 3704" not in space.built.masker.mask(TEXT).text
    without = build_masker(space.in_use(), Vault(ROOT_KEY), None)[0]  # the settings in use, without her identity
    assert _masked_at(without, TEXT) <= _masked_at(space.built.masker, TEXT)


def test_a_linked_persons_name_is_the_providers_and_neither_her_settings_nor_her_computer_change_it(cluster, scim):
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    assert _own_settings(cluster, emily, full_name=IBAN).status_code == 200  # before the policy: her own, kept
    _policy(cluster, LOCKING)
    _named(cluster, emily, "Emily Carter")
    for space in _spaces(cluster, emily):
        assert space.config.identity.full_name == IBAN and space.named_by_provider == "Emily Carter"
        in_use = space.in_use()  # the overlay keeps what no dump holds: the carving, the company's terms
        assert in_use.carves_terms and [t.value for t in in_use.locked_terms] == ["Project Falcon"]
        assert "Project Falcon" not in space.built.masker.mask("ship Project Falcon").text
        masked = space.built.masker.mask(TEXT)
        assert IBAN not in masked.text and {r.etype for r in masked.replacements} == {"IBAN"}
    view = cluster.a.get(f"{API}/config", headers=UI).json()
    changed = {"etag": view["etag"], "identity": {"full_name": "Mallory"}}
    refused = cluster.a.put(f"{API}/config", json=changed, headers=UI)
    assert refused.status_code == 409 and refused.json()["floor"] == [PROVIDER_NAME]
    assert cluster.a.get(f"{API}/config", headers=UI).json()["etag"] == view["etag"]  # nothing changed
    sent = {"usernames": ["emily.carter"], "full_name": "Mallory"}
    assert cluster.a.put(IDENTITY, json=sent, headers=_computer(cluster, emily)).json() == {"ok": True}
    held = _spaces(cluster, emily)[0].config.identity
    assert "emily.carter" in held.usernames and held.full_name == IBAN  # the user name merged, the name set aside
    shown = cluster.a.get(f"{API}/config", headers=UI).json()["identity"]
    assert shown["full_name"] == "Emily Carter" and shown["full_name_from"] == "provider"
    below = cluster.a.get(f"{API}/ee/policy", headers=UI).json()["below"]
    assert not any("identity" in line for line in below), below  # the provider's name is no lowering of hers


def test_a_rename_or_a_link_from_the_provider_applies_at_once(cluster, scim):
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _policy(cluster)
    for person in (emily, daniel):  # each lets names through, as the company does
        assert _own_settings(cluster, person).status_code == 200
    _named(cluster, emily, "Emily Carter")
    _named(cluster, daniel, None)  # added by hand, never linked: his own settings name nobody
    rename = {"schemas": [PATCH], "Operations": [{"op": "Replace", "path": "displayName", "value": "Emily Okafor"}]}
    assert cluster.a.patch(f"{SCIM}/Users/{emily.id}", json=rename, headers=scim).status_code == 200
    _named(cluster, emily, "Emily Okafor")
    with cluster.people.changing(BY_HAND) as changing:
        cluster.people.link_person(daniel.id, subject="https://login.acme.test/realms/acme 7b21", within=changing)
    _named(cluster, daniel, "Daniel Okafor")  # no other change of his: the link alone
    # His computer sends his git author: it fills his empty name, kept, never in use while the provider's is.
    sent = {"usernames": ["daniel.okafor"], "full_name": "Daniel A. Okafor"}
    assert cluster.a.put(IDENTITY, json=sent, headers=_computer(cluster, daniel)).json() == {"ok": True}
    held = _spaces(cluster, daniel)[0].config.identity
    assert "daniel.okafor" in held.usernames and held.full_name == "Daniel A. Okafor"
    _named(cluster, daniel, "Daniel Okafor")


@pytest.mark.parametrize(
    ("policy", "linked", "policies"),
    [
        ("latet_rules: 1\nexceptions: personal\npolicies: {PERSON: allow}\n", True, LET_THROUGH),
        (NAMES_MASKED, True, None),
        (COMPANY, False, LET_THROUGH),
    ],
    ids=["her exceptions count", "no name allowed", "not linked"],
)
def test_a_persons_name_stays_her_own_where_the_company_allows_no_name_counts_her_exceptions_or_she_is_not_linked(
    cluster, scim, policy, linked, policies
):
    """Her own settings let PERSON through wherever they may (with `personal`, and for a person not linked): only the
    floor or her account keeps the provider's name out of use, and her name is hers to change. (Under a company allowing
    no name, a PERSON: allow of hers would be refused as below the floor.)"""
    if linked:
        emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    else:
        emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    _policy(cluster, policy)
    for space in _spaces(cluster, emily):
        settle(lambda space=space: space.floor_used is not None)
    assert _own_settings(cluster, emily, policies=policies, full_name="E. Carter").status_code == 200
    _named(cluster, emily, "E. Carter")
    assert all(space.named_by_provider is None for space in _spaces(cluster, emily))
    shown = cluster.a.get(f"{API}/config", headers=UI).json()["identity"]
    assert shown["full_name"] == "E. Carter" and shown["full_name_from"] is None


@pytest.mark.parametrize(
    "policy",
    [COMPANY, "latet_rules: 1\nexceptions: company\npolicies: {USERNAME: allow}\n"],
    ids=["her default", "user names alone allowed"],
)
def test_where_names_are_masked_a_linked_persons_own_name_stays_in_use_and_masked(cluster, scim, policy):
    """A full name is an identity value of type PERSON alone. Where PERSON is masked (her own settings' default, or a
    company that lets only user names through), the provider's name in place of hers would leave her git author, as
    her computers send it, undetected and in clear: her own stays in use, masked (fix round 1 of step 6b). Where the
    company lets names through, her name is the provider's to give all the same: hers is not to change (fix round 2)."""
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    _policy(cluster, policy)
    sent = {"full_name": "Emily Jane Carter"}
    assert cluster.a.put(IDENTITY, json=sent, headers=_computer(cluster, emily)).json() == {"ok": True}
    _named(cluster, emily, "Emily Jane Carter")
    for space in _spaces(cluster, emily):
        assert space.named_by_provider is None
        assert "Emily Jane Carter" not in space.built.masker.mask(AUTHOR).text
    changed = _own_settings(cluster, emily, policies=None, full_name="E. Carter")
    if policy == COMPANY:  # linked, and the company lets names through: the provider names her
        assert changed.status_code == 409 and changed.json()["floor"] == [PROVIDER_NAME]
        _named(cluster, emily, "Emily Jane Carter")
    else:
        assert changed.status_code == 200
        _named(cluster, emily, "E. Carter")
    shown = cluster.a.get(f"{API}/config", headers=UI).json()["identity"]
    assert shown["full_name"] == _spaces(cluster, emily)[0].in_use().identity.full_name
    assert shown["full_name_from"] is None


@pytest.mark.parametrize("masked_by", ["the company's policy", "her own settings"])
def test_a_computers_name_fills_an_empty_one_and_is_masked_once_names_are(cluster, scim, masked_by):
    """`latet connect` while the provider's name is in use: her computer's name fills her empty one and is kept, so
    that once names are masked (the company's policy drops PERSON, or she masks it herself) her git author is an
    identity value again, masked on every instance, never left undetected (fix round 2 of step 6b)."""
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    _policy(cluster)
    assert _own_settings(cluster, emily).status_code == 200  # names let through, as the company does
    _named(cluster, emily, "Emily Carter")
    sent = {"full_name": "Emily Jane Carter"}
    assert cluster.a.put(IDENTITY, json=sent, headers=_computer(cluster, emily)).json() == {"ok": True}
    for space in _spaces(cluster, emily):
        settle(lambda space=space: space.config.identity.full_name == "Emily Jane Carter")  # kept
        assert space.in_use().identity.full_name == "Emily Carter"  # not in use while the provider's is
    if masked_by == "the company's policy":
        _policy(cluster, NAMES_MASKED)
    else:
        assert _own_settings(cluster, emily, policies={}).status_code == 200
    _named(cluster, emily, "Emily Jane Carter")
    for space in _spaces(cluster, emily):
        assert space.named_by_provider is None
        assert "Emily Jane Carter" not in space.built.masker.mask(AUTHOR).text


def test_a_change_of_her_name_is_refused_while_the_provider_names_her_whatever_else_the_save_changes(cluster, scim):
    """Linked, the company letting names through: no save gives her a name of her own choosing, nor two saves in a row,
    whether it lets names through, masks them, or adds a folder masking them besides (fix round 2 of step 6b)."""
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    _policy(cluster)
    _named(cluster, emily, None)  # names masked (her default): her own settings, which name nobody
    sign_in_as(cluster, emily)
    view = cluster.a.get(f"{API}/config", headers=UI).json()
    refused = _own_settings(cluster, emily, full_name="Mallory")  # names let through, and her name, at once
    assert refused.status_code == 409 and refused.json()["floor"] == [PROVIDER_NAME]
    assert cluster.a.get(f"{API}/config", headers=UI).json()["etag"] == view["etag"]  # nothing changed
    assert _own_settings(cluster, emily).status_code == 200  # names let through alone
    _named(cluster, emily, "Emily Carter")
    refused = _own_settings(cluster, emily, policies={}, full_name="Mallory")  # names masked, and her name, at once
    assert refused.status_code == 409 and refused.json()["floor"] == [PROVIDER_NAME]
    assert _own_settings(cluster, emily, policies={}).status_code == 200  # names masked alone
    _named(cluster, emily, None)
    refused = _own_settings(cluster, emily, policies=None, full_name="Mallory")  # then her name
    assert refused.status_code == 409 and refused.json()["floor"] == [PROVIDER_NAME]
    # A folder masking names, and her name, in one save: no dashboard request carries both (folders have routes of
    # their own), so the space's runtime is asked, which every save goes through.
    space = _spaces(cluster, emily)[0]
    mine = space.config
    folder = ProjectEntry(path="/home/emily/billing", policies={"PERSON": Policy.MASK})

    def both(config):
        renamed = config.identity.model_copy(update={"full_name": "Mallory"})
        return config.model_copy(update={"projects": [*config.projects, folder], "identity": renamed})

    with pytest.raises(BelowFloor) as below:
        space.update_config(both)
    assert below.value.reasons == [PROVIDER_NAME] and space.config == mine


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------


def test_the_rules_page_is_told_her_name_is_locked_and_an_unread_account_is_said_as_hers(cluster, scim, monkeypatch):
    """Linked, the company letting names through, her own settings masking them: her own name is in use (no
    ``full_name_from``), and a change of it is refused all the same, which ``full_name_locked`` says. While her account
    cannot be read, a change is refused (503) in words that name her account, never the company's policy."""
    emily = _provisioned(cluster, scim, "emily.carter@acme.com", "Emily Carter")
    _policy(cluster)
    sign_in_as(cluster, emily)
    view = cluster.a.get(f"{API}/config", headers=UI).json()
    shown = view["identity"]
    assert (shown["full_name_from"], shown["full_name_locked"]) == (None, True)
    renamed = {"etag": view["etag"], "identity": {"full_name": "E. Carter"}}
    refused = cluster.a.put(f"{API}/config", json=renamed, headers=UI)
    assert refused.status_code == 409 and refused.json()["floor"] == [PROVIDER_NAME]
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")  # added by hand: never linked
    sign_in_as(cluster, daniel)
    assert cluster.a.get(f"{API}/config", headers=UI).json()["identity"]["full_name_locked"] is False

    def unread(identity_id):
        raise OSError("connection to 10.0.0.9 refused")

    sign_in_as(cluster, emily)
    monkeypatch.setattr(cluster.people, "account", unread)
    unanswered = cluster.a.put(f"{API}/config", json=renamed, headers=UI)
    assert (unanswered.status_code, unanswered.json()) == (503, {"error": ACCOUNT_UNAVAILABLE}), unanswered.text
    assert unanswered.headers["retry-after"] == "1" and "10.0.0.9" not in unanswered.text
