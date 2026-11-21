"""The Sign-in page's routes, and the same for `latet server sign-in` (ee/sign_in_api.py)."""

import asyncio
import contextlib
import json
import logging
import math
import threading

import pytest
from psycopg import sql
from starlette.requests import Request

from latet.api.rest import COOKIE
from latet.ee import sign_in_api
from latet.ee.access import ADMINISTRATION
from latet.ee.oidc import secret_bound
from latet.ee.people import Changing
from latet.ee.roles import Role
from latet.ee.routes import Refused, Who
from latet.logline import Paced

from .fake_provider import CLIENT_SECRET, ISSUER, SSO, FakeProvider
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
    license,
    on_trial,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS

DAY = 86400.0
SERVER_API = "/_latet/server/v1"  # the computers' calls
COMPANY_SIGN_IN = {
    "error": "sign in with your company account to change what applies to everyone",
    "code": "company-sign-in",
}
OTHER_ISSUER = "https://login.acme.test/realms/other"


def _session(cluster, identity, via):
    """The dashboard session of ``identity``, opened as ``via`` says: `latet dashboard`'s one-time code (``code``), or
    the company's single sign-on (``sso``), which a change that applies to everyone takes once single sign-on is set up
    (decision (c))."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(identity.id, via=via), domain=HOST, path="/_latet")


def _set_up(cluster, **changes):
    """Single sign-on set up as an administrator sets it up by hand, known to every instance."""
    cluster.server.edition.settings_store.set_section("sso", {**SSO, **changes}, actor=BY_HAND)
    settle(lambda: all(server.edition.settings().sso is not None for server in cluster.servers))


def _admin(cluster, method, path, body=None):
    """`latet server sign-in …`'s call: the administration API, with the server's administration token."""
    return cluster.a.request(method, f"{ADMIN_EE}{path}", headers=as_admin(cluster), json=body)


def _me(cluster):
    return cluster.a.get(f"{API}/me").json()["edition"]


@pytest.fixture
def emily(cluster):
    """Emily Carter, administrator, signed in with the company's single sign-on."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    _session(cluster, emily, "sso")
    return emily


def test_the_settings_are_checked_known_to_every_instance_and_the_secret_never_comes_back(cluster, emily):
    wrong = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "issuer": "http://login.acme.com"}})
    assert wrong.status_code == 400 and wrong.json()["details"]
    view = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": SSO}).json()
    assert view["sso"]["issuer"] == ISSUER and view["secret"] is False
    assert "company_changes" not in view["sso"] and view["company_changes"] == "sso"  # set by `latet server` alone
    assert view["redirect_uri"] == f"{URL}/_latet/sso/callback"
    assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": CLIENT_SECRET}).json() == {"ok": True}
    other = cluster.servers[1].edition
    bound = secret_bound(ISSUER)  # kept for the issuer it was saved for (M4)
    settle(lambda: other.settings().sso is not None and other.secrets.load("sso-client-secret", bound=bound))
    view = cluster.b.get(f"{API}/ee/sign-in").json()
    assert view["secret"] is True and CLIENT_SECRET not in json.dumps(view)
    actions = {entry.action for entry in cluster.server.audit.query(actor=emily.id)}
    assert {"sign-in.changed", "sign-in.secret.changed"} <= actions
    assert CLIENT_SECRET not in json.dumps([entry.details for entry in cluster.server.audit.query()])
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={}).status_code == 400  # never read as "off"


def test_a_new_group_mapping_applies_to_everyone_at_once(cluster, emily):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.apply_claims(daniel.id, ["security-team"], {})
    sso = {**SSO, "groups": {"security-team": "Security team"}}
    cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": sso})
    assert [group.name for group in cluster.people.groups_of(daniel.id)] == ["Security team"]


GOOGLE_SSO = {
    **SSO,
    "issuer": "https://accounts.google.com",
    "accounts": "open",
    "access_group": None,
    "allowed_domains": ["acme.com"],
}


def _regroups(cluster, monkeypatch):
    """What instance A's People is asked: each regroup of everyone, and each write of memberships from sign-ins."""
    people, asked = cluster.people, []
    regroup, written = people.regroup, people._oidc_members

    def regrouped(*args, **kwargs):
        asked.append("regroup")
        return regroup(*args, **kwargs)

    def wrote(*args, **kwargs):
        asked.append("members")
        return written(*args, **kwargs)

    monkeypatch.setattr(people, "regroup", regrouped)
    monkeypatch.setattr(people, "_oidc_members", wrote)
    return asked


def test_a_save_regroups_everyone_only_when_the_mapping_changes_or_single_sign_on_turns_on_or_off(
    cluster, emily, monkeypatch
):
    """The final review's Important: every save regrouped every account, the last administrator's lock and the
    settings' table lock held meanwhile (SCIM and every keeping change waiting behind it); a change of allowed_domains
    or of company_changes alone regroups no one now, and a new mapping still regroups everyone."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.apply_claims(daniel.id, ["security-team"], {})
    cluster.people.apply_claims(priya.id, ["security-team", "payments-leads"], {})
    assert _admin(cluster, "PUT", "/sign-in", {"sso": GOOGLE_SSO}).status_code == 200  # on: its mapping applied
    assert [group.name for group in cluster.people.groups_of(priya.id)] == ["Payments leads"]
    asked = _regroups(cluster, monkeypatch)
    wider = {**GOOGLE_SSO, "allowed_domains": ["acme.com", "acme.co.uk"]}
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": wider}).status_code == 200
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**wider, "company_changes": "any-session"}}).status_code == 200
    assert asked == []
    mapped = {**wider, "groups": {"security-team": "Security team"}}
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": mapped}).status_code == 200
    assert asked == ["regroup", "members"]  # everyone at once
    security = cluster.people.group("Security team")
    assert {ref.id for ref in cluster.people.members(security.id)} == {daniel.id, priya.id}
    assert [group.name for group in cluster.people.groups_of(priya.id)] == ["Security team"]
    assert _admin(cluster, "PUT", "/sign-in", {"sso": None}).status_code == 200  # off: no group from sign-ins stays
    assert asked == ["regroup", "members", "regroup", "members"]
    assert cluster.people.members(security.id) == []


def test_the_test_says_whether_the_provider_answers(cluster, emily, clock, caplog):
    caplog.set_level(logging.DEBUG)
    provider = FakeProvider(clock)
    cluster.server.edition.oidc.http = provider.client
    answer = cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={"sso": SSO}).json()
    assert answer["ok"] and answer["issuer"] == ISSUER and answer["keys"] == 1
    provider.issuer_named = "https://login.acme.test/realms/Acme"
    answer = cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={"sso": SSO}).json()
    assert not answer["ok"] and "realms/Acme" in answer["error"]  # the provider's words, to an administrator alone
    assert "realms/Acme" not in caplog.text  # never in a log line (L3)
    nothing = cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={})
    assert nothing.status_code == 409  # nothing set up to test
    _set_up(cluster)
    provider.issuer_named = ISSUER
    assert cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={}).json()["ok"]  # the settings saved


def test_a_test_of_the_provider_is_in_the_administration_log_by_its_outcome_alone(cluster, emily, clock, monkeypatch):
    """The final review's M2 (code): the dashboard's test has the server reach the issuer a body names, as the
    administration API's does; each is recorded ``sign-in.tested``, by whom and from where, with its outcome, never the
    issuer, an address or the provider's words. A test the log does not take is not told."""
    provider = FakeProvider(clock)
    cluster.server.edition.oidc.http = provider.client
    assert cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={"sso": SSO}).json()["ok"]
    provider.issuer_named = "https://login.acme.test/realms/Acme"
    assert not cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={"sso": SSO}).json()["ok"]
    assert _admin(cluster, "POST", "/sign-in/test", {"sso": SSO}).json()["ok"] is False
    entries = sorted(cluster.server.audit.query(action="sign-in.tested"), key=lambda entry: entry.id)
    assert [(entry.actor, entry.target, entry.details) for entry in entries[:2]] == [
        (emily.id, None, {"outcome": "ok"}),
        (emily.id, None, {"outcome": "unavailable"}),
    ]
    assert entries[0].address == "203.0.113.7"
    assert entries[2].actor.startswith("admin:") and entries[2].details["outcome"] == "unavailable"  # and its status
    said = json.dumps([entry.details for entry in entries])
    assert all(value not in said for value in ("acme", "realms", "login", "https"))
    record = cluster.server.audit.record

    def refusing(actor, action, *args, **kwargs):
        if action == "sign-in.tested":
            raise RuntimeError("the administration log at 10.0.0.5 does not take it")
        return record(actor, action, *args, **kwargs)

    monkeypatch.setattr(cluster.server.audit, "record", refusing)
    failed = cluster.a.post(f"{API}/ee/sign-in/test", headers=UI, json={"sso": SSO})
    assert failed.status_code == 500
    assert all(value not in failed.text for value in ("issuer", "keys", "endpoint", "10.0.0.5"))


def test_scim_tokens_are_shown_once_kept_as_keyed_hashes_end_and_are_removed_one_by_one(cluster, emily):
    first = cluster.a.post(f"{API}/ee/sign-in/scim-tokens", headers=UI)
    assert first.status_code == 201 and first.json()["token"].startswith("latet_scim_")
    assert first.json()["url"] == f"{URL}/scim/v2" and first.json()["expires"] is not None  # 90 days by default
    assert first.json()["expires"] == cluster.clock() + 90 * DAY
    cluster.clock.advance(1)
    # M3: a token that never ends is `latet server sign-in scim-token --never`'s alone.
    never = cluster.a.post(f"{API}/ee/sign-in/scim-tokens", headers=UI, json={"days": None})
    assert never.status_code == 400 and "--never" in never.json()["error"]
    for days in (0, -1, 3651, True, "30", 1.5):
        assert cluster.a.post(f"{API}/ee/sign-in/scim-tokens", headers=UI, json={"days": days}).status_code == 400
    second = _admin(cluster, "POST", "/sign-in/scim-tokens", {"days": None})
    assert second.status_code == 201 and second.json()["expires"] is None  # never
    cluster.clock.advance(1)
    third = cluster.a.post(f"{API}/ee/sign-in/scim-tokens", headers=UI, json={"days": 7}).json()
    assert third["expires"] == cluster.clock() + 7 * DAY
    tokens = cluster.a.get(f"{API}/ee/sign-in").json()["scim"]["tokens"]
    assert [token["id"] for token in tokens] == [first.json()["id"], second.json()["id"], third["id"]]
    # O3: each by its maker, never the token.
    emily_made = {"kind": "person", "id": emily.id, "name": "Emily Carter", "admin": True, "disabled": False}
    assert tokens[0]["maker"] == emily_made
    assert tokens[1]["maker"] == {"kind": "administration", "id": tokens[1]["made_by"].removeprefix("admin:")}
    shown = json.dumps(tokens)
    assert all(made["token"] not in shown for made in (first.json(), second.json(), third))
    assert first.json()["token"] not in json.dumps([entry.details for entry in cluster.server.audit.query()])
    assert cluster.server.audit.query(action="scim.token.made")[0].actor == emily.id
    cluster.a.delete(f"{API}/ee/sign-in/scim-tokens/{first.json()['id']}", headers=UI)
    left = cluster.a.get(f"{API}/ee/sign-in").json()["scim"]["tokens"]
    assert [token["id"] for token in left] == [second.json()["id"], third["id"]]
    assert cluster.server.edition.scim_tokens.check(first.json()["token"]) is None
    gone = cluster.a.delete(f"{API}/ee/sign-in/scim-tokens/{first.json()['id']}", headers=UI)
    assert gone.status_code == 404 and first.json()["id"] not in gone.text  # its words quote no path
    assert cluster.a.delete(f"{API}/ee/sign-in/scim-tokens/not-a-token", headers=UI).status_code == 404


def test_a_scim_tokens_maker_who_no_longer_administers_is_said_on_the_page(cluster, emily):
    """O3: a token of someone demoted or disabled still serves (it is the provider's credential), and the page says
    whose it is."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.ADMIN)
    cluster.server.edition.scim_tokens.make(made_by=daniel.id)
    cluster.people.revoke(daniel.id, Role.ADMIN, actor=BY_HAND)
    [token] = cluster.a.get(f"{API}/ee/sign-in").json()["scim"]["tokens"]
    demoted = {"kind": "person", "id": daniel.id, "name": "Daniel Okafor", "admin": False, "disabled": False}
    assert token["maker"] == demoted
    cluster.server.identities.disable(daniel.id)
    [token] = cluster.a.get(f"{API}/ee/sign-in").json()["scim"]["tokens"]
    assert token["maker"] == {**demoted, "disabled": True}


def test_who_made_a_scim_token_is_told_to_whoever_may_edit_the_sign_in_alone(cluster):
    """Fix round 1: while the license has sso without roles everyone reads the Sign-in page; a token's maker (their
    name, id, whether they administer) is then the administration's alone."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    made, _secret = cluster.server.edition.scim_tokens.make(made_by=emily.id)
    license(cluster, features=["locked-rules", "sso"])
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com"))
    [token] = cluster.a.get(f"{API}/ee/sign-in").json()["scim"]["tokens"]
    assert set(token) == {"id", "created", "expires", "last_used"} and token["id"] == made.id
    assert emily.id not in cluster.a.get(f"{API}/ee/sign-in").text
    [told] = _admin(cluster, "GET", "/sign-in").json()["scim"]["tokens"]
    assert told["maker"]["id"] == emily.id and told["made_by"] == emily.id


def test_the_sign_in_names_only_secrets_of_its_own(cluster, emily):
    """Fix round 1: client_secret names a secret beginning sso-, so that the page's secret routes save and remove
    nothing of another part's (a SIEM's token)."""
    cluster.server.edition.secrets.save("siem-token", "kept-for-the-siem")
    for wrong in ({**SSO, "client_secret": "siem-token"}, {**SSO, "client_secret": "ssoclient"}):
        refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": wrong})
        assert refused.status_code == 400 and "client_secret" in json.dumps(refused.json()["details"])
        assert _admin(cluster, "PUT", "/sign-in", {"sso": wrong}).status_code == 400
    named = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "client_secret": "sso-okta"}})
    assert named.status_code == 200
    assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": CLIENT_SECRET}).status_code == 200
    secrets = cluster.server.edition.secrets
    assert secrets.load("sso-okta", bound=secret_bound(ISSUER)) == CLIENT_SECRET
    assert cluster.a.delete(f"{API}/ee/sign-in/secret", headers=UI).status_code == 200
    assert secrets.load("siem-token") == "kept-for-the-siem"


def test_anyone_signed_in_makes_a_code_to_connect_a_computer_once_single_sign_on_is_set_up(cluster):
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    assert cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).status_code == 404
    _set_up(cluster)
    answer = cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).json()
    # E0c's shape (F14): the command names the server, and the code is shown apart, out of a shell's history.
    assert answer["command"] == f"latet connect {URL}" and answer["code"] not in answer["command"]
    [entry] = cluster.server.audit.query(action="person.code.made")  # L6: for oneself too
    assert (entry.actor, entry.target) == (daniel.id, daniel.id) and answer["code"] not in repr(entry)
    assert cluster.server.identities.enrol(answer["code"], "laptop") is not None


def test_a_session_a_one_time_code_opened_makes_no_code_while_single_sign_on_decides(cluster):
    """L10: a twelve-hour session that `latet dashboard`'s code opened (an agent at the computer can ask for one) mints
    no lasting computer token while the server asks for the company's sign-in (O1)."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _set_up(cluster)
    _session(cluster, daniel, "code")
    refused = cluster.a.post(f"{API}/ee/sign-in/code", headers=UI)
    assert refused.status_code == 403 and refused.json() == COMPANY_SIGN_IN
    assert cluster.server.audit.query(action="person.code.made") == []
    _set_up(cluster, company_changes="any-session")
    settle(lambda: all(s.edition.settings().sso.company_changes == "any-session" for s in cluster.servers))
    assert cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).status_code == 200


def test_an_api_key_makes_no_code_to_connect_a_computer(cluster):
    """Task 9's review: the gate refuses a key only inside a key's space; the server opens no dashboard session for a
    key, and the route refuses anything but a person should one ever come."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    _set_up(cluster)
    _session(cluster, key, "sso")
    assert cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).status_code == 401  # the server's own gate

    async def run(work, *args):
        return work(*args)

    async def change(work):
        raise AssertionError("nothing is made for an API key")

    for who in (Who(key.id, key.id, None, run, change), Who(ADMINISTRATION, "admin:tests", None, run, change)):
        with pytest.raises(Refused) as refused:
            asyncio.run(cluster.server.edition.sign_in_api.connect_code(_POST, who))
        assert refused.value.status == 403
    assert cluster.server.audit.query(action="person.code.made") == []


def _changing(cluster, actor):
    """``Who.change`` as the dashboard's routes run it, but for the license and the session, which these tests leave
    aside: the work in one transaction of E0b's identities, its entries written last."""

    async def change(work):
        with cluster.server.identities.changing() as changes:
            within = Changing(changes, actor, None)
            result = work(within)
            within.write(cluster.server.audit)
        return result

    return change


async def _run_now(work, *args):
    return work(*args)


_POST = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})


def test_a_code_is_weighed_again_in_its_transaction(cluster, monkeypatch):
    """What this instance read before (its copy of the settings, the person's identity) is read again in the change's
    transaction: single sign-on turned off meanwhile makes no code, nor does a person disabled meanwhile."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    api = cluster.server.edition.sign_in_api
    monkeypatch.setattr(api.oidc, "ready", lambda: True)  # this instance's copy says it is set up: it is not
    who = Who(daniel.id, daniel.id, None, _run_now, _changing(cluster, daniel.id))
    with pytest.raises(Refused) as refused:
        asyncio.run(api.connect_code(_POST, who))
    assert refused.value.status == 404
    _set_up(cluster)
    monkeypatch.setattr(cluster.server.identities, "current", lambda identity_id: daniel)  # read before the disable
    cluster.server.identities.disable(daniel.id)
    with pytest.raises(Refused) as refused:
        asyncio.run(api.connect_code(_POST, who))
    assert refused.value.status == 409
    assert cluster.server.audit.query(action="person.code.made") == []


def test_someone_without_the_admin_role_reads_no_sign_in_settings(cluster):
    sign_in_as(cluster, add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com", Role.SECURITY))
    assert cluster.a.get(f"{API}/ee/sign-in").status_code == 403
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None}).status_code == 403


def test_once_a_trial_ended_the_sign_in_settings_are_read_and_nothing_changes(cluster):
    on_trial(cluster)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    cluster.server.edition.settings_store.set_section("sso", SSO, actor=BY_HAND)
    end_trial(cluster)
    _session(cluster, emily, "sso")
    assert cluster.a.get(f"{API}/ee/sign-in").json()["sso"]["issuer"] == ISSUER  # as it stood at the end
    for refused in (
        cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None}),
        cluster.a.post(f"{API}/ee/sign-in/scim-tokens", headers=UI),
        cluster.a.post(f"{API}/ee/sign-in/code", headers=UI),
        _admin(cluster, "PUT", "/sign-in", {"sso": None}),
        _admin(cluster, "POST", "/sign-in/scim-tokens", {"days": None}),
    ):
        assert refused.status_code == 403 and refused.json()["code"] == "trial-ended"
    assert _admin(cluster, "GET", "/sign-in").status_code == 200
    assert cluster.server.edition.settings().sso is not None


def test_the_generic_settings_route_leaves_the_sign_in_to_its_own(cluster):
    """F9: the sso section is set through the sign-in's routes alone (what they check, record and apply)."""
    for refused in (_admin(cluster, "PUT", "/settings/sso", SSO), _admin(cluster, "DELETE", "/settings/sso")):
        assert refused.status_code == 409 and "latet server sign-in set" in refused.json()["error"]
    assert cluster.server.edition.settings().sso is None


@pytest.mark.parametrize(
    "issuer",
    [
        "https://169.254.169.254/realms/acme",  # the cloud's metadata address
        "https://[fe80::1]/realms/acme",
        "https://[::ffff:10.0.0.7]/realms/acme",  # an IPv6 form that carries an IPv4 address
        "https://0.0.0.0/realms/acme",
        "https://127.1/realms/acme",  # numbers a resolver reads as this computer
        "https://keycloak.localhost/realms/acme",
    ],
)
def test_an_issuer_at_an_address_latet_never_reaches_is_refused_when_saved(cluster, emily, issuer):
    """F15: a literal address that egress refuses (``inside``: the company's network allowed) is said at once; a name
    is judged when the server connects."""
    refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "issuer": issuer}})
    assert refused.status_code == 400 and issuer not in refused.text
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "issuer": issuer}}).status_code == 400
    assert cluster.server.edition.settings().sso is None


def test_an_issuer_on_the_company_network_or_named_on_this_computer_is_kept(cluster, emily):
    for issuer in ("https://10.20.0.5/realms/acme", "http://127.0.0.1:8080/realms/acme", "http://localhost:8080/r"):
        saved = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "issuer": issuer}})
        assert saved.status_code == 200, saved.text


def test_who_gets_an_account_is_said_in_full(cluster, emily):
    """M2: ``accounts`` is written out; a group needs its access group; Google its Workspace domains."""
    without = {key: value for key, value in SSO.items() if key != "accounts"}
    google = {**SSO, "issuer": "https://accounts.google.com", "accounts": "open", "access_group": None}
    for sso in (without, {**SSO, "access_group": None}, google):
        refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": sso})
        assert refused.status_code == 400 and refused.json()["details"]
    kept = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**google, "allowed_domains": ["acme.com"]}})
    assert kept.status_code == 200 and kept.json()["sso"]["allowed_domains"] == ["acme.com"]


def test_whether_company_wide_changes_need_the_company_sign_in_is_set_by_latet_server_alone(cluster, emily):
    """Decision (c), O1: the dashboard keeps the stored setting and refuses a body that names it; while it asks for the
    company's sign-in, single sign-on is turned off by `latet server` alone; the administration sets it, recorded."""
    named = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "company_changes": "any-session"}})
    assert named.status_code == 400 and "latet server sign-in set" in named.json()["error"]
    assert cluster.server.edition.settings().sso is None
    set_by_token = _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "company_changes": "any-session"}})
    assert set_by_token.status_code == 200 and set_by_token.json()["company_changes"] == "any-session"
    [entry] = cluster.server.audit.query(action="sign-in.changed")
    assert entry.actor.startswith("admin:") and entry.details["company_changes"] == "any-session"
    view = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "client_id": "latet-2"}}).json()
    assert view["company_changes"] == "any-session"  # kept as stored
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "client_id": "latet-3"}}).json()["company_changes"] == (
        "any-session"  # a body that names none keeps it there too
    )
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "company_changes": "sso"}}).status_code == 200
    off = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None})
    assert off.status_code == 409 and "latet server sign-in off" in off.json()["error"]
    assert cluster.server.edition.settings().sso is not None
    turned_off = _admin(cluster, "PUT", "/sign-in", {"sso": None})
    assert (
        turned_off.status_code == 200
        and turned_off.json()["sso"] is None
        and turned_off.json()["company_changes"] is None
    )
    assert cluster.server.edition.settings().sso is None


def test_the_page_turns_single_sign_on_off_when_any_session_may_change_what_applies_to_everyone(cluster, emily):
    """While nobody signed in and no account waits for its first sign-in (Daniel, added by hand, is linked to no
    company account), from `latet dashboard`'s session too."""
    _set_up(cluster, company_changes="any-session")
    add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, emily, "code")
    off = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None})
    assert off.status_code == 200 and cluster.server.edition.settings().sso is None


def test_the_page_never_turns_single_sign_on_off_once_someone_signed_in_or_an_account_waits(cluster, emily):
    """The final fix's re-review: turned off from the page and then on, single sign-on took a new issuer or user-name
    claim past their guards, which weigh a change between two settings. Whatever company_changes says and whichever
    session asks, the page turns it off only while nobody signed in and no account waits for its first sign-in;
    `latet server sign-in off` always does, recorded."""
    _set_up(cluster, company_changes="any-session")
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # waits for her sign-in
    for via in ("sso", "code"):
        _session(cluster, emily, via)
        refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None})
        assert refused.status_code == 409 and "latet server sign-in off" in refused.json()["error"]
    cluster.people.link(priya.id, subject=f"{ISSUER} 2d4f-priya", claim="priya.shah@acme.com")  # she signed in
    assert cluster.people.unclaimed("priya.shah@acme.com", scim_only=False) is None  # no account waits now
    refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": None})
    assert refused.status_code == 409 and "latet server sign-in off" in refused.json()["error"]
    assert cluster.server.edition.settings().sso is not None
    assert cluster.server.audit.query(action="sign-in.changed") == []
    turned_off = _admin(cluster, "PUT", "/sign-in", {"sso": None})
    assert turned_off.status_code == 200 and turned_off.json()["sso"] is None
    assert cluster.server.edition.settings().sso is None
    [entry] = cluster.server.audit.query(action="sign-in.changed")
    assert entry.actor.startswith("admin:") and entry.details["company_changes"] is None


def test_a_new_issuer_needs_the_secret_saved_again_and_is_latet_servers_once_people_signed_in(cluster, emily):
    """M4: the secret saved for one issuer is never sent to another's token endpoint; once someone signed in, a new
    issuer is `latet server sign-in set`'s alone (their accounts are then unlinked and linked again)."""
    _set_up(cluster)
    assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": CLIENT_SECRET}).status_code == 200
    moved = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "issuer": OTHER_ISSUER}}).json()
    assert moved["secret"] is False and moved["secret_issuer_changed"] is True
    [entry, *_] = cluster.server.audit.query(action="sign-in.changed")
    assert entry.details["issuer_changed"] is True and OTHER_ISSUER not in json.dumps(entry.details)
    back = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": SSO}).json()
    assert back["secret"] is True and back["secret_issuer_changed"] is False  # sealed for this one: it opens again
    cluster.people.link(emily.id, subject=f"{ISSUER} 5d1e-emily", actor=BY_HAND)
    refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "issuer": OTHER_ISSUER}})
    assert refused.status_code == 409 and "latet server sign-in set" in refused.json()["error"]
    assert cluster.server.edition.settings().sso.issuer == ISSUER
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "client_id": "x"}}).status_code == 200
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "issuer": OTHER_ISSUER}}).status_code == 200


def test_a_new_user_name_claim_is_latet_servers_once_someone_signed_in_or_an_account_waits_for_a_first_sign_in(
    cluster, emily
):
    """The final review's F5: pointed at a claim people set themselves (`name`, `nickname`), the user-name claim hands
    an account waiting for its first sign-in to whoever sets theirs to its user name, an administrator's own company
    account included. It is changed as the issuer is: from the page while nobody is linked, by `latet server sign-in
    set` once someone signed in or an account waits; `sign-in.changed` names the fields, never their values."""
    _set_up(cluster)
    nickname = {**SSO, "user_name_claim": "nickname"}
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": nickname}).status_code == 200  # nobody yet
    priya = add_person(cluster, "Priya Shah", "priya.shah@acme.com")
    cluster.people.link_person(priya.id, user_name="priya.shah@acme.com", actor=BY_HAND)  # waits for a first sign-in
    refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": SSO})
    assert refused.status_code == 409 and "latet server sign-in set" in refused.json()["error"]
    assert "nickname" not in refused.text and "preferred_username" not in refused.text
    cluster.people.link(priya.id, subject=f"{ISSUER} 2d4f-priya", claim="priya.shah@acme.com")  # she signed in
    assert cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": SSO}).status_code == 409
    assert cluster.server.edition.settings().sso.user_name_claim == "nickname"
    other = {**nickname, "client_id": "latet-2"}
    assert (
        cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": other}).status_code == 200
    )  # the rest: the page's
    assert _admin(cluster, "PUT", "/sign-in", {"sso": SSO}).status_code == 200
    assert cluster.server.edition.settings().sso.user_name_claim == "preferred_username"
    entries = sorted(cluster.server.audit.query(action="sign-in.changed"), key=lambda entry: entry.id)
    assert [entry.details["fields"] for entry in entries] == [
        ["user_name_claim"],
        ["client_id"],
        ["client_id", "user_name_claim"],
    ]
    assert entries[-1].actor.startswith("admin:")
    said = json.dumps([entry.details for entry in entries])
    assert all(value not in said for value in ("nickname", "preferred_username", "latet-2"))


def test_a_mapping_that_would_leave_no_administrator_is_refused_from_the_page(cluster):
    """L5: Emily administers through the group her last sign-in gave her; a mapping without it, from the page, would
    leave nobody; `latet server` may."""
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    _set_up(cluster)
    cluster.people.apply_claims(emily.id, ["latet-admins"], SSO["groups"])
    cluster.people.grant(cluster.people.group("Latet admins").id, Role.ADMIN, actor=BY_HAND)
    _session(cluster, emily, "sso")
    refused = cluster.a.put(f"{API}/ee/sign-in", headers=UI, json={"sso": {**SSO, "groups": {}}})
    assert refused.status_code == 409 and Role.ADMIN in cluster.people.roles_of(emily.id)
    assert cluster.server.edition.settings().sso.groups == SSO["groups"]
    assert _admin(cluster, "PUT", "/sign-in", {"sso": {**SSO, "groups": {}}}).status_code == 200
    assert Role.ADMIN not in cluster.people.roles_of(emily.id)


def test_the_secret_is_one_line_kept_for_an_issuer_and_forgotten(cluster, emily):
    assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": CLIENT_SECRET}).status_code == 409
    _set_up(cluster)
    for secret in ("", "  ", "two\nlines", "a\x1bb", 7, None, "x" * 4097):
        assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": secret}).status_code == 400
    assert cluster.a.put(f"{API}/ee/sign-in/secret", headers=UI, json={"secret": CLIENT_SECRET}).status_code == 200
    secrets = cluster.server.edition.secrets
    assert secrets.load("sso-client-secret", bound=secret_bound(ISSUER)) == CLIENT_SECRET
    assert cluster.a.delete(f"{API}/ee/sign-in/secret", headers=UI).json() == {"ok": True}
    assert secrets.load("sso-client-secret", bound=secret_bound(ISSUER)) is None
    assert cluster.a.delete(f"{API}/ee/sign-in/secret", headers=UI).status_code == 404
    saved = [entry.details for entry in cluster.server.audit.query(action="sign-in.secret.changed")]
    assert saved == [{"saved": False}, {"saved": True}]


def test_the_sign_in_line_of_the_checks_says_what_is_set_up_without_asking_the_provider(cluster):
    edition = cluster.server.edition

    def line():
        [found] = [check for check in edition.checks() if check.title == "Sign-in"]
        return found

    assert [check for check in edition.checks() if check.title == "Sign-in"] == []  # nothing set up: no line
    edition.settings_store.set_section("sso", SSO, actor=BY_HAND)
    assert line().status == "warn" and "client secret is not saved" in line().detail
    edition.secrets.save("sso-client-secret", CLIENT_SECRET, bound=secret_bound(ISSUER))
    edition.scim_tokens.make(made_by=BY_HAND)
    assert line().status == "ok" and ISSUER in line().detail and "SCIM provisioning on (1 token)" in line().detail
    assert "company sign-in" in line().detail  # decision (c): what the dashboard's company-wide changes take


def test_the_sign_in_line_warns_of_open_accounts_of_a_last_scim_token_ending_and_of_a_mapping_scim_holds(cluster):
    """M2 and Task 7's carry: what lets in more people than meant, or stops letting anyone in, is a warning."""
    edition = cluster.server.edition

    def line():
        [found] = [check for check in edition.checks() if check.title == "Sign-in"]
        return found

    edition.secrets.save("sso-client-secret", CLIENT_SECRET, bound=secret_bound(ISSUER))
    edition.settings_store.set_section("sso", {**SSO, "accounts": "open", "access_group": None}, actor=BY_HAND)
    assert line().status == "warn" and "accounts: open" in line().detail
    edition.settings_store.set_section("sso", {**SSO, "accounts": "scim", "access_group": None}, actor=BY_HAND)
    assert line().status == "warn" and "no SCIM token" in line().detail
    edition.scim_tokens.make(made_by=BY_HAND, days=30)
    assert line().status == "ok"
    cluster.clock.advance(17 * DAY)
    assert line().status == "warn" and "last SCIM token ends" in line().detail
    edition.scim_tokens.make(made_by=BY_HAND, days=None)
    assert line().status == "ok"
    by_hand = cluster.people.add_group("Latet admins", actor=BY_HAND)  # made by hand: a sign-in joins it
    assert line().status == "ok"
    cluster.people.remove_group(by_hand.id, actor=BY_HAND)
    cluster.people.add_group("Latet admins", source="scim", external_id="g1", actor="scim:s_tests")
    assert line().status == "warn" and "Latet admins" in line().detail  # the mapping's own words, no provider's
    edition.settings_store.set_section("sso", None, actor=BY_HAND)
    assert "any administrator" in line().detail  # SCIM alone: no single sign-on decides who changes what


def test_the_sign_in_line_says_its_settings_could_not_be_read_and_never_raises(cluster, monkeypatch):
    """F5, R17: `latet server status` answers whatever the store does."""
    edition = cluster.server.edition
    edition.settings_store.set_section("sso", SSO, actor=BY_HAND)

    def unread(**kwargs):
        raise OSError("connection to 10.0.0.9 refused")

    monkeypatch.setattr(edition.scim_tokens, "all", unread)
    [found] = [check for check in edition.checks() if check.title == "Sign-in"]
    assert found.status == "warn" and "OSError" in found.detail and "10.0.0.9" not in found.detail


def _connect(cluster, code, label):
    """`latet connect` on a computer: the code spent, the computer's token made and recorded (E0c's token.added)."""
    answer = cluster.a.post(f"{SERVER_API}/enrol", json={"code": code, "label": label})
    assert answer.status_code == 200, answer.text
    return answer.json()["token"], answer.json()["computer_key"]


def _dashboard_from(cluster, computer):
    """`latet dashboard` on that computer: a one-time link bound to its token, opened in the browser (a `code`
    session, which records the computer it was opened from)."""
    token, key = computer
    login = cluster.a.post(f"{SERVER_API}/login-code", headers={"x-latet-token": token, "x-latet-computer-key": key})
    cluster.a.cookies.clear()
    opened = cluster.a.get(f"/_latet/ui/login?code={login.json()['code']}", follow_redirects=False)
    assert opened.status_code == 303 and "login=" not in opened.headers["location"]


def _code_for(cluster, person):
    """A one-time code an administrator makes for ``person`` with `latet server` (E3's person.code.made)."""
    return _admin(cluster, "POST", f"/people/{person.id}/code").json()["code"]


def _seen(cluster, until, client=None):
    return (client or cluster.a).post(f"{API}/ee/notices/told", headers=UI, json={"until": until})


def test_me_tells_of_computers_and_codes_until_the_person_says_they_saw_them_and_writes_nothing(cluster, emily):
    """O6 (M4, L10), fix rounds 1 and 2: /me lists, read alone; a probe that drops what it read, a HEAD, another
    instance tell nobody anything away; the dashboard of a session the company sign-in opened says it seen once it is
    on screen."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    assert "connected" not in _me(cluster)  # nothing yet
    cluster.clock.advance(60)
    code = _code_for(cluster, daniel)  # an administrator's
    cluster.clock.advance(60)
    _connect(cluster, code, "laptop")
    for _ in range(2):
        assert cluster.a.head(f"{API}/me").status_code == 200
        connected = _me(cluster)["connected"]
        assert connected["computers"] == [{"label": "laptop", "connected": cluster.clock()}]
        assert connected["codes"] == [{"made": cluster.clock() - 60, "by_administrator": True}]
        assert cluster.b.get(f"{API}/me").json()["edition"]["connected"] == connected
    assert code not in json.dumps(connected)
    told = _seen(cluster, connected["until"])
    assert told.status_code == 200 and told.json() == {"ok": True, "told": True}
    assert "connected" not in _me(cluster) and "connected" not in cluster.b.get(f"{API}/me").json()["edition"]
    assert _seen(cluster, connected["until"]).json() == {"ok": True, "told": False}  # once
    _set_up(cluster)
    cluster.clock.advance(60)
    own = cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).json()
    cluster.clock.advance(60)
    _connect(cluster, own["code"], "desktop")
    connected = cluster.b.get(f"{API}/me").json()["edition"]["connected"]
    assert [c["label"] for c in connected["computers"]] == ["desktop"]
    assert [c["by_administrator"] for c in connected["codes"]] == [False]  # his own


def test_an_entry_committed_after_a_read_is_still_told_whatever_its_time(cluster, emily):
    """The order is the administration log's (its ids, one entry at a time to its commit), never an instance's clock:
    an entry stamped before the one a visit read, committed after it, is still told."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    _code_for(cluster, daniel)
    read = _me(cluster)["connected"]
    cluster.clock.advance(-3600)  # an instance whose clock is an hour behind
    _code_for(cluster, daniel)
    assert _seen(cluster, read["until"]).json()["told"] is True  # what the visit read
    later = _me(cluster)["connected"]
    assert later["codes"] == [{"made": cluster.clock(), "by_administrator": True}] and later["until"] > read["until"]


def test_what_is_said_seen_is_one_of_the_persons_own_entries(cluster, emily):
    """Nothing still to come is said seen ahead, nor another person's entries."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _code_for(cluster, emily)  # Emily's entry
    _session(cluster, daniel, "sso")
    _code_for(cluster, daniel)
    until = _me(cluster)["connected"]["until"]
    for wrong in (until + 1000, until - 1, 0, -5, "1", None, True, 2**70):
        refused = _seen(cluster, wrong)
        assert refused.status_code == 400 and str(wrong) not in refused.json()["error"], wrong
    assert _me(cluster)["connected"]["until"] == until  # still told


def test_only_a_session_the_company_sign_in_opened_says_it_seen_and_no_computer_hides_anything(cluster):
    """Fix round 2, M4's own threat: an administrator makes two codes for Daniel and connects computers A and B as him,
    then opens `latet dashboard` on each. Whatever ``until`` either session names, neither says anything seen
    (without single sign-on and with it); only a session Daniel's company sign-in opened does."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    a = _connect(cluster, _code_for(cluster, daniel), "computer A")
    cluster.clock.advance(60)
    b = _connect(cluster, _code_for(cluster, daniel), "computer B")
    _session(cluster, daniel, "sso")
    told = _me(cluster)["connected"]
    assert [c["label"] for c in told["computers"]] == ["computer A", "computer B"] and len(told["codes"]) == 2
    entries = [entry.id for entry in cluster.server.audit.query(action="token.added")]  # newest first: B's, A's
    untils = [*entries, *(entry.id for entry in cluster.server.audit.query(action="person.code.made"))]
    for set_up in (False, True):
        if set_up:
            _set_up(cluster, company_changes="any-session")  # the route's own refusal, past Task 8's rule
            settle(lambda: all(s.edition.settings().sso.company_changes == "any-session" for s in cluster.servers))
        for computer in (b, a):
            _dashboard_from(cluster, computer)
            for until in untils:
                refused = _seen(cluster, until)
                assert refused.status_code == 403 and "company sign-in" in refused.json()["error"], (set_up, until)
        sign_in_as(cluster, daniel)  # `latet dashboard`'s code of any computer
        assert _seen(cluster, told["until"]).status_code == 403
        assert _me(cluster)["connected"] == told  # nothing hidden
    _set_up(cluster)  # company_changes: sso
    _dashboard_from(cluster, a)
    assert _seen(cluster, told["until"]).json() == COMPANY_SIGN_IN  # Task 8's rule, before the body is read
    _session(cluster, daniel, "sso")
    assert _seen(cluster, told["until"]).json() == {"ok": True, "told": True}
    assert "connected" not in _me(cluster)


def test_an_entry_is_told_fourteen_days_unless_said_seen_sooner(cluster):
    """Without single sign-on nobody can say it seen: it is told on every read of every session of the person for 14
    days after it happened, then goes."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    computer = _connect(cluster, _code_for(cluster, daniel), "laptop")
    cluster.clock.advance(14 * DAY - 60)
    for opened in (lambda: _dashboard_from(cluster, computer), lambda: sign_in_as(cluster, daniel)):
        opened()
        for client in (cluster.a, cluster.b):
            assert client.head(f"{API}/me").status_code == 200
            connected = client.get(f"{API}/me").json()["edition"]["connected"]
            assert [c["label"] for c in connected["computers"]] == ["laptop"] and len(connected["codes"]) == 1
    cluster.clock.advance(120)  # 14 days and a minute after both
    sign_in_as(cluster, daniel)
    assert "connected" not in _me(cluster)


def test_every_computer_and_every_administrators_code_is_told_however_many_codes_the_person_made(cluster, monkeypatch):
    """Fix round 3: only the codes the person made themselves are told the newest 40 (``more``: how many more): 45 of
    them, made from a session at a computer of theirs, push out neither the computer connected nor the administrator's
    code; said seen, none of them is told again. (Gate 2's limit is raised here: 45 codes in a minute.)"""
    monkeypatch.setattr(sign_in_api, "CONNECT_CODES", 50)
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    made = cluster.clock()
    _connect(cluster, _code_for(cluster, daniel), "laptop")  # an administrator's code, then the computer
    _set_up(cluster, company_changes="any-session")
    _session(cluster, daniel, "code")
    own = []
    for _ in range(45):
        cluster.clock.advance(1)
        own.append(cluster.clock())
        assert cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).status_code == 200
    for client in (cluster.a, cluster.b):
        connected = client.get(f"{API}/me").json()["edition"]["connected"]
        assert connected["computers"] == [{"label": "laptop", "connected": made}]
        assert connected["codes"] == [
            {"made": made, "by_administrator": True},
            *({"made": at, "by_administrator": False} for at in own[5:]),
        ]
        assert connected["more"] == 5
    _session(cluster, daniel, "sso")
    assert _seen(cluster, connected["until"]).json() == {"ok": True, "told": True}
    assert "connected" not in _me(cluster)
    cluster.clock.advance(60)  # one more of his own: told 14 days, as every entry is
    assert cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).status_code == 200
    assert _me(cluster)["connected"]["codes"] == [{"made": cluster.clock(), "by_administrator": False}]
    assert _me(cluster)["connected"]["more"] == 0
    cluster.clock.advance(14 * DAY + 60)
    _session(cluster, daniel, "sso")
    assert "connected" not in _me(cluster)


class _Meanwhile:
    """Instance A's connection, but right after the first statement that reads the notice (of any connection: one
    ``pending`` list for all), what waits in ``pending`` runs to its end: entries committed elsewhere at that moment."""

    def __init__(self, conn, pending):
        self._conn, self._pending = conn, pending

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, query, *args, **kwargs):
        answer = self._conn.execute(query, *args, **kwargs)
        if self._pending and "FROM audit a" in str(query):
            self._pending.pop()()
        return answer


def test_a_visit_reads_the_notice_at_one_moment_and_saying_it_seen_hides_nothing_committed_meanwhile(
    cluster, monkeypatch
):
    """Fix round 3: what /me tells is read in one statement. An administrator's code and then one of Daniel's own,
    committed on instance B while his visit reads on A, are not in what it lists, and saying that seen leaves both to
    tell (read in two, the second would see his own code past the administrator's, and its ``until`` cover both)."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _code_for(cluster, daniel)
    _session(cluster, daniel, "sso")
    other = cluster.servers[1]

    def meanwhile():
        other.audit.record(BY_HAND, "person.code.made", daniel.id)  # an administrator's
        other.audit.record(daniel.id, "person.code.made", daniel.id)  # then one of his own

    pending = [meanwhile]
    connection = cluster.server.db.connection

    @contextlib.contextmanager
    def watched(*args, **kwargs):
        with connection(*args, **kwargs) as conn:
            yield _Meanwhile(conn, pending)

    monkeypatch.setattr(cluster.server.db, "connection", watched)
    read = _me(cluster)["connected"]
    monkeypatch.setattr(cluster.server.db, "connection", connection)
    assert pending == []  # it ran, during the read
    assert read["codes"] == [{"made": cluster.clock(), "by_administrator": True}]
    assert _seen(cluster, read["until"]).json() == {"ok": True, "told": True}
    later = _me(cluster)["connected"]
    assert [code["by_administrator"] for code in later["codes"]] == [True, False] and later["until"] > read["until"]


def test_two_at_once_say_it_seen_once(cluster, emily):
    """Another visit says it seen meanwhile (its transaction not committed yet, so that this one found nothing to hold):
    this one waits for it, and finds it said."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    _code_for(cluster, daniel)
    until = _me(cluster)["connected"]["until"]
    answer: dict = {}
    with cluster.server.db.transaction() as other:
        other.execute("INSERT INTO notified (identity_id, until) VALUES (%s, %s)", (daniel.id, until))
        thread = threading.Thread(target=lambda: answer.update(_seen(cluster, until).json()))
        thread.start()

        def waiting():  # this one's row waits for the other's
            with cluster.server.db.connection() as look:
                row = look.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                    "AND query LIKE 'INSERT INTO notified%%'"
                ).fetchone()
            return bool(row and row[0])

        settle(waiting)
    thread.join(10)
    assert answer == {"ok": True, "told": False}
    assert _seen(cluster, until).json() == {"ok": True, "told": False}  # and after it: nothing more


def test_the_notice_is_told_while_roles_or_sso_serve_and_said_seen_while_sso_does(cluster, emily):
    """Fix round 2: with sso alone (one's own Sign-in page's code serves) it is told and an SSO session says it seen;
    with roles alone it is told, and no session single sign-on opens says it seen; with neither, nothing is told."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _code_for(cluster, daniel)
    _session(cluster, daniel, "sso")
    until = _me(cluster)["connected"]["until"]
    license(cluster, features=["locked-rules"])
    assert "connected" not in _me(cluster)
    assert _seen(cluster, until).status_code == 403
    license(cluster, features=["locked-rules", "roles"])
    assert _me(cluster)["connected"]["until"] == until
    assert _seen(cluster, until).status_code == 403  # sso does not serve: single sign-on opens no session
    license(cluster, features=["locked-rules", "sso"])
    assert _me(cluster)["connected"]["until"] == until
    assert _seen(cluster, until).json() == {"ok": True, "told": True}
    assert "connected" not in _me(cluster)


def test_me_lists_the_computers_connected_to_ones_account_for_good_and_who_made_their_codes(cluster, emily):
    """The final review's F4: the notice goes after 14 days, seen or not; /me lists the computers connected to the
    person's account always: each one's label, when it was connected, when it was last used, and whether the code it
    was connected with was made by an administrator. Their own and nobody else's, never a token, a hash or a code; while
    roles or sso serve, as the notice."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    assert _me(cluster)["computers"] == []  # none yet: listed empty
    cluster.clock.advance(60)
    laptop = _connect(cluster, _code_for(cluster, daniel), "laptop")  # with a code an administrator made
    _set_up(cluster)
    cluster.clock.advance(60)
    own = cluster.a.post(f"{API}/ee/sign-in/code", headers=UI).json()["code"]  # one he made himself
    cluster.clock.advance(60)
    desktop = _connect(cluster, own, "desktop")
    computers = _me(cluster)["computers"]
    assert [(c["label"], c["connected"], c["by_administrator"]) for c in computers] == [
        ("desktop", cluster.clock(), False),
        ("laptop", cluster.clock() - 120, True),
    ]
    assert all(c["last_used"] is None or isinstance(c["last_used"], float) for c in computers)
    assert set(computers[0]) == {"label", "connected", "last_used", "by_administrator"}
    said = json.dumps(computers)
    assert all(secret not in said for secret in (*laptop, *desktop, own))
    cluster.clock.advance(15 * DAY)  # the notice has gone by itself; the list stays
    _session(cluster, daniel, "sso")  # the session before lasted 12 hours
    assert "connected" not in _me(cluster) and len(_me(cluster)["computers"]) == 2
    assert cluster.b.get(f"{API}/me").json()["edition"]["computers"] == _me(cluster)["computers"]
    _session(cluster, emily, "sso")
    assert _me(cluster)["computers"] == []  # Emily's own: none of Daniel's
    license(cluster, features=["locked-rules"])
    assert "computers" not in _me(cluster)  # neither roles nor sso serve


def test_a_computer_connected_with_the_code_made_with_a_person_is_said_connected_with_an_administrators(cluster, emily):
    """The code shown when an administrator adds someone (person.added) is theirs too."""
    made = cluster.a.post(f"{API}/ee/people", headers=UI, json={"name": "Tom Becker", "email": "tom.becker@acme.com"})
    _connect(cluster, made.json()["code"], "tom-laptop")
    [computer] = cluster.server.edition.sign_in_api.computers(made.json()["person"]["id"])
    assert (computer["label"], computer["by_administrator"]) == ("tom-laptop", True)


def test_me_answers_whatever_the_computers_store_does(cluster, emily, monkeypatch, caplog):
    """R17: a list that cannot be read is left out of /me, said by its type alone."""

    def unread(identity_id):
        raise OSError("connection to 10.0.0.9 refused")

    monkeypatch.setattr(cluster.server.edition.sign_in_api, "computers", unread)
    with caplog.at_level(logging.WARNING, logger="latet.ee.edition"):
        answer = cluster.a.get(f"{API}/me")
    assert answer.status_code == 200 and "computers" not in answer.json()["edition"]
    assert "OSError" in caplog.text and "10.0.0.9" not in caplog.text


def test_me_says_how_the_session_was_opened_whichever_instance_answers(cluster, emily):
    """Task 12: the dashboard offers the notice's acknowledgement only in a session the company sign-in opened
    (``/me.session.via``); the route decides again (403 to any other, tested above)."""
    for client in (cluster.a, cluster.b, cluster.b):  # found by the lookup, then held in memory
        assert client.get(f"{API}/me").json()["session"] == {"via": "sso"}
    _session(cluster, emily, "code")
    for client in (cluster.a, cluster.b, cluster.b):
        assert client.get(f"{API}/me").json()["session"] == {"via": "code"}
    sign_in_as(cluster, emily)  # a session of `latet dashboard`'s one-time code, as the server opens one
    assert cluster.b.get(f"{API}/me").json()["session"] == {"via": "code"}


def test_me_answers_whatever_the_notices_store_does(cluster, emily, monkeypatch):
    """R17: /me never raises for what E3 adds to it."""

    def unread(identity_id):
        raise OSError("connection to 10.0.0.9 refused")

    monkeypatch.setattr(cluster.server.edition.sign_in_api, "connected", unread)
    answer = cluster.a.get(f"{API}/me")
    assert answer.status_code == 200 and "connected" not in answer.json()["edition"]


def test_me_says_whether_the_persons_own_account_is_linked_to_the_company_sign_in(cluster, emily):
    """H1, fix round 1: ``/me.edition.linked`` is the person's own, true once their account holds the subject of their
    company sign-ins, whichever instance answers: the dashboard offers **Link my company account** to a session of
    `latet dashboard`'s code only when it says ``false`` (and does not read the People list for it)."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "code")
    for client in (cluster.a, cluster.b):
        assert client.get(f"{API}/me").json()["edition"]["linked"] is False  # added by hand: no account yet
    cluster.people.link_person(daniel.id, user_name="daniel.okafor@acme.com", actor=BY_HAND)
    assert _me(cluster)["linked"] is False  # a user name finds them at a first sign-in: it is no sign-in yet
    cluster.people.link_person(daniel.id, subject=f"{ISSUER} 7a1f-daniel", actor=BY_HAND)
    for client in (cluster.a, cluster.b):
        assert client.get(f"{API}/me").json()["edition"]["linked"] is True
    assert cluster.a.head(f"{API}/me").status_code == 200
    assert "7a1f" not in json.dumps(_me(cluster))  # never what links them
    _session(cluster, emily, "sso")
    assert _me(cluster)["linked"] is False  # Emily's own, not Daniel's
    cluster.people.unlink(daniel.id, actor=BY_HAND)
    _session(cluster, daniel, "code")
    assert _me(cluster)["linked"] is False  # taken off again by an administrator (the issuer changed)


def test_me_says_nothing_of_a_link_to_an_api_key(cluster, emily):
    """An API key has no account: working in its space, /me names no link (and the person's own still does)."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    cluster.people.add_controller(key.id, emily.id, actor=BY_HAND)
    assert "linked" not in cluster.server.edition.me(key.ref())
    through = cluster.a.get(f"{API}/ee/keys/{key.id}/space/me")
    assert through.status_code == 200 and "linked" not in through.json()["edition"]
    assert _me(cluster)["linked"] is False


def test_me_answers_and_says_nothing_of_a_link_it_cannot_read(cluster, emily, monkeypatch, caplog):
    """R17: People's database not answering is told by its type, once, and /me still answers, without ``linked``: the
    dashboard then offers nothing it cannot be sure of."""

    def unread(identity_id):
        raise OSError("connection to 10.0.0.9 refused")

    monkeypatch.setattr(cluster.people, "account", unread)
    with caplog.at_level(logging.WARNING, logger="latet.ee.edition"):
        answer = cluster.a.get(f"{API}/me")
        cluster.a.get(f"{API}/me")
    assert answer.status_code == 200 and "linked" not in answer.json()["edition"]
    assert caplog.text.count("OSError") == 1 and "10.0.0.9" not in caplog.text  # said once a minute, by its type


# --- the end-of-plan wave -----------------------------------------------------------------------------------------------

CHANGED_SINCE = {
    "error": "not changed: the sign-in settings were changed since they were read here: read them again, then change "
    "them",
    "code": "sign-in-changed",
}


def test_a_change_made_from_settings_read_before_another_one_is_refused_and_changes_nothing(cluster):
    """`latet server sign-in set` with options alone sends the settings it read, changed, and their etag: a change made
    meanwhile, on any instance, is never overwritten unseen (409 in fixed words). The view's etag is the settings' as
    PostgreSQL has them, whichever instance answers; a body without one is taken as before."""
    assert _admin(cluster, "GET", "/sign-in").json()["etag"] is None  # nothing set up
    _set_up(cluster)
    read = _admin(cluster, "GET", "/sign-in").json()
    assert read["etag"] == cluster.b.get(f"{ADMIN_EE}/sign-in", headers=as_admin(cluster)).json()["etag"]
    listener, cluster.server.edition._listener = cluster.server.edition._listener, None
    listener.stop()  # instance A no longer hears ee:settings: its copy stays as it was
    cluster.servers[1].edition.settings_store.set_section("sso", {**SSO, "accounts": "scim"}, actor=BY_HAND)
    for stale in (read["etag"], None, "0" * 64):
        refused = _admin(cluster, "PUT", "/sign-in", {"sso": {**read["sso"], "client_id": "other"}, "etag": stale})
        assert refused.status_code == 409 and refused.json() == CHANGED_SINCE, refused.text
    for wrong in (7, "", "e" * 129, ["x"]):
        refused = _admin(cluster, "PUT", "/sign-in", {"sso": read["sso"], "etag": wrong})
        assert refused.status_code == 400 and "etag" in refused.json()["error"], wrong
    assert cluster.server.edition.settings_store.load().sso.client_id == SSO["client_id"]  # nothing changed
    again = _admin(cluster, "GET", "/sign-in").json()
    assert again["sso"]["accounts"] == "scim" and again["etag"] != read["etag"]
    done = _admin(cluster, "PUT", "/sign-in", {"sso": {**again["sso"], "client_id": "other"}, "etag": again["etag"]})
    assert done.status_code == 200 and done.json()["sso"]["client_id"] == "other", done.text
    assert done.json()["etag"] == _admin(cluster, "GET", "/sign-in").json()["etag"]
    assert _admin(cluster, "PUT", "/sign-in", {"sso": SSO}).status_code == 200  # no etag: as before
    first = _admin(cluster, "PUT", "/sign-in", {"sso": None, "etag": None})  # off, from settings set up meanwhile
    assert first.status_code == 409 and cluster.server.edition.settings_store.load().sso is not None


# --- E3's release gates 1 and 2: the computers one person connects, and the codes they make ----------------------------


def _own_code(client):
    """**Make a code** on the Sign-in page, from the session the browser holds."""
    return client.post(f"{API}/ee/sign-in/code", headers=UI)


def _codes_held(cluster, person):
    """How many one-time codes the database holds for ``person``."""
    with cluster.server.db.connection() as conn:
        return conn.execute("SELECT count(*) FROM codes WHERE identity_id = %s", (person.id,)).fetchone()[0]


def _computers(cluster, person):
    return len(cluster.server.identities.tokens(person.id))


def _own_codes_made(cluster, person):
    """The codes ``person`` made themselves, as the administration log has them."""
    return [entry for entry in cluster.server.audit.query(action="person.code.made") if entry.actor == person.id]


def _at_most(monkeypatch, *, computers=None, codes=None):
    """The server's limits, lowered for a test: each is read where the code is made, never copied."""
    if computers is not None:
        monkeypatch.setattr(sign_in_api, "COMPUTERS_MOST", computers)
    if codes is not None:
        monkeypatch.setattr(sign_in_api, "CONNECT_CODES", codes)


def _connected_by_administrator(cluster, person, count):
    for number in range(count):
        _connect(cluster, _code_for(cluster, person), f"computer {number}")


def _disconnect(cluster, person, *, every=False):
    """A computer of the person's disconnected (the newest, or ``every`` one): its token's row leaves ``tokens``, as at
    `latet disconnect` or `latet server people tokens --revoke`, which is all that gate 1 counts. Those also end the
    person's dashboard sessions on every instance as each hears it (E0b's ``revoked``), the test's own session too, at a
    moment no test can wait for: the row alone goes here."""
    with cluster.server.db.connection() as conn:
        if every:
            conn.execute("DELETE FROM tokens WHERE identity_id = %s", (person.id,))
            return
        conn.execute(
            "DELETE FROM tokens WHERE id = (SELECT id FROM tokens WHERE identity_id = %s ORDER BY created DESC, id DESC "
            "LIMIT 1)",
            (person.id,),
        )


def _waiting_behind(cluster, holder):
    """How many sessions wait for the transaction of the connection ``holder``, directly or behind another that waits."""
    with cluster.server.db.connection() as look:
        rows = look.execute(
            "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
        ).fetchall()
    behind, waiting, found = {holder.info.backend_pid}, set(), True
    while found:
        found = False
        for pid, blocking in rows:
            if pid not in waiting and behind & set(blocking):
                waiting.add(pid)
                behind.add(pid)
                found = True
    return len(waiting)


def _at_once(cluster, person, *requests):
    """``requests``, each in a thread of its own, run against each other in PostgreSQL: the person's row is held, as
    another change of theirs holds it, until each of them waits for it, then let go. What each answered, or the error
    that ended it (PostgreSQL's deadlock check may end one)."""
    answers: list = [None] * len(requests)

    def run(number, request):
        try:
            answers[number] = request()
        except Exception as exc:  # what ended it is the answer
            answers[number] = exc

    threads = [threading.Thread(target=run, args=pair) for pair in enumerate(requests)]
    with cluster.server.db.transaction() as holding:
        holding.execute("SELECT 1 FROM identities WHERE id = %s FOR UPDATE", (person.id,))
        for thread in threads:
            thread.start()
        settle(lambda: _waiting_behind(cluster, holding) >= len(requests), seconds=10)
    for thread in threads:
        thread.join(30)
    return answers


def _status(answer):
    return answer.status_code if hasattr(answer, "status_code") else type(answer).__name__


@pytest.fixture
def daniel(cluster):
    """Daniel Okafor, no role, signed in with the company's single sign-on, which is set up."""
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _set_up(cluster)
    _session(cluster, daniel, "sso")
    return daniel


def test_a_person_with_the_most_computers_makes_no_code_until_one_is_disconnected(cluster, daniel, monkeypatch):
    """Gate 1: a person's own codes connect computers up to COMPUTERS_MOST of theirs, then **Make a code** is refused
    (409, on any instance) inside the code's transaction: no code and no entry. A computer disconnected frees its
    place at once."""
    _at_most(monkeypatch, computers=2)
    _connected_by_administrator(cluster, daniel, 2)
    for client in (cluster.a, cluster.b):
        refused = _own_code(client)
        assert refused.status_code == 409, refused.text
        assert (refused.json()["code"], refused.json()["most"]) == ("computers-most", 2)
    assert _own_codes_made(cluster, daniel) == [] and _codes_held(cluster, daniel) == 0
    _disconnect(cluster, daniel)
    made = _own_code(cluster.b)
    assert made.status_code == 200, made.text
    _connect(cluster, made.json()["code"], "laptop")
    assert _computers(cluster, daniel) == 2 and _own_code(cluster.a).status_code == 409


def test_computers_an_administrator_connected_count_and_an_administrators_codes_are_not_refused(
    cluster, daniel, monkeypatch
):
    """Gate 1 stops the person's own codes alone (D2): computers connected with an administrator's codes count toward
    the most, and an administrator's code (the administration API, `latet server`) is still made and still enrols."""
    _at_most(monkeypatch, computers=2)
    _connected_by_administrator(cluster, daniel, 2)
    assert _own_code(cluster.a).status_code == 409
    _connect(cluster, _code_for(cluster, daniel), "a third")
    assert _computers(cluster, daniel) == 3


def test_a_person_holds_one_code_at_a_time(cluster, daniel):
    """What gate 1 counts at the code rests on: a new code ends the one before, so one code connects one computer."""
    first = _own_code(cluster.a).json()["code"]
    second = _own_code(cluster.b).json()["code"]
    assert _codes_held(cluster, daniel) == 1
    assert cluster.a.post(f"{SERVER_API}/enrol", json={"code": first, "label": "laptop"}).status_code == 401
    _connect(cluster, second, "laptop")
    assert _computers(cluster, daniel) == 1 and _codes_held(cluster, daniel) == 0


def test_two_codes_made_at_once_on_two_instances_leave_one_code(cluster, daniel):
    """L8.3: two codes asked for at the same moment, through A and through B, wait for each other at the person's row:
    one code is left, the one made last, and only it enrols."""
    answers = _at_once(cluster, daniel, lambda: _own_code(cluster.a), lambda: _own_code(cluster.b))
    assert [_status(answer) for answer in answers] == [200, 200]
    assert _codes_held(cluster, daniel) == 1
    enrolled = [
        cluster.a.post(f"{SERVER_API}/enrol", json={"code": answer.json()["code"], "label": "laptop"}).status_code
        for answer in answers
    ]
    assert sorted(enrolled) == [200, 401] and _computers(cluster, daniel) == 1


def test_codes_and_enrolments_at_once_never_pass_the_most(cluster, daniel, monkeypatch):
    """One below the most and holding a code, the person connects a computer with it on A while a new code is asked
    for on B, at the same moment, round after round. Whichever commits first, a code made counted the computers as they
    were once it held the person's row, so the computer it then connects never passes the most. A round may lose one of
    the two to PostgreSQL's deadlock check (a code's row and the person's, taken in each order)."""
    most = 3
    _at_most(monkeypatch, computers=most)
    _connected_by_administrator(cluster, daniel, most - 1)
    for number in range(6):
        held = _own_code(cluster.a).json()["code"]
        enrol, code = _at_once(
            cluster,
            daniel,
            lambda held=held, number=number: cluster.a.post(
                f"{SERVER_API}/enrol", json={"code": held, "label": f"round {number}"}
            ),
            lambda: _own_code(cluster.b),
        )
        said = (number, _status(enrol), _status(code))
        assert _computers(cluster, daniel) <= most, said
        if _status(code) == 200:  # the computer it is for, connected
            cluster.a.post(f"{SERVER_API}/enrol", json={"code": code.json()["code"], "label": f"after {number}"})
        assert _computers(cluster, daniel) <= most, said
        while _computers(cluster, daniel) >= most:  # back to one below, for the next round
            _disconnect(cluster, daniel)


def test_the_refusals_quote_nothing_and_the_log_names_no_one(cluster, daniel, monkeypatch, caplog):
    """Each refusal is in fixed words and the server's own numbers: nothing of the person or of the request; the server's
    log says it once a minute, naming no one."""
    monkeypatch.setattr(sign_in_api, "_AT_MOST", Paced())
    monkeypatch.setattr(sign_in_api, "_TOO_MANY", Paced())
    _at_most(monkeypatch, computers=1, codes=1)
    _connected_by_administrator(cluster, daniel, 1)
    with caplog.at_level(logging.WARNING, logger="latet.ee.sign_in_api"):
        most = _own_code(cluster.a)
        _disconnect(cluster, daniel, every=True)
        assert _own_code(cluster.a).status_code == 200
        many = _own_code(cluster.b)
    assert most.status_code == 409 and most.json() == {
        "error": (
            "you have 1 computers or more connected to your account, the most one person may connect with their own "
            "codes: disconnect one you no longer use (`latet disconnect` on it, or an administrator with `latet server "
            "people tokens`), then make a code"
        ),
        "code": "computers-most",
        "most": 1,
    }
    assert many.status_code == 429 and many.json() == {
        "error": "you made 1 codes in the last hour, the most one person may: try again later",
        "code": "codes-too-many",
        "most": 1,
    }
    said = caplog.text + most.text + many.text + json.dumps(dict(most.headers)) + json.dumps(dict(many.headers))
    for named in (daniel.id, "Daniel", "daniel.okafor", "203.0.113.7"):
        assert named not in said, named
    assert "the most computers connected" in caplog.text and "the most codes of an hour" in caplog.text


def test_a_person_past_the_most_is_told_the_most_or_more(cluster, daniel, monkeypatch):
    """L7: an administrator's codes are never refused, so a person can be past the most: told so, not that they have
    exactly it."""
    _at_most(monkeypatch, computers=2)
    _connected_by_administrator(cluster, daniel, 3)
    refused = _own_code(cluster.a)
    assert refused.status_code == 409 and "you have 2 computers or more" in refused.json()["error"]


def _whole_seconds(response):
    value = response.headers["retry-after"]
    assert value.isdigit(), value
    return int(value)


def test_a_person_makes_the_most_codes_in_any_hour_whatever_instance_they_ask(cluster, daniel, monkeypatch):
    """Gate 2: CONNECT_CODES of one's own codes in any CONNECT_CODE_WINDOW, counted in PostgreSQL under the person's row,
    whichever instance makes them: past it 429, with retry-after in whole seconds until the oldest of them leaves the
    window, and nothing written; the window slides."""
    _at_most(monkeypatch, codes=3)
    window = sign_in_api.CONNECT_CODE_WINDOW
    first = cluster.clock()
    for client in (cluster.a, cluster.b, cluster.a):
        assert _own_code(client).status_code == 200
        cluster.clock.advance(1)
    refused = _own_code(cluster.b)
    assert refused.status_code == 429 and refused.json()["code"] == "codes-too-many", refused.text
    assert _whole_seconds(refused) == math.ceil(first + window - cluster.clock())
    assert len(_own_codes_made(cluster, daniel)) == 3  # the refused one wrote nothing
    cluster.clock.now = first + window  # the first code left the window: it is no longer in the last hour
    assert _own_code(cluster.b).status_code == 200
    again = _own_code(cluster.a)
    assert again.status_code == 429 and _whole_seconds(again) == math.ceil(first + 1 + window - cluster.clock())
    cluster.clock.advance(0.25)  # never a fraction of a second, and never 0
    assert _whole_seconds(_own_code(cluster.a)) == math.ceil(first + 1 + window - cluster.clock())
    cluster.clock.now = first + 1 + window - 0.5
    assert _whole_seconds(_own_code(cluster.a)) == 1


def test_each_person_has_their_own_codes(cluster, daniel, monkeypatch):
    _at_most(monkeypatch, codes=2)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com")
    _session(cluster, emily, "sso")
    assert [_own_code(cluster.a).status_code for _ in range(3)] == [200, 200, 429]
    _session(cluster, daniel, "sso")
    assert _own_code(cluster.b).status_code == 200


def test_codes_an_administrator_made_for_a_person_are_not_theirs(cluster, daniel, monkeypatch):
    """The People page's **Code** for Daniel names its administrator as the actor, and the administration API's its
    token: neither uses up Daniel's own."""
    _at_most(monkeypatch, codes=2)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    _session(cluster, emily, "sso")
    for _ in range(3):
        assert cluster.a.post(f"{API}/ee/people/{daniel.id}/code", headers=UI).status_code == 200
        _code_for(cluster, daniel)
    _session(cluster, daniel, "sso")
    assert [_own_code(cluster.b).status_code for _ in range(3)] == [200, 200, 429]


def test_a_refusal_costs_no_code(cluster, monkeypatch):
    """L8.4: a request refused before the count (a session a one-time code opened while the company sign-in decides,
    single sign-on not ready) or inside the transaction (the most computers, a person disabled meanwhile) writes no
    entry, so it uses up none of the person's codes."""
    codes = 3
    _at_most(monkeypatch, computers=1, codes=codes)
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    _session(cluster, daniel, "sso")
    assert _own_code(cluster.a).status_code == 404  # single sign-on not set up
    _set_up(cluster)
    _session(cluster, daniel, "code")
    assert _own_code(cluster.a).json() == COMPANY_SIGN_IN
    _session(cluster, daniel, "sso")
    _connected_by_administrator(cluster, daniel, 1)
    assert _own_code(cluster.b).json()["code"] == "computers-most"
    _disconnect(cluster, daniel, every=True)
    api = cluster.server.edition.sign_in_api
    who = Who(daniel.id, daniel.id, None, _run_now, _changing(cluster, daniel.id))
    # Disabled after the request read him and before its transaction: his row alone, so that no instance ends the
    # test's session (a disable is told on the bus, and each instance ends his sessions as it hears it).
    with cluster.server.db.connection() as conn:
        conn.execute("UPDATE identities SET disabled = true WHERE id = %s", (daniel.id,))
    with pytest.raises(Refused) as refused:
        asyncio.run(api.connect_code(_POST, who))
    assert (refused.value.status, refused.value.message) == (409, "your account is disabled")
    with cluster.server.db.connection() as conn:
        conn.execute("UPDATE identities SET disabled = false WHERE id = %s", (daniel.id,))
    assert _own_codes_made(cluster, daniel) == []
    assert [_own_code(cluster.a).status_code for _ in range(codes + 1)] == [200] * codes + [429]


def test_two_codes_at_once_on_two_instances_never_pass_the_limit(cluster, daniel, monkeypatch):
    """One below the limit, two codes asked for at the same moment through A and B: the second counts the first's entry,
    written in its transaction, so one is made and the other refused."""
    codes = 4
    _at_most(monkeypatch, codes=codes)
    for _ in range(codes - 1):
        assert _own_code(cluster.a).status_code == 200
    answers = _at_once(cluster, daniel, lambda: _own_code(cluster.a), lambda: _own_code(cluster.b))
    assert sorted(_status(answer) for answer in answers) == [200, 429]
    assert len(_own_codes_made(cluster, daniel)) == codes


def _plan_indexes(plan):
    """The indexes a plan of EXPLAIN (FORMAT JSON) reads, and the kinds of its other steps."""
    found, steps = set(), set()
    nodes = [plan]
    while nodes:
        node = nodes.pop()
        if "Index Name" in node:
            found.add(node["Index Name"])
        steps.add(node.get("Node Type"))
        nodes.extend(node.get("Plans", []))
    return found, steps


def test_an_index_serves_the_count_of_a_persons_codes(cluster, daniel):
    """Gate 2's count is the code's own statement (CODES_MADE, bound as the code binds it): a walk of the person's own
    entries by the notices' index in the order of its ids, stopped at the limit (no sort of their whole history), never
    a read of every entry of the hour (audit_ts, audit_ts_id) nor of the whole log; as psycopg plans it once it
    prepares the statement too (a generic plan, where only a literal action lets the notices' index serve)."""
    for _ in range(3):
        _own_code(cluster.a)
    args = sign_in_api.codes_made_by(daniel.id)
    numbered = sign_in_api.CODES_MADE
    for number in range(1, len(args) + 1):
        numbered = numbered.replace("%s", f"${number}", 1)
    plans = {}
    with cluster.server.db.transaction() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute(f"PREPARE codes_made AS {numbered}")  # the parameters' types as PostgreSQL infers them
        for mode in ("force_custom_plan", "force_generic_plan"):  # with the values, as the first executions; without
            conn.execute(f"SET LOCAL plan_cache_mode = {mode}")
            # A utility statement takes no parameters of its own: the values are written in, quoted.
            values = sql.SQL(", ").join(map(sql.Literal, args))
            explained = conn.execute(sql.SQL("EXPLAIN (FORMAT JSON) EXECUTE codes_made ({})").format(values)).fetchone()
            plans[mode] = explained[0][0]["Plan"]
        conn.execute("DEALLOCATE codes_made")
    for mode, plan in plans.items():
        indexes, steps = _plan_indexes(plan)
        assert indexes == {"audit_notices"}, (mode, plan)
        assert not steps & {"Seq Scan", "Sort", "Incremental Sort"}, (mode, plan)
