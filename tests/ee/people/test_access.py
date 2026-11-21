"""What a person may do with the Enterprise pages, and the rule each Enterprise route is under (ee/access.py)."""

import asyncio
import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, Router
from starlette.testclient import TestClient

from latet.api import rest
from latet.ee.access import ACTING, ADMINISTRATION, IDENTITY, RULES, Access, rule_for
from latet.ee.api import _change_of
from latet.ee.license import LicenseError
from latet.ee.roles import ACTIONS, Role
from latet.state import IdentityRef

API = "/_latet/api/v1"
EE = "/_latet/api/v1/ee"
EMILY = IdentityRef("u_emilycartr", "Emily Carter", "person")
BILLING = IdentityRef("u_billingci0", "Billing CI", "key")


def test_the_person_working_in_a_keys_space_is_named_by_communitys_one_constant():
    """Community's dashboard reads the person a forget in a key's space is recorded by from ``scope[ACTING]``: the
    Enterprise Edition sets the key Community reads, never a copy of its string (ee may import Community)."""
    assert ACTING is rest.ACTING and ACTING == "latet.acting"


class _Licensing:
    def __init__(self, *features):
        self.features = set(features)

    def licensed(self, feature):
        return feature in self.features


class _People:
    def __init__(self, roles=()):
        self.roles = frozenset({Role.USER, *roles})

    def roles_of(self, identity_id):
        return frozenset() if identity_id == BILLING.id else self.roles


class _Unanswered:
    """People whose store does not answer (PostgreSQL or Valkey down): its words name an address."""

    def roles_of(self, identity_id):
        raise ConnectionError("connection to 10.0.0.5 refused")


def _status(access, method, path, identity_id=EMILY.id):
    refusal = access.refusal(rule_for(EE + path), method, identity_id)
    return refusal[0] if refusal else 200


def test_the_administration_token_may_take_every_action_and_no_identity_none():
    # R21: every edition is a server's; the administration token is named by its sentinel alone (L2), never by None.
    access = Access(_People(), _Licensing("roles", "sso"))
    assert access.actions(ADMINISTRATION) == frozenset(ACTIONS)  # `latet server`'s calls: Who(ADMINISTRATION, …)
    assert access.roles(ADMINISTRATION) == frozenset()
    assert access.actions(None) == frozenset() and access.roles(None) == frozenset()
    assert not access.allows(None, "people.manage") and not access.reads(None, "audit.read")


@pytest.mark.parametrize(
    ("roles", "method", "path", "status"),
    [
        ((), "GET", "/policy", 200),  # everyone reads the floor: their pages show its locks
        ((), "GET", "/license", 403),
        ((Role.ADMIN,), "PUT", "/license", 200),
        ((Role.SECURITY,), "PUT", "/license", 403),
        ((Role.SECURITY,), "PUT", "/policy", 200),
        ((), "GET", "/people", 403),
        ((Role.ADMIN,), "POST", "/groups/{id}/members", 200),
        ((), "GET", "/keys", 200),  # the keys one may open
        ((), "POST", "/keys", 403),
        ((), "POST", "/keys/{key}/space/{rest:path}", 200),  # the space checks the control of the key itself
        # Task 9: the control of a key, and a key rotated, disabled or enabled (keys.manage)
        ((), "POST", "/keys/{id}/controllers", 403),
        ((Role.ADMIN,), "DELETE", "/keys/{id}/controllers/{subject}", 200),
        ((Role.SECURITY,), "POST", "/keys/{id}/rotate", 403),
        ((), "POST", "/sign-in/code", 200),
        ((), "GET", "/sign-in", 403),
        ((Role.ADMIN,), "DELETE", "/sign-in/scim-tokens/{id}", 200),
    ],
)
def test_on_a_server_the_roles_decide_each_route(roles, method, path, status):
    assert _status(Access(_People(roles), _Licensing("roles", "sso")), method, path) == status


def test_without_roles_in_the_license_everyone_reads_and_nobody_changes_from_the_dashboard():
    access = Access(_People(), _Licensing("sso", "locked-rules"))
    assert _status(access, "GET", "/sign-in") == 200
    assert access.refusal(rule_for(EE + "/license"), "PUT", EMILY.id) == (
        403,
        "on this server, the Enterprise settings are changed with `latet server`",
    )
    assert access.refusal(rule_for(EE + "/people"), "GET", EMILY.id) == (403, "this needs Latet Enterprise with roles")
    assert access.actions(EMILY.id) == frozenset()
    # Everyone reads them while roles do not decide; E4: nobody reads reports or the log.
    assert access.pages(EMILY, ["license", "reports", "audit"], sign_in_ready=False) == ["license", "sign-in"]


def test_a_route_under_no_rule_is_refused_and_so_is_a_request_with_no_identity_on_a_server():
    access = Access(_People(), _Licensing("roles"))
    assert rule_for(EE + "/whatever") is None and rule_for(EE + "/keysafe") is None
    assert access.refusal(None, "GET", EMILY.id) == (403, "this Enterprise route has no access rule")
    assert access.refusal(rule_for(EE + "/policy"), "GET", None) == (401, "unauthorized")


def test_a_request_in_an_api_keys_space_reaches_no_enterprise_route():
    """Task 3's carry, kept by Task 9: in a key's space the key is the identity, the person working there ``acting``;
    the routes that let anyone signed in through (the keys one may open, the floor, connecting a computer) are a
    person's, and the others a role's, which a key never holds."""
    access = Access(_People((Role.ADMIN,)), _Licensing("roles", "sso"))
    refused = (403, "an API key's space reaches none of the Enterprise pages")
    for method, path in (
        ("GET", "/keys"),
        ("GET", "/policy"),
        ("POST", "/sign-in/code"),
        ("GET", "/license"),
        ("GET", "/policy/text"),  # Task 13: the policy's editor is a person's of the security role, never a key's
        ("PUT", "/policy"),
    ):
        assert access.refusal(rule_for(EE + path), method, BILLING.id, acting=EMILY.id) == refused, path
        assert access.weighed(rule_for(EE + path), method, BILLING.id, acting=EMILY.id) == refused, path
    assert access.refusal(rule_for(EE + "/keys"), "GET", EMILY.id) is None  # her own dashboard: the keys she opens


def test_the_gate_refuses_every_enterprise_route_to_a_request_in_an_api_keys_space():
    routes = [Route(f"{EE}/keys", _ok), Route(f"{EE}/policy", _ok)]
    app = Starlette(routes=Access(_People(), _Licensing("roles")).gated(routes, run=asyncio.to_thread))

    async def in_the_keys_space(scope, receive, send):
        await app({**scope, IDENTITY: BILLING.id, ACTING: EMILY.id}, receive, send)

    with TestClient(in_the_keys_space) as client:
        for path in (f"{EE}/keys", f"{EE}/policy"):
            answer = client.get(path)
            assert (answer.status_code, answer.json()) == (
                403,
                {"error": "an API key's space reaches none of the Enterprise pages"},
            )


def test_the_pages_are_those_the_person_reads_and_an_api_keys_space_shows_none():
    access = Access(_People((Role.AUDITOR,)), _Licensing("roles", "sso"))
    pages = ["license", "policy", "reports", "audit"]
    assert access.pages(EMILY, pages, sign_in_ready=True) == ["policy", "reports", "audit", "sign-in"]
    assert access.pages(EMILY, pages, sign_in_ready=False) == ["policy", "reports", "audit"]
    assert access.pages(BILLING, pages, sign_in_ready=True) == []


def test_the_specific_rules_come_before_the_ones_they_fall_under():
    assert rule_for(EE + "/keys/{key}/space/{rest:path}").path == "/ee/keys/*/space"
    assert rule_for(EE + "/sign-in/code").path == "/ee/sign-in/code"
    assert rule_for(EE + "/sign-in/scim-tokens/{id}").path == "/ee/sign-in"  # both actions named: its subtree too
    assert len({rule.path for rule in RULES}) == len(RULES)


def test_a_rule_that_lets_anyone_read_or_change_covers_its_own_path_alone():
    # A route below one is under no rule until its part adds a row (deny by default): never anyone's by a prefix.
    # Task 9's row for what is below /ee/keys names both actions (keys.manage): its subtree.
    assert rule_for(EE + "/keys").path == "/ee/keys" and rule_for(EE + "/keys/{id}/controllers").path == "/ee/keys/*"
    assert rule_for(EE + "/policy").path == "/ee/policy" and rule_for(EE + "/policy/history") is None
    assert rule_for(EE + "/sign-in/code/{rest:path}").path == "/ee/sign-in"  # the stricter one it is below
    # The key's space alone is a subtree by design: KeySpace checks the control of the key for all of it.
    assert rule_for(EE + "/keys/{key}/space/history/{id}").path == "/ee/keys/*/space"


def test_term_sync_is_the_security_roles_and_nobodys_while_roles_do_not_decide():
    """E6's Task 12: one row for the Term sync page's routes, its sources and their secrets below it; terms.manage is
    read through roles only (fix round 1's C1), so that without roles in the license nobody reads a synced term or a
    source in the dashboard (`latet server terms` does)."""
    assert {rule_for(EE + path).path for path in ("/term-sync", "/term-sync/gone", "/term-sync/secrets/{source}")} == {
        "/ee/term-sync"
    }
    security = Access(_People((Role.SECURITY,)), _Licensing("roles", "term-sync"))
    for method, path in (("GET", "/term-sync/gone"), ("PUT", "/term-sync/sources"), ("DELETE", "/term-sync/secrets/x")):
        assert _status(security, method, path) == 200, path
    for roles in ((), (Role.ADMIN,), (Role.AUDITOR,)):
        assert _status(Access(_People(roles), _Licensing("roles", "term-sync")), "GET", "/term-sync") == 403
    unroled = Access(_People((Role.SECURITY,)), _Licensing("term-sync", "sso"))
    assert _status(unroled, "GET", "/term-sync") == 403 and _status(unroled, "GET", "/term-sync/gone") == 403
    assert unroled.pages(EMILY, ["license", "term-sync"], sign_in_ready=False) == ["license", "sign-in"]
    assert _status(Access(_People((Role.SECURITY,)), _Licensing("roles")), "GET", "/term-sync") == 403  # the feature


def test_the_policys_text_is_read_by_those_who_set_it_and_by_nobody_while_roles_do_not_decide():
    assert rule_for(EE + "/policy/text").path == "/ee/policy/text"
    security = Access(_People((Role.SECURITY,)), _Licensing("roles"))
    assert _status(security, "GET", "/policy/text") == 200 and _status(security, "DELETE", "/policy") == 200
    assert _status(Access(_People((Role.AUDITOR,)), _Licensing("roles")), "GET", "/policy/text") == 403
    unroled = Access(_People((Role.SECURITY,)), _Licensing("locked-rules"))
    assert _status(unroled, "GET", "/policy/text") == 403 and _status(unroled, "GET", "/policy") == 200


# --- the gate ----------------------------------------------------------------------------------------------------------


def _ok(request):
    return JSONResponse({"reached": request.url.path})


def _client(access, routes, identity_id=EMILY.id):
    """The gated routes served as the server's gate hands a request on: the identity in the scope (E0b's)."""
    app = Starlette(routes=access.gated(routes, run=asyncio.to_thread))

    async def handed(scope, receive, send):
        await app({**scope, IDENTITY: identity_id} if identity_id is not None else scope, receive, send)

    return TestClient(handed)


def test_the_gate_lets_through_what_the_rule_allows_and_refuses_the_rest_with_its_reason():
    routes = [Route(f"{EE}/license", _ok, methods=["GET", "PUT"])]
    with _client(Access(_People(), _Licensing("roles")), routes) as client:
        refused = client.get(f"{EE}/license")
        assert refused.status_code == 403
        assert refused.json() == {"error": "your roles on this server do not include license.manage"}
        assert client.head(f"{EE}/license").status_code == 403  # HEAD is a read, as GET
        assert client.put(f"{EE}/license").json() == {"error": "your roles on this server do not allow license.manage"}
    with _client(Access(_People((Role.ADMIN,)), _Licensing("roles")), routes) as client:
        assert client.get(f"{EE}/license").json() == {"reached": f"{EE}/license"}
        assert client.put(f"{EE}/license").status_code == 200


def test_the_gate_refuses_a_request_the_server_handed_on_with_no_identity():
    with _client(Access(_People((Role.ADMIN,)), _Licensing("roles")), [Route(f"{EE}/policy", _ok)], None) as client:
        answer = client.get(f"{EE}/policy")
    assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})


def test_a_route_under_no_rule_is_served_refused_and_one_the_gate_cannot_hold_is_left_out(caplog):
    access = Access(_People((Role.ADMIN,)), _Licensing("roles", "sso"))
    routes = [Route(f"{EE}/whatever", _ok), Mount(f"{EE}/files", app=Starlette(routes=[Route("/x", _ok)]))]
    with caplog.at_level(logging.ERROR, logger="latet.ee.access"), _client(access, routes) as client:
        answer = client.get(f"{EE}/whatever")
        assert (answer.status_code, answer.json()) == (403, {"error": "this Enterprise route has no access rule"})
        assert client.get(f"{EE}/files/x").status_code == 404  # never reached: the Mount is not served
    assert access.unruled == {f"{EE}/whatever", f"{EE}/files"}
    assert "under no access rule" in caplog.text and "no plain route" in caplog.text


def test_every_route_of_the_edition_is_held_by_the_gate_or_left_out_whatever_its_path(caplog):
    # Every route the edition hands the gate is an Enterprise one: one outside EE + "/" is never served ungated.
    access = Access(_People(), _Licensing("roles", "sso"))
    routes = [
        Mount(EE, app=Starlette(routes=[Route("/license", _ok)])),
        Mount(API, routes=[Route("/ee/license", _ok)]),
        Route(EE + "{rest:path}", _ok),
        Route(EE, _ok),
        Route(f"{API}/me", _ok),
        Router(routes=[Route(f"{EE}/license", _ok)]),
    ]
    with caplog.at_level(logging.ERROR, logger="latet.ee.access"), _client(access, routes) as client:
        for path in (f"{EE}/license", EE, f"{EE}x", f"{API}/me"):
            assert client.get(path).status_code == 404, path  # nothing of it served
    assert access.unruled == {EE, API, EE + "{rest:path}", f"{API}/me", ""}


def test_the_gate_refuses_while_the_peoples_roles_cannot_be_read_and_says_it_by_its_type(caplog):
    reached = []

    def handler(request):
        reached.append(request.url.path)
        return JSONResponse({})

    access = Access(_Unanswered(), _Licensing("roles"))
    with (
        caplog.at_level(logging.WARNING, logger="latet.ee.access"),
        _client(access, [Route(f"{EE}/license", handler)]) as client,
    ):
        answer = client.get(f"{EE}/license")
    assert answer.status_code == 503 and answer.headers["retry-after"] == "1"
    assert answer.json() == {"error": "who may do what on this server cannot be read now: try again"}
    assert reached == [] and "ConnectionError" in caplog.text and "10.0.0.5" not in caplog.text


class _LicenseUnread:
    """A license that cannot be read now (its store does not answer): its words name an address."""

    def licensed(self, feature):
        raise ConnectionError("connection to 10.0.0.5 refused")


def test_the_gate_marks_what_it_closes_in_front_of_an_api_keys_space_and_nothing_else(caplog):
    """Task 12, fix round 1: the dashboard goes back to the person's own space on ``space: "closed"``, never on the gate's
    words. The license without roles (403) and a license that cannot be read (503) close the key's space; the same
    refusals on any other route do not, and neither does an Enterprise route refused inside the space (``acting``)."""
    space, people = f"{EE}/keys/u_billingci0/space/me", f"{EE}/people"
    routes = [Route(f"{EE}/keys/{{key}}/space/{{rest:path}}", _ok), Route(f"{EE}/people", _ok)]
    no_roles = Access(_People((Role.ADMIN,)), _Licensing("sso"))
    with _client(no_roles, routes) as client:
        closed, kept = client.get(space), client.get(people)
    assert (closed.status_code, closed.json()) == (
        403,
        {"error": "this needs Latet Enterprise with roles", "space": "closed"},
    )
    assert (kept.status_code, kept.json()) == (403, {"error": "this needs Latet Enterprise with roles"})
    unread = Access(_People((Role.ADMIN,)), _LicenseUnread())
    with caplog.at_level(logging.WARNING, logger="latet.ee.access"), _client(unread, routes) as client:
        closed, kept = client.get(space), client.get(people)
    unreadable = "who may do what on this server cannot be read now: try again"
    assert (closed.status_code, closed.json(), closed.headers["retry-after"]) == (
        503,
        {"error": unreadable, "space": "closed"},
        "1",
    )
    assert (kept.status_code, kept.json()) == (503, {"error": unreadable})
    assert "10.0.0.5" not in caplog.text
    # An Enterprise route refused inside the key's space is the person's own page asked of the key: nothing closes.
    app = Starlette(routes=no_roles.gated(routes, run=asyncio.to_thread))

    async def inside(scope, receive, send):
        await app({**scope, IDENTITY: BILLING.id, ACTING: EMILY.id}, receive, send)

    with TestClient(inside) as client:
        answer = client.get(space)
    assert (answer.status_code, answer.json()) == (
        403,
        {"error": "an API key's space reaches none of the Enterprise pages"},
    )
    # A request the gate lets through is not marked.
    with _client(Access(_People(), _Licensing("roles")), routes) as client:
        assert client.get(space).json() == {"reached": space}


# --- the License page's change, as its body is read ----------------------------------------------------------------


def test_a_license_change_body_is_a_json_object_and_a_license_file_holds_whole_characters():
    assert _change_of(b"", installing=False) == (None, False)
    assert _change_of(json.dumps({"policy_stops": True}).encode(), installing=False) == (None, True)
    assert _change_of(json.dumps({"policy_stops": "yes"}).encode(), installing=False) == (None, False)
    for body in (b"[1]", b'"x"', b"null", b"{", b"NaN"):
        refused = _change_of(body, installing=False)
        assert isinstance(refused, Response) and refused.status_code == 400, body
    sent = json.dumps({"license": "text", "policy_stops": True}).encode()
    assert _change_of(sent, installing=True) == (b"text", True)
    for body in (b"", b"[1]", json.dumps({"license": 1}).encode()):
        refused = _change_of(body, installing=True)
        assert isinstance(refused, Response) and refused.status_code == 400, body
    half = json.dumps({"license": chr(0xD800)}).encode()  # JSON's escape of a lone surrogate
    with pytest.raises(LicenseError) as refused_file:
        _change_of(half, installing=True)
    assert refused_file.value.code == "format"
