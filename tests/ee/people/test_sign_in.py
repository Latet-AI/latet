"""Single sign-on end to end on a server of two instances: a person signs in at the fake provider and lands in their
own dashboard session, whichever instance the provider sends the browser back to (ee/oidc.py)."""

import hashlib
import ipaddress
import json
import logging
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from latet.api.rest import COOKIE
from latet.ee import oidc
from latet.ee.license import FEATURES, State
from latet.ee.oidc import LINK, SignInError, secret_bound
from latet.ee.roles import Role
from latet.ee.settings import SsoSettings
from latet.logline import Paced
from latet.server import egress
from latet.server.operations import server_app

from ..licenses import at
from ..servers import URL
from .fake_provider import CLIENT_SECRET, ISSUER, SSO, FakeProvider
from .server_support import (
    ADMIN_EE,
    API,
    BY_HAND,
    SERVER_TESTS,
    UI,
    add_person,
    as_admin,
    community_sign_in,
    end_trial,
    license,
    on_trial,
    paused,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS

EMILY = {
    "sub": "0f6a-emily",
    "preferred_username": "emily.carter@acme.com",
    "name": "Emily Carter",
    "email": "emily.carter@acme.com",
    "email_verified": True,
    "groups": ["latet-users", "latet-admins", "payments-leads"],
}
DANIEL = {
    "sub": "1c2b-daniel",
    "preferred_username": "daniel.okafor@acme.com",
    "name": "Daniel Okafor",
    "email": "daniel.okafor@acme.com",
    "email_verified": True,
    "groups": ["latet-users"],
}
SAM = {"sub": "9e9e-sam", "preferred_username": "sam.rivera@acme.com", "name": "Sam Rivera", "groups": ["sales"]}
MALLORY = {  # someone else at the company: a provider account of their own
    "sub": "7d7d-mallory",
    "preferred_username": "mallory.west@acme.com",
    "name": "Mallory West",
    "email": "mallory.west@acme.com",
    "email_verified": True,
    "groups": ["latet-users"],
}
NOT_YOURS = "this company account is not yours on this server; ask an administrator to link it"


def set_up_sign_in(cluster, sso=SSO):
    """Single sign-on as an administrator sets it up (the sso section, the client secret bound to its issuer), known
    to both instances."""
    edition = cluster.server.edition
    edition.settings_store.set_section("sso", sso, actor=BY_HAND)
    edition.secrets.save("sso-client-secret", CLIENT_SECRET, bound=secret_bound(sso["issuer"]))
    settle(lambda: all(server.edition.settings().sso == SsoSettings.model_validate(sso) for server in cluster.servers))


@pytest.fixture
def provider(cluster, clock):
    fake = FakeProvider(clock)
    set_up_sign_in(cluster)
    for server in cluster.servers:
        server.edition.oidc.http = fake.client
    return fake


def _people(cluster):
    return cluster.server.identities.all(kind="person")


def _sign_in(client, provider, person, back=None, start="/_latet/sso/login"):
    """What the person's browser does: the sign-in page's button on ``client``, the provider's page, and back, to
    ``back`` when the load balancer chooses the other instance."""
    started = client.get(start, follow_redirects=False)
    assert started.status_code == 303 and "sign-in=" not in started.headers["location"], started.headers
    return (back or client).get(provider.authorize(started.headers["location"], person), follow_redirects=False)


def _signed_in(response):
    return response.status_code == 200 and 'url=/_latet/ui/"' in response.text


def _refused(response):
    return response.headers.get("location", "").removeprefix("/_latet/ui/?sign-in=")


def _session(cluster):
    """The dashboard session the browser holds, as the server reads it: whose, and how it was opened."""
    return cluster.server.sign_ins.session_of(cluster.a.cookies.get(COOKIE, domain="latet.acme.lan", path="/_latet"))


def _unlinked(cluster, person):
    """Whether no company account is linked to ``person``: their account names no subject."""
    account = cluster.people.account(person.id)
    return account is None or account.subject is None


class _Meanwhile:
    """Instance A's client of Valkey, but right after its first command on ``key``, ``then`` runs to its end: another
    request, reaching instance B at that very moment. Every other command goes through as it is."""

    def __init__(self, client, key, then):
        self._client, self._key, self._then = client, key, then

    def __getattr__(self, name):
        command = getattr(self._client, name)
        if not callable(command):
            return command

        def run(*args, **kwargs):
            answer = command(*args, **kwargs)
            if self._then is not None and args and args[0] == self._key:
                then, self._then = self._then, None
                then()
            return answer

        return run


def test_a_member_of_the_access_group_gets_an_account_a_session_and_the_groups_the_mapping_names(cluster, provider):
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    assert cluster.a.get(f"{API}/me").json()["edition"]["roles"] == ["admin", "user"]
    [emily] = _people(cluster)
    assert (emily.name, emily.email, emily.source) == ("Emily Carter", "emily.carter@acme.com", "oidc")
    assert [group.name for group in cluster.people.groups_of(emily.id)] == ["Latet admins", "Payments leads"]
    assert cluster.people.group("Payments leads").source == "oidc"
    assert cluster.server.identities.enabled_count() == 1
    assert _session(cluster) == (emily.id, "sso")  # opened with the company's sign-in (decision (c))


def test_the_provider_may_send_the_browser_back_to_the_other_instance(cluster, provider):
    assert _signed_in(_sign_in(cluster.a, provider, EMILY, back=cluster.b))
    assert cluster.a.get(f"{API}/me").status_code == 200 and cluster.b.get(f"{API}/me").status_code == 200


def test_the_dashboard_knows_how_its_session_was_opened(cluster, provider, monkeypatch):
    """Decision (c): the gate hands the dashboard the session's kind (``scope["latet.via"]``), ``sso`` or ``code``."""
    seen = []

    async def probe(scope, receive, send):
        seen.append(scope.get("latet.via"))
        await JSONResponse({})(scope, receive, send)

    async def app(identity):
        return probe

    for server in cluster.servers:
        monkeypatch.setattr(server.spaces, "app", app)
    _sign_in(cluster.a, provider, EMILY)
    cluster.a.get(f"{API}/me")
    cluster.b.get(f"{API}/me")  # found by the lookup there, held in memory here
    cluster.b.get(f"{API}/me")
    [emily] = _people(cluster)
    community_sign_in(cluster, emily)
    cluster.a.get(f"{API}/me")
    cluster.a.get(f"{API}/me")  # held in this instance's memory, with its kind
    assert seen == ["sso", "sso", "sso", "code", "code"]


def test_someone_outside_the_access_group_gets_no_account_and_is_told_why(cluster, provider):
    assert _refused(_sign_in(cluster.a, provider, SAM)) == "not-allowed"
    assert _people(cluster) == []
    assert cluster.a.get(f"{API}/me").status_code == 401


def test_a_person_added_by_hand_is_never_taken_by_a_sign_in_with_their_e_mail(cluster, provider):
    """H1: an unlinked administrator added by hand, and someone whose provider account names their e-mail as its user
    name and e-mail: not that administrator, and no new account with their e-mail either."""
    daniel = add_person(cluster, "Daniel Okafor", "Daniel.Okafor@acme.com", Role.ADMIN)
    assert _refused(_sign_in(cluster.a, provider, DANIEL)) == "not-provisioned"
    assert [person.id for person in _people(cluster)] == [daniel.id]
    assert cluster.people.account(daniel.id) is None  # no accounts row changed
    set_up_sign_in(cluster, {**SSO, "accounts": "scim", "access_group": None})
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, DANIEL)) == "not-provisioned"
    assert cluster.people.account(daniel.id) is None


def test_a_person_added_by_hand_links_their_company_account_from_their_own_session(cluster, provider):
    """H1 (i): **Link my company account**: begun from Daniel's own dashboard session, recorded, and his next sign-in
    finds him."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    sign_in_as(cluster, daniel)
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL, start="/_latet/sso/link"))
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"
    entries = cluster.server.audit.query(action="person.linked")  # the subject, then the user name it came with
    assert {(entry.actor, entry.target) for entry in entries} == {(daniel.id, daniel.id)}
    assert sorted(field for entry in entries for field in entry.details["fields"]) == ["subject", "user_name"]
    assert "daniel.okafor" not in json.dumps([entry.details for entry in entries])
    assert _session(cluster) == (daniel.id, "code")  # begun from a one-time code's session: it stays one
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert [person.id for person in _people(cluster)] == [daniel.id]


def test_ones_own_link_never_links_an_account_whose_user_name_scim_changed(cluster, provider):
    """The claim fix's review, L1: a SCIM rename leaves the account to an administrator's link, and **Link my company
    account** is no way round it. A SCIM token renamed Emily to Mallory's user name before anything was in her account;
    whoever holds a session at her computer (connected with an administrator's code) and signs in at the provider as
    Mallory links nothing. Daniel, renamed and back, does not link his own account either: the user name SCIM wrote
    names nobody until an administrator links it, and then his sign-in finds him."""
    set_up_sign_in(cluster, {**SSO, "accounts": "scim"})
    scim = _scim(cluster)
    identities = cluster.server.identities
    emily = _provisioned(cluster, scim, "emily.carter@acme.com")
    assert _scim_renamed(cluster, scim, emily, MALLORY["preferred_username"]) == 200
    community_sign_in(cluster, identities.get(emily))  # her computer, and `latet dashboard` on it
    assert _refused(_sign_in(cluster.a, provider, MALLORY, start="/_latet/sso/link")) == "not-provisioned"
    account = cluster.people.account(emily)
    assert (account.subject, account.linked_by, account.renamed) == (None, None, True)
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, MALLORY)) == "not-provisioned"
    daniel = _provisioned(cluster, scim, "daniel.okafor@acme.com")
    assert _scim_renamed(cluster, scim, daniel, "dan.okafor@acme.com") == 200
    assert _scim_renamed(cluster, scim, daniel, "daniel.okafor@acme.com") == 200  # back: his own user name again
    community_sign_in(cluster, identities.get(daniel))
    assert _refused(_sign_in(cluster.a, provider, DANIEL, start="/_latet/sso/link")) == "not-provisioned"
    assert cluster.people.account(daniel).subject is None
    _linked_by_an_administrator(cluster, daniel, "daniel.okafor@acme.com")
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert _session(cluster)[0] == daniel and cluster.people.account(daniel).subject == f"{ISSUER} 1c2b-daniel"


def test_linking_needs_ones_own_session_and_a_company_account_no_one_else_has(cluster, provider):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    assert _refused(cluster.a.get("/_latet/sso/link", follow_redirects=False)) == "expired"  # no session
    sign_in_as(cluster, daniel)
    elsewhere = cluster.a.get("/_latet/sso/link", headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
    assert _refused(elsewhere) == "failed"
    _sign_in(cluster.a, provider, EMILY)  # Emily's company account is hers
    sign_in_as(cluster, daniel)
    assert _refused(_sign_in(cluster.a, provider, EMILY, start="/_latet/sso/link")) == "failed"
    assert cluster.people.account(daniel.id) is None or cluster.people.account(daniel.id).subject is None


KELVIN_KATE = "\N{KELVIN SIGN}ate.king@acme.com"  # the final review's probe: a K that is no ASCII K


@pytest.mark.parametrize("accounts", ["group", "scim"])
def test_a_first_sign_in_with_a_look_alike_user_name_claims_no_one_elses_account(cluster, provider, accounts):
    """F3, end to end: an administrator an administrator linked by user name (or one SCIM made), who never signed in, is
    not claimed by a company account whose user name reads as theirs; it gets an account of its own where accounts are
    given (group), and none where SCIM gives them."""
    if accounts == "group":
        kate = add_person(cluster, "Kate King", "kate.king@acme.com", Role.ADMIN)
        cluster.people.link_person(kate.id, user_name="kate.king@acme.com", actor=BY_HAND)
    else:
        set_up_sign_in(cluster, {**SSO, "accounts": "scim", "access_group": None})
        kate, _code = cluster.server.identities.add_person("Kate King", "kate.king@acme.com", source="scim")
        cluster.people.link(kate.id, source="scim", user_name="kate.king@acme.com", scim_managed=True)
        cluster.people.grant(kate.id, Role.ADMIN, actor=BY_HAND)
    answer = _sign_in(cluster.a, provider, {**MALLORY, "preferred_username": KELVIN_KATE})
    assert _unlinked(cluster, kate)
    if accounts == "scim":
        assert _refused(answer) == "not-provisioned"
        assert cluster.a.cookies.get(COOKIE, domain="latet.acme.lan", path="/_latet") is None  # no session opened
        return
    assert _signed_in(answer)
    mallory, via = _session(cluster)
    assert mallory != kate.id and via == "sso"
    assert cluster.people.account(mallory).user_name == KELVIN_KATE  # a user name of its own, beside Kate's
    assert cluster.a.get(f"{API}/me").json()["edition"]["roles"] == ["user"]


# A user name claim with white space around it, which Python's strip() would take off, or a character Latet never keeps.
NOT_AS_KEPT = (
    "emily.carter@acme.com\N{NO-BREAK SPACE}",
    "\N{IDEOGRAPHIC SPACE}emily.carter@acme.com",
    "emily.carter@acme.com\t",
    " emily.carter@acme.com",
    "emily.carter@acme.com\N{ZERO WIDTH SPACE}",
)


@pytest.mark.parametrize("accounts", ["group", "scim"])
def test_a_first_sign_in_whose_user_name_is_not_as_latet_keeps_one_claims_no_ones_account(cluster, provider, accounts):
    """The final security review's L1: the user name claim is matched as the ID token sends it, never stripped. With
    white space around it, or a character Latet never keeps, it names nobody: Emily's account, linked by an
    administrator (group) or made by SCIM, waits for her own first sign-in, and the other company account gets one of
    its own with no user name where accounts are given, none where SCIM gives them."""
    if accounts == "group":
        emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
        cluster.people.link_person(emily.id, user_name="emily.carter@acme.com", actor=BY_HAND)
    else:
        set_up_sign_in(cluster, {**SSO, "accounts": "scim", "access_group": None})
        emily, _code = cluster.server.identities.add_person("Emily Carter", "emily.carter@acme.com", source="scim")
        cluster.people.link(emily.id, source="scim", user_name="emily.carter@acme.com", scim_managed=True)
    for n, user_name in enumerate(NOT_AS_KEPT):
        cluster.a.cookies.clear()
        other = {
            "sub": f"7d7d-other-{n}",
            "preferred_username": user_name,
            "name": "Mallory West",
            "groups": ["latet-users"],
        }
        answer = _sign_in(cluster.a, provider, other)
        assert _unlinked(cluster, emily), repr(user_name)
        if accounts == "scim":
            assert _refused(answer) == "not-provisioned", repr(user_name)
            assert cluster.a.cookies.get(COOKIE, domain="latet.acme.lan", path="/_latet") is None
            continue
        assert _signed_in(answer), repr(user_name)
        mallory, _via = _session(cluster)
        assert mallory != emily.id and cluster.people.account(mallory).user_name is None, repr(user_name)
    assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=False) == emily.id
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))  # hers, as sent
    assert _session(cluster)[0] == emily.id


def test_ones_own_link_never_takes_a_look_alike_user_name(cluster, provider):
    """F3: from Kate's own session (`latet dashboard`'s, which an agent at her computer can open), a company account
    whose user name reads as hers is not linked to her."""
    kate = add_person(cluster, "Kate King", "kate.king@acme.com", Role.ADMIN)
    cluster.people.link_person(kate.id, user_name="kate.king@acme.com", actor=BY_HAND)
    community_sign_in(cluster, kate)
    answer = _sign_in(cluster.a, provider, {**MALLORY, "preferred_username": KELVIN_KATE}, start=LINK)
    assert _refused(answer) == "not-provisioned" and _unlinked(cluster, kate)


@pytest.mark.parametrize("source", ["local", "scim"])
def test_ones_own_link_takes_a_company_account_whose_verified_e_mail_is_the_one_given_by_hand_or_by_scim(
    cluster, provider, source
):
    """Fix round 3 (H1 (i)): with no user name on his account, Daniel's own link takes the company account whose e-mail
    the provider verified and is the one an administrator or SCIM gave him (case aside, as Latet compares e-mails)."""
    daniel, _code = cluster.server.identities.add_person("Daniel Okafor", "Daniel.Okafor@ACME.com", source=source)
    sign_in_as(cluster, daniel)
    assert _signed_in(_sign_in(cluster.a, provider, {**DANIEL, "preferred_username": "dokafor"}, start=LINK))
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"


@pytest.mark.parametrize(
    ("given", "verified", "linked"),
    [
        ("emily.carter@acme.com", "Emily.Carter@ACME.com", True),  # ASCII, case aside
        ("kate@acme.com", "\N{KELVIN SIGN}ate@acme.com", False),  # its lower case is k, in PostgreSQL's too
        ("ivan@acme.com", "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}van@acme.com", False),
        ("\N{KELVIN SIGN}ate@acme.com", "kate@acme.com", False),  # either side
        ("jos\N{LATIN SMALL LETTER E WITH ACUTE}@acme.com", "jos\N{LATIN SMALL LETTER E WITH ACUTE}@acme.com", False),
    ],
)
def test_the_e_mail_that_links_is_compared_in_ascii_alone(cluster, provider, given, verified, linked):
    """Fix round 4: PostgreSQL's ``lower`` follows the database's locale, which folds look-alikes onto ASCII letters
    (the Kelvin sign onto k, a capital I with a dot onto i): an e-mail is compared only when both it and the one given
    are ASCII, ASCII's case aside. A non-ASCII e-mail on either side never links: an administrator does."""
    person, _code = cluster.server.identities.add_person("Emily Carter", given)
    sign_in_as(cluster, person)
    token = {**EMILY, "preferred_username": "ecarter", "email": verified}
    answer = _sign_in(cluster.a, provider, token, start=LINK)
    if linked:
        assert _signed_in(answer) and cluster.people.account(person.id).subject == f"{ISSUER} 0f6a-emily"
    else:
        assert _refused(answer) == "not-provisioned" and _unlinked(cluster, person)


def test_a_company_account_with_another_e_mail_is_not_linked_and_the_refusal_names_nothing(
    cluster, provider, caplog, monkeypatch
):
    """Fix round 3: refused in fixed words, nothing linked, recorded as the flow's other refusals are, no claim's
    value in the log, the event or the administration log."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    events = []
    monkeypatch.setattr(cluster.server.edition, "record", events.append)
    sign_in_as(cluster, daniel)
    other = {**DANIEL, "email": "d.okafor@acme.com", "preferred_username": "d.okafor@acme.com"}
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        assert _refused(_sign_in(cluster.a, provider, other, start=LINK)) == "not-provisioned"
    assert _unlinked(cluster, daniel)
    assert NOT_YOURS in caplog.text
    said = caplog.text + json.dumps(events)
    for value in ("d.okafor", "daniel.okafor", "1c2b-daniel", "Daniel"):
        assert value not in said
    assert [(event["outcome"], event["reason"], event["identity"]) for event in events] == [
        ("refused", "not-provisioned", daniel.id)
    ]
    assert cluster.server.audit.query(action="person.linked") == []
    assert _session(cluster) == (daniel.id, "code")  # his session as it was


@pytest.mark.parametrize("verified", ["absent", False, "true", 1])
def test_an_e_mail_not_verified_as_json_true_links_nobody(cluster, provider, verified):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    sign_in_as(cluster, daniel)
    token = {key: value for key, value in DANIEL.items() if key != "email_verified"}
    if verified != "absent":
        token["email_verified"] = verified
    assert _refused(_sign_in(cluster.a, provider, token, start=LINK)) == "not-provisioned"
    assert _unlinked(cluster, daniel)


def test_an_account_with_a_user_name_is_linked_by_that_user_name_alone(cluster, provider):
    """Fix round 3: an account that has a user name (an administrator's or SCIM's) takes the company account whose
    user-name claim, the one the Sign-in settings name, is that user name (case aside); the e-mail he was given,
    verified, does not stand for it."""
    set_up_sign_in(cluster, {**SSO, "user_name_claim": "upn"})
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link_person(daniel.id, user_name="dokafor@corp.acme.com", actor=BY_HAND)
    sign_in_as(cluster, daniel)
    for named in ({"preferred_username": "dokafor@corp.acme.com"}, {"upn": "d.okafor@corp.acme.com"}):
        assert _refused(_sign_in(cluster.a, provider, {**DANIEL, **named}, start=LINK)) == "not-provisioned"
        assert _unlinked(cluster, daniel)
    unverified = {**DANIEL, "upn": "DOkafor@corp.acme.com", "email_verified": False}
    assert _signed_in(_sign_in(cluster.a, provider, unverified, start=LINK))
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"


@pytest.mark.parametrize("claim", ["email", "preferred_username"])
def test_a_claim_holding_a_character_latet_never_keeps_names_nobody(cluster, provider, claim):
    """Fix round 3: an e-mail or a user name with a zero byte in it (which PostgreSQL's text cannot hold) is nobody's:
    refused as any other company account that is not the person's, never a server that cannot answer."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    if claim == "preferred_username":
        cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    sign_in_as(cluster, daniel)
    token = {**DANIEL, claim: DANIEL[claim] + "\N{NULL}"}
    assert _refused(_sign_in(cluster.a, provider, token, start=LINK)) == "not-provisioned"
    assert _unlinked(cluster, daniel)


@pytest.mark.parametrize("given", ["no e-mail", "an ID token's e-mail"])
def test_a_person_with_neither_a_user_name_nor_an_e_mail_given_by_hand_is_linked_by_an_administrator_alone(
    cluster, provider, given
):
    """Fix round 3: no user name on the account and no e-mail given by hand or by SCIM (none, or one an ID token brought:
    an account made at a first sign-in, unlinked since): their own link is refused; an administrator links them."""
    email, source = (None, "local") if given == "no e-mail" else ("daniel.okafor@acme.com", "oidc")
    daniel, _code = cluster.server.identities.add_person("Daniel Okafor", email, source=source)
    sign_in_as(cluster, daniel)
    assert _refused(_sign_in(cluster.a, provider, DANIEL, start=LINK)) == "not-provisioned"
    assert _unlinked(cluster, daniel)
    cluster.people.link_person(daniel.id, user_name=DANIEL["preferred_username"], actor=BY_HAND)
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert _session(cluster) == (daniel.id, "sso")


def test_with_allowed_domains_the_e_mail_that_links_is_within_them(cluster, provider):
    """Fix round 3: Google's sign-in, its hosted domain allowed, the e-mail verified and the one given by hand, but at a
    domain the settings do not allow: refused; allowed, linked."""
    google = {
        "issuer": "https://accounts.google.com",
        "client_id": "latet",
        "accounts": "open",
        "allowed_domains": ["acme.com"],
    }
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme-partners.com")
    claims = {"sub": "2", "hd": "acme.com", "email": "daniel.okafor@acme-partners.com", "email_verified": True}
    oidc = cluster.server.edition.oidc
    narrow = SsoSettings.model_validate(google)
    with pytest.raises(SignInError) as caught:
        oidc._person(narrow, narrow.issuer, claims, "203.0.113.7", daniel.id)
    assert (caught.value.code, caught.value.detail, caught.value.identity) == ("not-provisioned", NOT_YOURS, daniel.id)
    assert _unlinked(cluster, daniel)
    wider = SsoSettings.model_validate({**google, "allowed_domains": ["acme.com", "acme-partners.com"]})
    assert oidc._person(wider, wider.issuer, claims, "203.0.113.7", daniel.id).id == daniel.id
    assert cluster.people.account(daniel.id).subject == "https://accounts.google.com 2"


def test_a_session_at_daniels_computer_cannot_link_another_company_account_to_him(cluster, provider):
    """Fix round 3, the finding: an administrator who connected a computer as Daniel, or an agent at his computer,
    opens `latet dashboard` there and links Mallory's company account to him (hers even names his e-mail as its user
    name). Refused; and Mallory's single sign-on never opens a session as Daniel."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.ADMIN)
    community_sign_in(cluster, daniel)  # `latet dashboard`'s one-time link, from a computer connected as him
    mallory = {**MALLORY, "preferred_username": "daniel.okafor@acme.com"}
    assert _refused(_sign_in(cluster.a, provider, mallory, start=LINK)) == "not-provisioned"
    assert _unlinked(cluster, daniel)
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, mallory))  # accounts: group: someone new, never Daniel
    assert _session(cluster)[0] != daniel.id
    assert cluster.a.get(f"{API}/me").json()["edition"]["roles"] == ["user"]
    assert _unlinked(cluster, daniel)


def test_an_account_an_administrator_linked_by_user_name_is_found_at_the_first_sign_in(cluster, provider):
    """H1 (ii): an administrator links a person added by hand by their user name at the provider."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert [person.id for person in _people(cluster)] == [daniel.id]
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"
    cluster.a.cookies.clear()  # the account is claimed once: another subject with that user name is someone else
    assert _signed_in(_sign_in(cluster.a, provider, {**DANIEL, "sub": "other", "email_verified": False}))
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"
    assert _session(cluster)[0] != daniel.id and len(_people(cluster)) == 2


def test_the_page_never_turns_single_sign_on_off_and_on_past_the_user_name_claims_guard(cluster, provider):
    """The final fix's re-review: the user-name claim's guard weighed a change between two settings alone, so under
    company_changes: any-session the page turned single sign-on off, then on with the claim `nickname`, and Mallory's
    company account, its nickname set to the user name of Priya's account waiting for its first sign-in, signed in as
    Priya. The page's turning off is refused at once while an account waits (or someone signed in), whatever
    company_changes says: the claim in use stays, and Mallory signs in as herself."""
    set_up_sign_in(cluster, {**SSO, "company_changes": "any-session"})
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # waits for her sign-in
    sign_in_as(cluster, emily)  # `latet dashboard`'s session, which any-session lets change what applies to everyone
    nickname = {**SSO, "user_name_claim": "nickname"}
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": nickname}).status_code == 409
    off = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None})
    assert off.status_code == 409 and "latet server sign-in off" in off.json()["error"]
    assert "priya" not in off.text.lower() and "preferred_username" not in off.text
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": nickname}).status_code == 409
    assert all(server.edition.settings().sso.user_name_claim == "preferred_username" for server in cluster.servers)
    assert [entry for entry in cluster.server.audit.query(action="sign-in.changed") if entry.actor == emily.id] == []
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, {**MALLORY, "nickname": "priya.shah@acme.com"}))
    assert _session(cluster)[0] != priya.id and _unlinked(cluster, priya)


def test_an_e_mail_the_provider_has_not_verified_is_nobodys(cluster, provider):
    assert _signed_in(_sign_in(cluster.a, provider, {**EMILY, "email_verified": False}))
    [emily] = _people(cluster)
    assert emily.email is None


def test_an_unverified_e_mail_is_never_a_user_name(cluster, provider):
    set_up_sign_in(cluster, {**SSO, "user_name_claim": "email"})
    assert _refused(_sign_in(cluster.a, provider, {**EMILY, "email_verified": "true"})) == "failed"
    assert _people(cluster) == []


def test_a_person_is_known_by_their_subject_whatever_their_user_name_becomes(cluster, provider):
    _sign_in(cluster.a, provider, EMILY)
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, {**EMILY, "preferred_username": "emily.carter-jones@acme.com"}))
    assert len(_people(cluster)) == 1


def test_with_accounts_from_scim_only_provisioned_people_sign_in(cluster, provider):
    """M2: who gets an account is the setting, never whether a token lives: the last token ending changes nothing."""
    set_up_sign_in(cluster, {**SSO, "accounts": "scim"})
    made, _token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    assert _refused(_sign_in(cluster.a, provider, EMILY)) == "not-provisioned"
    cluster.server.edition.scim_tokens.remove(made.id)
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, EMILY)) == "not-provisioned"
    assert _people(cluster) == []


def _scim(cluster):
    """What a provider's SCIM client sends with each request: a token, made as on the Sign-in page."""
    _made, token = cluster.server.edition.scim_tokens.make(made_by=BY_HAND)
    return {"authorization": f"Bearer {token}"}


def _provisioned(cluster, scim, user_name):
    body = {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"], "userName": user_name, "displayName": "Emily"}
    made = cluster.a.post("/scim/v2/Users", json=body, headers=scim)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _scim_renamed(cluster, scim, person, user_name):
    operation = {"op": "Replace", "path": "userName", "value": user_name}
    patch = {"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [operation]}
    return cluster.a.patch(f"/scim/v2/Users/{person}", json=patch, headers=scim).status_code


def _linked_by_an_administrator(cluster, person, user_name):
    """`latet server people link <id> --user-name <name> --scim`, through the administration API."""
    link = {"user_name": user_name, "scim": True}
    answer = cluster.a.post(f"{ADMIN_EE}/people/{person}/link", json=link, headers=as_admin(cluster))
    assert answer.status_code == 200, answer.text


@pytest.mark.parametrize("accounts", ["scim", "group", "open"])
def test_someone_the_provider_renamed_before_their_first_sign_in_is_linked_again_by_an_administrator(
    cluster, provider, accounts
):
    """The provider really renamed Emily before she ever signed in: SCIM's rename leaves her account to an
    administrator's link. Her first sign-in under her new user name finds no account: with `accounts: scim` it is
    refused as not provisioned; with `group` or `open` it makes her a new, empty one, which an administrator removes.
    Once an administrator linked her account by that user name, her sign-in opens it."""
    sso = {**SSO, "accounts": accounts}
    if accounts == "open":  # anyone the provider signs in: no access group
        del sso["access_group"]
    set_up_sign_in(cluster, sso)
    scim = _scim(cluster)
    emily = _provisioned(cluster, scim, "emily.carter@acme.com")
    assert _scim_renamed(cluster, scim, emily, "emily.jones@acme.com") == 200
    jones = {**EMILY, "preferred_username": "emily.jones@acme.com", "email": "emily.jones@acme.com"}
    first = _sign_in(cluster.a, provider, jones)
    if accounts == "scim":
        assert _refused(first) == "not-provisioned"
    else:  # an account of her own, empty, which keeps no user name another account has
        assert _signed_in(first)
        [made] = [person.id for person in _people(cluster) if person.id != emily]
        assert _session(cluster)[0] == made and cluster.people.account(made).user_name is None
        assert cluster.server.identities.remove(made)  # `latet server people remove`
    assert [person.id for person in _people(cluster)] == [emily] and cluster.people.account(emily).subject is None
    _linked_by_an_administrator(cluster, emily, "emily.jones@acme.com")
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, jones))
    assert _session(cluster)[0] == emily and cluster.people.account(emily).subject == f"{ISSUER} 0f6a-emily"
    assert [person.id for person in _people(cluster)] == [emily]


def test_a_scim_token_that_renames_someone_who_never_signed_in_hands_their_account_to_nobody(cluster, provider):
    """The final security review's probe of its wider option: a leaked SCIM token renames Emily, provisioned and holding
    nothing, to Mallory's user name, and Mallory, who never signed in either, signs in; the token renames the account
    back, and an administrator then makes Emily the code for her computer. Mallory's sign-in finds no account, and
    Emily's account waits for an administrator's link: once it is made, Emily's code connects her computer to her own
    account, and her own sign-in opens her session."""
    set_up_sign_in(cluster, {**SSO, "accounts": "scim"})
    scim = _scim(cluster)
    emily = _provisioned(cluster, scim, "emily.carter@acme.com")
    assert _scim_renamed(cluster, scim, emily, MALLORY["preferred_username"]) == 200
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, MALLORY)) == "not-provisioned"
    assert _session(cluster) is None and cluster.people.account(emily).subject is None
    assert _scim_renamed(cluster, scim, emily, "emily.carter@acme.com") == 200  # renamed back: it still waits
    for person in (MALLORY, EMILY):  # nobody's first sign-in takes it, by either user name
        cluster.a.cookies.clear()
        assert _refused(_sign_in(cluster.a, provider, person)) == "not-provisioned", person["sub"]
    assert cluster.people.unclaimed("emily.carter@acme.com", scim_only=False) is None
    _linked_by_an_administrator(cluster, emily, "emily.carter@acme.com")
    identities = cluster.server.identities
    enrolled = identities.enrol(identities.code(emily), "emily-laptop")  # an administrator's code, used by Emily
    assert enrolled is not None and enrolled[0].id == emily
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    assert _session(cluster)[0] == emily and cluster.people.account(emily).subject == f"{ISSUER} 0f6a-emily"
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, MALLORY)) == "not-provisioned"  # nothing of hers, ever
    assert [person.id for person in _people(cluster)] == [emily]


def test_a_sign_in_that_brings_the_user_name_scim_wrote_confirms_it_and_one_that_does_not_leaves_it_marked(
    cluster, provider
):
    """The claim fix's review, L3: a SCIM rename of someone who signed in marks the account too. Their sign-ins go on
    by their subject, and the user name SCIM wrote stays, which `link --all` would link again after an issuer change.
    A sign-in whose user name is the one SCIM wrote (A to Z's case aside) confirms it and ends the mark; a sign-in that
    brings another user name leaves it."""
    set_up_sign_in(cluster, {**SSO, "accounts": "scim"})
    scim = _scim(cluster)
    people = cluster.people
    emily = _provisioned(cluster, scim, "emily.carter@acme.com")
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))  # her first sign-in: her subject names her
    assert _scim_renamed(cluster, scim, emily, MALLORY["preferred_username"]) == 200  # a leaked token, by her subject
    assert people.account(emily).renamed
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))  # by her subject, as emily.carter: not the name SCIM wrote
    assert _session(cluster)[0] == emily and people.account(emily).renamed
    daniel = _provisioned(cluster, scim, "daniel.okafor@acme.com")
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert _scim_renamed(cluster, scim, daniel, "dan.okafor@acme.com") == 200  # the provider renamed him
    assert people.account(daniel).renamed
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, {**DANIEL, "preferred_username": "Dan.Okafor@acme.com"}))
    account = people.account(daniel)
    assert (account.renamed, account.user_name, account.subject) == (
        False,
        "dan.okafor@acme.com",
        f"{ISSUER} 1c2b-daniel",
    )
    assert people.account(emily).renamed  # nobody's but her own sign-in touches her account


def test_a_disabled_person_is_refused(cluster, provider):
    _sign_in(cluster.a, provider, EMILY)
    cluster.server.identities.disable(_people(cluster)[0].id)
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, EMILY)) == "disabled"


@pytest.mark.parametrize(("how", "why"), [("twice", "expired"), ("late", "expired"), ("another browser", "failed")])
def test_a_callback_used_twice_late_or_in_another_browser_signs_nobody_in(cluster, provider, clock, how, why):
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    back = provider.authorize(started.headers["location"], EMILY)
    if how == "twice":
        cluster.a.get(back, follow_redirects=False)
        cluster.a.cookies.clear()
    elif how == "late":
        clock.advance(601)
    else:
        cluster.a.cookies.clear()  # the link, opened in a browser that did not begin the sign-in
    assert _refused(cluster.b.get(back, follow_redirects=False)) == why
    assert cluster.a.get(f"{API}/me").status_code == 401


def test_of_two_callbacks_with_the_same_state_at_once_only_the_first_signs_in(cluster, provider, monkeypatch):
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    back = provider.authorize(started.headers["location"], EMILY)
    state = parse_qs(urlsplit(back).query)["state"][0]
    bus, second = cluster.server.bus, []
    flow = bus.key("sso:flow:" + hashlib.sha256(state.encode()).hexdigest())
    # The same link reaches instance B while instance A takes the sign-in from Valkey: with a read and then a delete,
    # both would take it, and both would go to the provider with the code.
    meanwhile = _Meanwhile(bus.client, flow, lambda: second.append(cluster.b.get(back, follow_redirects=False)))
    monkeypatch.setattr(bus, "client", meanwhile)
    assert _signed_in(cluster.a.get(back, follow_redirects=False))
    assert [_refused(response) for response in second] == ["expired"]
    assert len(provider.token_requests) == 1  # the code went to the provider once


@pytest.mark.parametrize(
    ("error", "why"), [("access_denied", "not-allowed"), ("login_required", "failed"), ("Emily's own words", "failed")]
)
def test_the_providers_refusal_is_said_on_the_sign_in_page_in_fixed_words(cluster, provider, caplog, error, why):
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        back = cluster.a.get("/_latet/sso/callback", params={"error": error, "state": state}, follow_redirects=False)
    assert _refused(back) == why
    assert "Emily" not in caplog.text and ("other" in caplog.text) is (error == "Emily's own words")


def test_a_callback_no_sign_in_waits_for_records_nothing(cluster, provider, monkeypatch):
    events = []
    monkeypatch.setattr(cluster.server.edition, "record", events.append)
    back = cluster.a.get("/_latet/sso/callback?code=x&state=never-begun", follow_redirects=False)
    assert _refused(back) == "expired" and events == []


def test_too_many_callbacks_from_one_address_wait(cluster, provider, monkeypatch):
    asked = []

    def allow(key, limit, per):
        asked.append(key.partition(":")[0])
        return not key.startswith("sso-callback:")

    monkeypatch.setattr(cluster.server.bus, "allow", allow)
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    back = provider.authorize(started.headers["location"], EMILY)
    assert _refused(cluster.a.get(back, follow_redirects=False)) == "unavailable"
    assert asked == ["sso-login", "sso-callback"] and provider.token_requests == []


def test_new_keys_at_the_provider_are_fetched_once_and_the_sign_in_works(cluster, provider, clock):
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    provider.rotate()
    clock.advance(120)
    cluster.a.cookies.clear()
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert provider.keys_fetched == 2


def test_keys_are_fetched_again_at_most_once_in_thirty_seconds(cluster, provider, clock):
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    provider.published = []  # the provider signs with a key it does not publish
    provider.rotate()
    provider.published = []
    clock.advance(31)
    for person in (EMILY, DANIEL):
        cluster.a.cookies.clear()
        assert _refused(_sign_in(cluster.a, provider, person)) in ("failed", "unavailable")
    assert provider.keys_fetched == 2  # one fetch again for both sign-ins, within 30 seconds


def test_a_provider_that_did_not_answer_is_not_asked_again_for_thirty_seconds(cluster, provider, clock):
    provider.issuer_named = "https://elsewhere.acme.test/realms/acme"  # its discovery document does not hold
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"
    asked = len(provider.requested)
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"
    assert len(provider.requested) == asked  # remembered on this instance
    provider.issuer_named = ISSUER
    clock.advance(31)
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    assert started.status_code == 303 and "sign-in=" not in started.headers["location"]


@pytest.mark.parametrize(
    ("methods", "how"), [(("client_secret_post", "client_secret_basic"), "post"), (("client_secret_basic",), "basic")]
)
def test_the_server_sends_the_pkce_verifier_and_the_secret_as_the_provider_takes_it(cluster, provider, methods, how):
    provider.methods = methods
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    sent = provider.token_requests[-1]
    assert len(sent["code_verifier"]) >= 43
    assert (sent.get("client_secret") == CLIENT_SECRET) is (how == "post")
    assert sent["authorization"].startswith("Basic ") is (how == "basic")


def test_the_client_secret_goes_to_the_issuer_it_was_saved_for_alone(cluster, provider, caplog):
    """M4: the issuer changed since the secret was saved: the secret is not used until it is saved again."""
    edition = cluster.server.edition
    edition.secrets.save("sso-client-secret", CLIENT_SECRET, bound=secret_bound("https://evil.example/realms/acme"))
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        assert _refused(_sign_in(cluster.a, provider, EMILY)) == "unavailable"
    assert provider.token_requests == [] and "saved for another issuer" in caplog.text
    assert CLIENT_SECRET not in caplog.text


def test_the_groups_of_the_next_sign_in_replace_the_last_ones_and_a_membership_by_hand_stays(cluster, provider):
    _sign_in(cluster.a, provider, EMILY)
    [emily] = _people(cluster)
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    cluster.people.add_member(platform.id, emily.id, actor=BY_HAND)
    cluster.a.cookies.clear()
    _sign_in(cluster.a, provider, {**EMILY, "groups": ["latet-users"]})
    assert [group.name for group in cluster.people.groups_of(emily.id)] == ["Platform"]


def test_a_sign_in_never_joins_a_group_scim_pushed_under_a_name_the_mapping_names(cluster, provider, caplog):
    """Task 7's fix round 1: a SCIM group of a name the mapping names (one made before the mapping named it: SCIM refuses
    the name since) is filled by whoever holds a SCIM token, and its role is no sign-in's to take: no sign-in joins it,
    the mapping's other groups still apply, and the log says so naming no group and no person."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    scims = cluster.people.add_group("Latet admins", source="scim", actor="scim:s_tests")
    cluster.people.grant(scims.id, Role.AUDITOR, actor=BY_HAND)
    cluster.people.set_members(scims.id, [daniel.id], source="scim", actor="scim:s_tests")
    with caplog.at_level(logging.WARNING, logger="latet.ee.people"):
        assert _signed_in(_sign_in(cluster.a, provider, EMILY))  # latet-admins among the ID token's groups
    emily = next(person for person in _people(cluster) if person.id != daniel.id)
    assert [group.name for group in cluster.people.groups_of(emily.id)] == ["Payments leads"]
    assert cluster.people.roles_of(emily.id) == {Role.USER} and cluster.people.members(scims.id) == [daniel.ref()]
    assert cluster.a.get(f"{API}/me").json()["edition"]["roles"] == ["user"]
    assert "a group SCIM pushed" in caplog.text
    for value in ("Latet admins", "latet-admins", "Emily", "emily", emily.id, scims.id, daniel.id):
        assert value not in caplog.text


def test_the_sign_in_page_learns_where_to_sign_in_only_while_single_sign_on_is_set_up(cluster, provider):
    assert cluster.a.get("/_latet/sso").json() == {"login": "/_latet/sso/login"}
    cluster.server.edition.settings_store.set_section("sso", None, actor=BY_HAND)
    settle(lambda: all(server.edition.settings().sso is None for server in cluster.servers))
    assert cluster.a.get("/_latet/sso").status_code == 404
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"


def test_the_tokens_of_the_persons_computers_keep_working_after_a_sign_in(cluster, provider):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    identities = cluster.server.identities
    _identity, token, _key = identities.enrol(identities.code(daniel.id), "laptop")
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL))
    assert identities.by_secret(token).id == daniel.id


def test_each_sign_in_is_an_event_with_the_persons_id_and_never_their_name(cluster, provider, monkeypatch):
    events = []
    monkeypatch.setattr(cluster.server.edition, "record", events.append)
    _sign_in(cluster.a, provider, EMILY)
    cluster.a.cookies.clear()
    _sign_in(cluster.a, provider, SAM)
    assert [(e["type"], e["outcome"], e.get("reason")) for e in events] == [
        ("sign-in", "signed-in", None),
        ("sign-in", "refused", "not-allowed"),
    ]
    assert events[0]["identity"] == _people(cluster)[0].id and events[1].get("identity") is None
    text = json.dumps(events)
    assert "Emily" not in text and "Sam" not in text and "acme.com" not in text


def test_what_waits_in_valkey_for_a_sign_in_is_sealed_for_its_own_purpose(cluster, provider):
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    query = parse_qs(urlsplit(started.headers["location"]).query)
    name = "sso:flow:" + hashlib.sha256(query["state"][0].encode()).hexdigest()
    stored = cluster.server.bus.get(name)
    assert stored is not None and query["nonce"][0].encode() not in stored
    with pytest.raises(Exception):  # noqa: B017, PT011 - the base sealer opens nothing of it (F15)
        cluster.server.master.sealer(b"sign-ins").open_json(stored, cluster.server.bus.key(name))
    assert cluster.server.master.sealer(b"sso-flow").open_json(stored, cluster.server.bus.key(name))["nonce"]
    # Gate 5: a sign-in begun over IPv6 is counted in its /64's set and its /48's, each named by a keyed hash.
    bus = cluster.server.bus
    assert _browser_at(cluster, "2001:db8:5:1::10").get("/_latet/sso/login", follow_redirects=False).status_code == 303
    names = list(bus.client.scan_iter(match=bus.key("*")))
    assert names and not any(b"2001:db8" in name or b"/48" in name or b"/64" in name for name in names)
    assert _waiting(cluster, "2001:db8:5:1::/64") == 1 and _waiting(cluster, "2001:db8:5::/48") == 1


def _flow_of(cluster, started):
    """The sign-in ``started`` began, as it waits in Valkey: its name and the flow opened."""
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    name = "sso:flow:" + hashlib.sha256(state.encode()).hexdigest()
    sealed = cluster.server.bus.get(name)
    return name, cluster.server.master.sealer(b"sso-flow").open_json(sealed, cluster.server.bus.key(name))


def test_one_48_lets_wait_at_most_its_own_across_its_64s(cluster, provider, monkeypatch, caplog):
    """Gate 5: a home's /56 holds 256 /64s, each letting FLOWS_FROM_ONE wait, past the server's FLOWS_WAITING. A /48
    lets FLOWS_FROM_WIDE wait across its /64s, weighed after its /64 and before the server's: past it 429 with
    retry-after, the /64's page of fixed words and no cookie, nothing stored and nothing of the server's count taken;
    another /48 still begins one, and once one of its own comes back, on any instance, it begins again."""
    monkeypatch.setattr(oidc, "FLOWS_FROM_ONE", 2)
    monkeypatch.setattr(oidc, "FLOWS_FROM_WIDE", 3)
    for paced in ("_FLOODED", "_FLOODED_WIDE", "_CROWDED"):  # said at once, and the module's own left unspent
        monkeypatch.setattr(oidc, paced, Paced())
    bus = cluster.server.bus
    browsers = [_browser_at(cluster, f"2001:db8:5:{number}::10") for number in (1, 2, 3)]
    started = [browser.get("/_latet/sso/login", follow_redirects=False) for browser in browsers]
    assert [response.status_code for response in started] == [303, 303, 303]
    waiting = _flows(bus)
    fourth = _browser_at(cluster, "2001:db8:5:4::10")
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        refused = fourth.get("/_latet/sso/login", follow_redirects=False)
    assert refused.status_code == 429 and 1 <= int(refused.headers["retry-after"]) <= int(oidc.FLOW_TTL)
    assert "Too many sign-ins begun from your address" in refused.text and "2001:db8" not in refused.text
    assert "/_latet/ui/?sign-in=unavailable" in refused.text and "set-cookie" not in refused.headers
    assert refused.headers["content-security-policy"] == "default-src 'none'"
    assert "from one /48 network" in caplog.text and "2001:db8" not in caplog.text
    assert _flows(bus) == waiting and _waiting(cluster) == 3  # the refused one took nothing
    assert _waiting(cluster, "2001:db8:5::/48") == 3 and _waiting(cluster, "2001:db8:5:4::/64") == 0
    assert _browser_at(cluster, "2001:db8:6::10").get("/_latet/sso/login", follow_redirects=False).status_code == 303
    back = TestClient(server_app(cluster.servers[1]), base_url=URL, client=("2001:db8:5:2::10", 50000))
    assert _refused(_came_back(back, started[1], error="access_denied")) == "not-allowed"  # through instance B
    assert _waiting(cluster, "2001:db8:5::/48") == 2 and _waiting(cluster, "2001:db8:5:2::/64") == 0
    assert fourth.get("/_latet/sso/login", follow_redirects=False).status_code == 303


def test_the_48_is_weighed_after_the_64_and_before_the_servers(cluster, provider, monkeypatch):
    """What one /64 is refused takes nothing of its /48's count; a /48 that is full is told so (429) before the server's
    count is weighed, full or not (503)."""
    monkeypatch.setattr(oidc, "FLOWS_FROM_ONE", 1)
    monkeypatch.setattr(oidc, "FLOWS_FROM_WIDE", 2)
    monkeypatch.setattr(oidc, "FLOWS_WAITING", 2)
    for paced in ("_FLOODED", "_FLOODED_WIDE", "_CROWDED"):  # the module's own, which later tests read, left unspent
        monkeypatch.setattr(oidc, paced, Paced())
    first, same = _browser_at(cluster, "2001:db8:7:1::10"), _browser_at(cluster, "2001:db8:7:1::11")
    assert first.get("/_latet/sso/login", follow_redirects=False).status_code == 303
    assert same.get("/_latet/sso/login", follow_redirects=False).status_code == 429  # its /64
    assert _waiting(cluster, "2001:db8:7::/48") == 1 and _waiting(cluster) == 1
    assert _browser_at(cluster, "2001:db8:7:2::10").get("/_latet/sso/login", follow_redirects=False).status_code == 303
    assert _browser_at(cluster, "2001:db8:7:3::10").get("/_latet/sso/login", follow_redirects=False).status_code == 429
    assert _browser_at(cluster, "198.51.100.31").get("/_latet/sso/login", follow_redirects=False).status_code == 503


def test_an_ipv4_address_has_no_48(cluster, provider):
    """IPv4, and IPv4 written as IPv6, is counted by its address alone: its flow names no /48."""
    for address, bucket in (("198.51.100.7", "198.51.100.7"), ("::ffff:198.51.100.8", "198.51.100.8")):
        _name, flow = _flow_of(cluster, _browser_at(cluster, address).get("/_latet/sso/login", follow_redirects=False))
        assert "wide" not in flow, address
        assert flow["counted"] == cluster.server.bus.rate_key(f"{oidc.FLOWS_FROM}:{bucket}")
    _name, flow = _flow_of(
        cluster, _browser_at(cluster, "2001:db8:5:1::10").get("/_latet/sso/login", follow_redirects=False)
    )
    assert flow["counted"] == cluster.server.bus.rate_key(f"{oidc.FLOWS_FROM}:2001:db8:5:1::/64")
    assert flow["wide"] == cluster.server.bus.rate_key(f"{oidc.FLOWS_FROM}:2001:db8:5::/48")


def test_a_flow_without_wide_gives_back_what_it_names(cluster, provider):
    """A sign-in an instance began before the tier waits with no ``wide``: its callback frees the server's entry and its
    /64's; the /48's, which a newer instance counted, ends with the flow. A ``wide`` that names no set of an address's
    frees nothing there."""
    bus = cluster.server.bus
    browser = _browser_at(cluster, "2001:db8:5:1::10")
    elsewhere = "rate:sso-waiting-elsewhere"  # a set that holds the flow, and that no address names
    for wide in (None, elsewhere):
        started = browser.get("/_latet/sso/login", follow_redirects=False)
        name, flow = _flow_of(cluster, started)
        older = {key: value for key, value in flow.items() if key != "wide"}
        if wide is not None:
            older["wide"] = wide
            bus.client.zadd(bus.key(elsewhere), {name.removeprefix("sso:flow:"): 1})
        sealed = cluster.server.master.sealer(b"sso-flow").seal_json(older, bus.key(name))
        bus.client.set(bus.key(name), sealed, keepttl=True)
        assert _refused(_came_back(browser, started, error="access_denied")) == "not-allowed"
        assert _waiting(cluster) == 0 and _waiting(cluster, "2001:db8:5:1::/64") == 0
    assert _waiting(cluster, "2001:db8:5::/48") == 2  # each ends with its flow
    assert bus.client.zcard(bus.key(elsewhere)) == 1  # never freed: not an address's


def test_too_many_sign_ins_begun_from_one_address_wait(cluster, provider, monkeypatch):
    monkeypatch.setattr(cluster.server.bus, "allow", lambda key, limit, per: False)
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"


def test_past_the_sign_ins_a_server_lets_wait_a_new_one_is_answered_503_and_stores_nothing(
    cluster, provider, monkeypatch, caplog
):
    """The final review's F6: each sign-in begun keeps a sealed flow in Valkey for ten minutes before anyone is known,
    and the limit by address multiplies with the addresses. Past FLOWS_WAITING on the whole server, on any instance, a
    new one is answered 503 with retry-after, in fixed words that send the browser to the sign-in page, and nothing is
    stored; once those waiting end unfinished, sign-ins begin again."""
    monkeypatch.setattr(oidc, "FLOWS_WAITING", 2)
    monkeypatch.setattr(oidc, "FLOW_TTL", 2.0)
    bus = cluster.server.bus

    def flows():
        return sorted(bus.client.scan_iter(match=bus.key("sso:flow:*")))

    for client in (cluster.a, cluster.b):
        assert client.get("/_latet/sso/login", follow_redirects=False).status_code == 303
    waiting = flows()
    assert len(waiting) == 2
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        for client in (cluster.a, cluster.b):  # whichever instance: the count is the server's
            busy = client.get("/_latet/sso/login", follow_redirects=False)
            assert busy.status_code == 503 and 1 <= int(busy.headers["retry-after"]) <= 2
            assert "Too many sign-ins are waiting" in busy.text and "/_latet/ui/?sign-in=unavailable" in busy.text
            assert busy.headers["cache-control"] == "no-store" and "set-cookie" not in busy.headers
    assert flows() == waiting
    assert bus.client.zcard(bus.key(bus.rate_key(oidc.FLOWS))) == 2  # the server's set: those waiting, no address
    assert "2 sign-ins are waiting at the provider" in caplog.text
    time.sleep(2.2)  # those waiting ended unfinished, as their flows
    assert cluster.a.get("/_latet/sso/login", follow_redirects=False).status_code == 303


def _flows(bus):
    """The sign-ins waiting in Valkey, by their names."""
    return sorted(bus.client.scan_iter(match=bus.key("sso:flow:*")))


def _browser_at(cluster, address):
    """Another browser, at ``address``, in front of instance A."""
    return TestClient(server_app(cluster.server), base_url=URL, client=(address, 50000))


def _waiting(cluster, address=None):
    """How many sign-ins Valkey counts waiting: begun from ``address``'s bucket, or on the whole server."""
    bus = cluster.server.bus
    return bus.client.zcard(bus.key(bus.rate_key(oidc.FLOWS if address is None else f"{oidc.FLOWS_FROM}:{address}")))


def _came_back(client, started, **query):
    """The provider sends the browser back with the state of the sign-in ``started`` began, and ``query``."""
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    return client.get(f"{oidc.CALLBACK}?{urlencode({'state': state, **query})}", follow_redirects=False)


def test_past_the_sign_ins_one_address_lets_wait_a_new_one_from_it_is_answered_429_and_stores_nothing(
    cluster, provider, monkeypatch, caplog
):
    """The final fix's re-review: the server's count of waiting sign-ins filled from a few addresses (each may begin
    LOGINS, 600 a minute), and every new company sign-in and Link my company account was refused. One address (an IPv4
    address, an IPv6 /64) lets FLOWS_FROM_ONE (100) wait unfinished, apart from LOGINS and before the server's count:
    past it, 429 with retry-after in fixed words, on either instance, its link too, nothing stored and nothing of the
    server's count taken; another address still begins one. Filling the server's count takes 200 addresses."""
    assert (oidc.FLOWS_FROM_ONE, oidc.FLOWS_WAITING, oidc.FLOW_TTL) == (100, 20_000, 600.0)
    monkeypatch.setattr(oidc, "_FLOODED", Paced())  # said at once, whatever another test said before
    bus = cluster.server.bus
    for _ in range(100):
        assert cluster.a.get("/_latet/sso/login", follow_redirects=False).status_code == 303
    waiting = _flows(bus)
    assert len(waiting) == 100
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        for client in (cluster.a, cluster.b):  # whichever instance: the count is the address's on the server
            refused = client.get("/_latet/sso/login", follow_redirects=False)
            assert refused.status_code == 429 and 1 <= int(refused.headers["retry-after"]) <= 600
            assert "Too many sign-ins begun from your address" in refused.text
            assert "/_latet/ui/?sign-in=unavailable" in refused.text and "203.0.113.7" not in refused.text
            assert refused.headers["cache-control"] == "no-store" and "set-cookie" not in refused.headers
            assert refused.headers["content-security-policy"] == "default-src 'none'"
        community_sign_in(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
        assert cluster.a.get(LINK, follow_redirects=False).status_code == 429  # Link my company account counts too
    assert _flows(bus) == waiting
    assert _waiting(cluster) == 100 and _waiting(cluster, "203.0.113.7") == 100  # the refused took nothing
    assert "more than 100 sign-ins begun from one address" in caplog.text and "203.0.113.7" not in caplog.text
    assert not any(b"203.0.113.7" in name for name in bus.client.scan_iter(match=bus.key("*")))
    assert _browser_at(cluster, "198.51.100.20").get("/_latet/sso/login", follow_redirects=False).status_code == 303
    assert len(_flows(bus)) == 101


def test_an_ipv6_address_counts_with_its_64_and_begins_again_once_its_sign_in_left_unfinished_ends(
    cluster, provider, monkeypatch
):
    """The limits' buckets (an IPv6 address's /64); a sign-in left unfinished stops counting when its flow ends."""
    monkeypatch.setattr(oidc, "FLOWS_FROM_ONE", 1)
    monkeypatch.setattr(oidc, "FLOW_TTL", 2.0)
    monkeypatch.setattr(oidc, "_FLOODED", Paced())
    first, same, other = (
        _browser_at(cluster, address) for address in ("2001:db8:5:6::10", "2001:db8:5:6:ffff::99", "2001:db8:5:7::10")
    )
    assert first.get("/_latet/sso/login", follow_redirects=False).status_code == 303
    assert same.get("/_latet/sso/login", follow_redirects=False).status_code == 429  # the same /64
    assert other.get("/_latet/sso/login", follow_redirects=False).status_code == 303  # another /64
    time.sleep(2.2)  # the /64's sign-in, left unfinished, ended
    assert same.get("/_latet/sso/login", follow_redirects=False).status_code == 303


def test_the_servers_count_still_holds_past_addresses_each_within_its_own(cluster, provider, monkeypatch):
    """FLOWS_WAITING stays the backstop: addresses each within FLOWS_FROM_ONE still meet it (503, nothing stored);
    a sign-in that comes back leaves the server's count too."""
    monkeypatch.setattr(oidc, "FLOWS_FROM_ONE", 1)
    monkeypatch.setattr(oidc, "FLOWS_WAITING", 2)
    monkeypatch.setattr(oidc, "_CROWDED", Paced())
    browsers = [_browser_at(cluster, f"198.51.100.{number}") for number in (1, 2, 3)]
    started = [browser.get("/_latet/sso/login", follow_redirects=False) for browser in browsers]
    assert [response.status_code for response in started] == [303, 303, 503]
    assert len(_flows(cluster.server.bus)) == 2 and _waiting(cluster) == 2
    assert _refused(_came_back(browsers[0], started[0], error="access_denied")) == "not-allowed"
    assert _waiting(cluster) == 1 and _waiting(cluster, "198.51.100.1") == 0
    assert browsers[2].get("/_latet/sso/login", follow_redirects=False).status_code == 303


def test_sign_ins_that_come_back_wait_no_more_and_a_replayed_callback_gives_nothing_back(cluster, provider):
    """The coordinator's ruling: an address's count is of the sign-ins it began that still wait, so that an office
    behind one NAT is refused only by many left unfinished at once, while abandoned ones still fill their own address's
    alone. A sign-in its callback takes, signed in or refused, leaves its address's count and the server's; a callback
    that takes nothing (replayed, or with a state never begun) gives nothing back, and no count goes below nothing."""
    for number in range(150):  # one address, each back within the flow's life: all begin
        started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
        assert started.status_code == 303, number
        if number % 30 == 0:  # some sign in
            back = provider.authorize(started.headers["location"], EMILY)
            assert _signed_in(cluster.a.get(back, follow_redirects=False))
        else:  # the others are refused at the provider
            assert _refused(_came_back(cluster.a, started, error="access_denied")) == "not-allowed"
    assert (_waiting(cluster), _waiting(cluster, "203.0.113.7"), _flows(cluster.server.bus)) == (0, 0, [])
    waiting = [cluster.a.get("/_latet/sso/login", follow_redirects=False) for _ in range(100)]
    assert all(started.status_code == 303 for started in waiting)
    assert cluster.a.get("/_latet/sso/login", follow_redirects=False).status_code == 429  # 100 left waiting
    assert _refused(_came_back(cluster.a, waiting[0], error="access_denied")) == "not-allowed"
    assert _waiting(cluster, "203.0.113.7") == 99
    assert _refused(_came_back(cluster.a, waiting[0], error="access_denied")) == "expired"  # replayed
    assert _refused(cluster.b.get(f"{oidc.CALLBACK}?state=never-begun", follow_redirects=False)) == "expired"
    assert (_waiting(cluster), _waiting(cluster, "203.0.113.7")) == (99, 99)  # nothing given back twice
    assert cluster.a.get("/_latet/sso/login", follow_redirects=False).status_code == 303  # the room one made
    assert cluster.a.get("/_latet/sso/login", follow_redirects=False).status_code == 429  # and that one alone


def test_an_account_made_at_a_first_sign_in_is_in_the_administration_log_by_the_person(cluster, provider):
    _sign_in(cluster.a, provider, EMILY)
    [emily] = _people(cluster)
    [entry] = cluster.server.audit.query(action="person.added")
    assert (entry.actor, entry.target, entry.details) == (emily.id, emily.id, {"source": "oidc"})


def test_a_provider_address_egress_refuses_is_never_reached_and_the_log_says_why(
    cluster, provider, monkeypatch, caplog
):
    """F11: the provider's name pointed at the cloud's metadata address: no request leaves, the sign-in page says
    unavailable, and the server's log says why by the error's type."""
    for server in cluster.servers:
        server.edition.oidc.http = None  # the server's own way: PinnedTransport
    monkeypatch.setattr(egress, "resolve", lambda host, port: [ipaddress.ip_address("169.254.169.254")])
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"
    assert "ConnectError" in caplog.text and provider.requested == []


def test_the_license_is_weighed_again_in_the_sign_ins_own_transaction(cluster, provider, monkeypatch):
    """F18: a trial that ends while the sign-in is at the provider makes no account."""
    licensing = cluster.server.edition.licensing
    weighed = licensing.standing_in

    def ended(conn):
        standing = weighed(conn)
        return replace(standing, state=State.TRIAL_ENDED)

    monkeypatch.setattr(licensing, "standing_in", ended)
    assert _refused(_sign_in(cluster.a, provider, EMILY)) == "trial-ended"
    assert _people(cluster) == []


def test_googles_hosted_domain_decides_who_signs_in(cluster, provider):
    sso = SsoSettings.model_validate(
        {
            "issuer": "https://accounts.google.com",
            "client_id": "latet",
            "accounts": "open",
            "allowed_domains": ["acme.com"],
        }
    )
    oidc = cluster.server.edition.oidc
    for hd in (None, "evil.example", ["acme.com"]):
        claims = {"sub": "1", "iss": sso.issuer, **({"hd": hd} if hd is not None else {})}
        with pytest.raises(SignInError) as caught:
            oidc._person(sso, sso.issuer, claims, "203.0.113.7", None)
        assert caught.value.code == "not-allowed"
    assert oidc._person(sso, sso.issuer, {"sub": "1", "hd": "ACME.com"}, "203.0.113.7", None).source == "oidc"


def test_a_trial_that_ends_stops_single_sign_on_and_nobody_is_locked_out(cluster, provider):
    """R3 and R5 (Review Focus 7): the sessions open stay, roles still decide, sign-in with SSO stops, and the Community
    sign-in still opens one's own dashboard."""
    on_trial(cluster)  # its last day, 18:00 UTC
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    assert _signed_in(_sign_in(cluster.a, provider, EMILY))
    cluster.clock.now = at("2026-12-02", "23:55:00")
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)  # begun five minutes before the end
    back = provider.authorize(started.headers["location"], EMILY)
    end_trial(cluster)
    for client in (cluster.a, cluster.b):  # her own dashboard's session: it lasts its twelve hours, on every instance
        edition = client.get(f"{API}/me").json()["edition"]
        assert edition["license"]["state"] == "trial-ended" and edition["roles"] == ["admin", "user"]
    assert _refused(cluster.b.get(back, follow_redirects=False)) == "trial-ended"
    assert cluster.a.get("/_latet/sso").status_code == 404  # the sign-in page shows the terminal's way alone
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "trial-ended"
    [emily] = _people(cluster)
    community_sign_in(cluster, emily)  # `latet dashboard`'s one-time link, bound to her computer's token
    assert cluster.a.get(f"{API}/me").status_code == 200 and cluster.b.get(f"{API}/me").status_code == 200
    assert _session(cluster) == (emily.id, "code")
    refused = cluster.a.post(f"{API}/ee/sign-in/code", headers=UI)
    assert refused.status_code in (403, 404)  # a code to connect a computer comes from an administrator now


def test_a_paused_license_keeps_single_sign_on_and_the_roles(cluster, provider):
    """R4: past a paid license's grace, nobody is kept out."""
    admins = cluster.people.add_group("Latet admins", actor=BY_HAND)
    cluster.people.grant(admins.id, Role.ADMIN, actor=BY_HAND)
    paused(cluster)
    assert cluster.server.edition.licensing.standing().state.value == "paused"
    assert cluster.a.get("/_latet/sso").json() == {"login": "/_latet/sso/login"}
    assert _signed_in(_sign_in(cluster.a, provider, EMILY, back=cluster.b))
    assert cluster.b.get(f"{API}/me").json()["edition"]["roles"] == ["admin", "user"]


def test_a_license_without_single_sign_on_offers_no_company_sign_in(cluster, provider):
    license(cluster, features=["locked-rules", "roles"])
    assert cluster.a.get("/_latet/sso").status_code == 404
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"


def test_a_license_file_that_cannot_be_used_stops_single_sign_on_and_roles_still_decide(cluster, provider):
    """R4: E1's serving is off while the file cannot be used; licensed answers from the features verified (M1)."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    licenses = cluster.server.edition._licenses  # E1's DatabaseLicenses: a document no key verifies, its features kept
    licenses.write(b'{"payload": {}}', frozenset(FEATURES))
    for server in cluster.servers:
        server.edition.refresh()
    assert cluster.server.edition.licensing.standing().state.value == "invalid"
    assert cluster.a.get("/_latet/sso").status_code == 404
    assert _refused(cluster.a.get("/_latet/sso/login", follow_redirects=False)) == "unavailable"
    assert cluster.people.roles_of(emily.id) == {Role.USER, Role.ADMIN}
    assert cluster.server.edition.access.allows(emily.id, "people.manage")


def test_a_sign_in_begun_before_the_issuer_changed_signs_nobody_in(cluster, provider):
    started = cluster.a.get("/_latet/sso/login", follow_redirects=False)
    back = provider.authorize(started.headers["location"], EMILY)
    set_up_sign_in(cluster, {**SSO, "issuer": "https://login2.acme.test/realms/acme"})
    assert _refused(cluster.a.get(back, follow_redirects=False)) == "failed"
    assert provider.token_requests == [] and _people(cluster) == []


def test_someone_who_left_the_access_group_is_refused_and_keeps_their_account(cluster, provider):
    _sign_in(cluster.a, provider, EMILY)
    cluster.a.cookies.clear()
    assert _refused(_sign_in(cluster.a, provider, {**EMILY, "groups": ["sales"]})) == "not-allowed"
    [emily] = _people(cluster)
    assert not emily.disabled  # a typo in access_group must not disable everyone


def test_an_account_deleted_at_the_provider_since_it_was_found_is_not_claimed(cluster, provider, monkeypatch):
    """Task 2's carry: what ``unclaimed`` found is checked again with the account's row held (``link(claim=)``)."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    cluster.people.link(daniel.id, deleted=True)  # meanwhile: deleted at the provider
    monkeypatch.setattr(cluster.people, "unclaimed", lambda user_name, scim_only: daniel.id)  # as read just before
    assert _refused(_sign_in(cluster.a, provider, DANIEL)) == "failed"
    assert cluster.people.account(daniel.id).subject is None


def test_with_accounts_from_scim_a_provisioned_person_signs_in_and_keeps_scims_groups(cluster, provider):
    """Review round 1: SCIM's people sign in, and the token's groups never change their memberships."""
    set_up_sign_in(cluster, {**SSO, "accounts": "scim"})
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", scim=True, actor=BY_HAND)
    token = {**DANIEL, "groups": ["latet-users", "latet-admins", "payments-leads"]}
    assert _signed_in(_sign_in(cluster.a, provider, token))
    assert _session(cluster) == (daniel.id, "sso")
    assert cluster.people.groups_of(daniel.id) == [] and cluster.people.group("Latet admins") is None
    cluster.a.cookies.clear()  # found by the subject from now on
    assert _signed_in(_sign_in(cluster.a, provider, token))
    assert cluster.people.groups_of(daniel.id) == []


@pytest.mark.parametrize("via", ["code", "sso"])
def test_linking_keeps_the_kind_of_the_session_that_began_it(cluster, provider, via):
    """Review round 1: **Link my company account** begun from a one-time code's session opens no sso session (it would
    pass ``company_changes: sso``); begun from an sso session, it stays one."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    sid = cluster.server.sign_ins.open(daniel.id, via=via)
    cluster.a.cookies.set(COOKIE, sid, domain="latet.acme.lan", path="/_latet")
    assert _signed_in(_sign_in(cluster.a, provider, DANIEL, start="/_latet/sso/link"))
    assert cluster.people.account(daniel.id).subject == f"{ISSUER} 1c2b-daniel"
    assert _session(cluster) == (daniel.id, via)


def test_the_client_secret_of_one_realm_is_not_another_realms_on_the_same_host(cluster, provider):
    """Review round 1: the secret is bound to the whole issuer (Keycloak realms, Entra tenants share an origin)."""
    assert secret_bound(ISSUER) == secret_bound(ISSUER + "/") != secret_bound(ISSUER.replace("acme", "other"))
    other_realm = "https://login.acme.test/realms/other"
    cluster.server.edition.secrets.save("sso-client-secret", CLIENT_SECRET, bound=secret_bound(other_realm))
    assert _refused(_sign_in(cluster.a, provider, EMILY)) == "unavailable"
    assert provider.token_requests == []
