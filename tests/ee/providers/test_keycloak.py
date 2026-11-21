"""Single sign-on against a real Keycloak in Docker (the release checklist's item 4 (a)), set up as
docs/src/content/docs/enterprise/sign-in.mdx says for Keycloak: a realm (keycloak/realm.json, imported), a client with
client authentication on and the standard flow, and a Group Membership mapper (claim `groups`) in its dedicated scope.
Latet's side is set with `latet server sign-in set`, `secret` and `test`, and roles given to groups made by hand with
`latet server groups add` and `latet server roles grant`. A person's browser is an HTTP client here: it follows Latet's
redirect to Keycloak, posts Keycloak's login form, and follows Keycloak's redirect back to Latet's callback on either
instance. Opt-in: LATET_PROVIDER_TESTS=1, with quay.io/keycloak/keycloak:26.8.2 pulled by hand (README.md)."""

from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from latet.ee.oidc import CALLBACK

from ..people.server_support import API, UI
from . import docker
from .conftest import HOST, PROVIDER_TESTS

pytestmark = PROVIDER_TESTS

IMAGE = "quay.io/keycloak/keycloak:26.8.2"
REALM = "acme"
CLIENT_ID = "latet"
REDIRECT_URI = f"https://latet.acme.lan{CALLBACK}"  # the cluster's address (tests/ee/servers.py: URL)
LOCALE = json.loads((Path(__file__).parents[3] / "dashboard" / "src" / "locales" / "en.json").read_text("utf-8"))
SAID = LOCALE["auth"]["company"]  # the sign-in page's words for each reason


@dataclass
class Keycloak:
    container: docker.Container
    base: str
    admin_password: str
    client_secret: str
    passwords: dict[str, str]

    @property
    def issuer(self) -> str:
        return f"{self.base}/realms/{REALM}"

    def admin(self) -> httpx.Client:
        """Keycloak's admin REST API, as its administrator signed in to the master realm."""
        token = httpx.post(
            f"{self.base}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                  "password": self.admin_password},
        ).json()["access_token"]  # fmt: skip
        return httpx.Client(base_url=f"{self.base}/admin/realms/{REALM}", headers={"authorization": f"Bearer {token}"})

    def set_enabled(self, username: str, enabled: bool) -> None:
        with self.admin() as api:
            [user] = api.get("/users", params={"username": username, "exact": "true"}).json()
            api.put(f"/users/{user['id']}", json={**user, "enabled": enabled}).raise_for_status()


def realm_file() -> dict:
    return json.loads((Path(__file__).parent / "keycloak" / "realm.json").read_text("utf-8"))


@contextmanager
def running_keycloak(tmp_path_factory, realm: dict, name: str = "keycloak", truststore: bytes | None = None):
    """Keycloak 26.8.2 in development mode, ``realm`` imported at its start with a client secret and its users'
    passwords made for this run; ``truststore``: a certificate authority's PEM, in Keycloak's truststore folder."""
    client_secret = secrets.token_urlsafe(32)
    realm["clients"][0]["secret"] = client_secret
    passwords = {}
    for user in realm.get("users", []):
        passwords[user["username"]] = password = secrets.token_urlsafe(16)
        user["credentials"] = [{"type": "password", "value": password, "temporary": False}]
    folder = tmp_path_factory.mktemp("keycloak-import")
    (folder / f"{REALM}.json").write_text(json.dumps(realm), "utf-8")
    files = {"/opt/keycloak/data/import": folder}
    if truststore is not None:  # conf/truststores: the folder Keycloak reads PEM files from at its start
        trusted = tmp_path_factory.mktemp("keycloak-truststores")
        (trusted / "acme-ca.pem").write_bytes(truststore)
        files["/opt/keycloak/conf/truststores"] = trusted
    admin_password = secrets.token_urlsafe(24)
    container = docker.start(
        name,
        IMAGE,
        ports=[8080],
        env={"KC_BOOTSTRAP_ADMIN_USERNAME": "admin", "KC_BOOTSTRAP_ADMIN_PASSWORD": admin_password},
        args=["start-dev", "--import-realm"],
        files=files,
    )
    try:
        base = f"http://{HOST}:{container.ports[8080]}"
        found = Keycloak(container, base, admin_password, client_secret, passwords)
        well_known = f"{found.issuer}/.well-known/openid-configuration"
        docker.wait(lambda: httpx.get(well_known).status_code == 200, "Keycloak's realm", container)
        yield found
    finally:
        container.remove()


@pytest.fixture(scope="module")
def keycloak(tmp_path_factory):
    """Keycloak with the realm of keycloak/realm.json."""
    with running_keycloak(tmp_path_factory, realm_file()) as found:
        yield found


class Browser:
    """A person's browser at Keycloak's pages. Keycloak marks its cookies Secure, and a browser sends them to
    http://127.0.0.1 and http://localhost too, which it counts as secure contexts: so does this one."""

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=30)
        self.jar: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {"cookie": "; ".join(f"{k}={v}" for k, v in self.jar.items())} if self.jar else {}
        answer = self.http.request(method, url, headers=headers, **kwargs)
        for line in answer.headers.get_list("set-cookie"):
            pair, *attributes = line.split(";")
            name, _, value = pair.strip().partition("=")
            if not value or any(a.strip().lower() == "max-age=0" for a in attributes):
                self.jar.pop(name, None)
            else:
                self.jar[name] = value
        return answer


@dataclass
class AtKeycloak:
    """Keycloak kept the person on its own page: what that page says."""

    status: int
    said: str


def _feedback(page: str) -> str:
    """What Keycloak's page tells the person: its error under the form, or its message."""
    for pattern in (r'id="input-error"[^>]*>(.*?)</span>', r'class="[^"]*kc-feedback-text[^"]*"[^>]*>(.*?)</',
                    r'id="kc-error-message"[^>]*>\s*<p[^>]*>(.*?)</p>'):  # fmt: skip
        found = re.search(pattern, page, re.S)
        if found:
            return " ".join(unescape(re.sub(r"<[^>]+>", " ", found.group(1))).split())
    return ""


def sign_in(cluster, keycloak, user: str, *, browser=None, begin=None, back=None, password=None):
    """The person's browser: **Sign in with your company account** on instance ``begin`` (A), Keycloak's login form
    posted with their password, and Keycloak's redirect followed to the callback on instance ``back`` (``begin``'s by
    default). Latet's answer, or AtKeycloak when Keycloak kept the browser on its own page."""
    browser = browser or Browser()
    begin = begin or cluster.a
    started = begin.get("/_latet/sso/login", follow_redirects=False)
    assert started.status_code == 303, started.text
    location = started.headers["location"]
    assert location.startswith(f"{keycloak.issuer}/protocol/openid-connect/auth?"), location
    page = browser.request("GET", location)
    if page.status_code == 200 and 'id="kc-form-login"' in page.text:
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page.text)
        assert form is not None, page.text
        action = unescape(form.group(1))
        given = password if password is not None else keycloak.passwords[user]
        page = browser.request("POST", action, data={"username": user, "password": given, "credentialId": ""})
    if page.status_code != 302:
        return AtKeycloak(page.status_code, _feedback(page.text))
    redirect = urlsplit(page.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT_URI, page.headers["location"]
    return (back or begin).get(f"{redirect.path}?{redirect.query}", follow_redirects=False)


def signed_in(response) -> bool:
    return not isinstance(response, AtKeycloak) and response.status_code == 200 and "url=/_latet/ui/" in response.text


def refused(response) -> str:
    """The reason Latet sends the browser back to its sign-in page with."""
    assert not isinstance(response, AtKeycloak), response
    assert response.status_code == 303, response.text
    return response.headers["location"].removeprefix("/_latet/ui/?sign-in=")


TEAMS = {"security-team": ("Security team", "security"), "auditors": ("Auditors", "auditor")}


def set_up(latet_server, keycloak, tmp_path, *, secret=None, issuer=None, teams=TEAMS):
    """Single sign-on set up as the page says, with `latet server sign-in set`, `secret` and `test`; the Latet groups
    that ``teams`` maps Keycloak's groups to made by hand, each given its role (none: no role)."""
    settings = {
        "issuer": issuer or keycloak.issuer,
        "client_id": CLIENT_ID,
        "accounts": "group",
        "access_group": "latet-users",
        "groups": {group: name for group, (name, _role) in teams.items()},
    }
    sso = tmp_path / "sso.yaml"
    sso.write_text(yaml.safe_dump({"sso": settings}, sort_keys=False), "utf-8")
    shown = latet_server("sign-in", "set", str(sso)).output
    assert f"Redirect URI to register at the provider: {REDIRECT_URI}" in shown, shown
    latet_server("sign-in", "secret", input=(secret or keycloak.client_secret) + "\n")
    tested = latet_server("sign-in", "test", ok=False)
    for name, role in teams.values():
        latet_server("groups", "add", name)
        if role is not None:
            latet_server("roles", "grant", name, role)
    return tested


def me(client) -> tuple[int, Any]:
    """The status of the session's /me, and what it says (None unless 200)."""
    answer = client.get(f"{API}/me", headers=UI)
    return answer.status_code, answer.json() if answer.status_code == 200 else None


def _people(cluster):
    return {person.name: person for person in cluster.server.identities.all(kind="person")}


def test_the_access_group_lets_a_member_in_and_keeps_another_out_and_the_groups_roles_decide_what_each_reads(
    cluster, keycloak, latet_server, tmp_path
):
    tested = set_up(latet_server, keycloak, tmp_path)
    assert tested.exit_code == 0, tested.output
    assert f"The provider answers: issuer {keycloak.issuer}, signing keys published: " in tested.output

    emily = Browser()
    assert signed_in(sign_in(cluster, keycloak, "emily.carter", browser=emily))
    status, found = me(cluster.a)
    assert status == 200 and found["edition"]["roles"] == ["security", "user"], found
    assert {"reports.all", "policy.edit", "terms.manage"} <= set(found["edition"]["actions"])
    assert cluster.a.get(f"{API}/ee/policy/text", headers=UI).status_code != 403  # the security role's alone

    cluster.a.cookies.clear()
    assert signed_in(sign_in(cluster, keycloak, "daniel.okafor"))
    status, found = me(cluster.a)
    assert status == 200 and found["edition"]["roles"] == ["auditor", "user"], found
    assert "audit.read" in found["edition"]["actions"] and "policy.edit" not in found["edition"]["actions"]
    assert cluster.a.get(f"{API}/ee/audit", headers=UI).status_code == 200
    assert cluster.a.get(f"{API}/ee/policy/text", headers=UI).status_code == 403

    cluster.a.cookies.clear()
    outside = sign_in(cluster, keycloak, "sam.rivera")  # signed in at Keycloak, in none of Latet's groups
    assert refused(outside) == "not-allowed"
    print(f"Outside access_group, the sign-in page says: {SAID['notAllowed']!r}")
    assert me(cluster.a)[0] == 401

    people = _people(cluster)
    assert sorted(people) == ["Daniel Okafor", "Emily Carter"]  # Sam got no account
    emily_id, daniel_id = people["Emily Carter"].id, people["Daniel Okafor"].id
    assert people["Emily Carter"].email == "emily.carter@acme.com" and people["Emily Carter"].source == "oidc"
    assert [g.name for g in cluster.people.groups_of(emily_id)] == ["Security team"]
    assert [g.name for g in cluster.people.groups_of(daniel_id)] == ["Auditors"]
    assert cluster.people.account(emily_id).user_name == "emily.carter"  # Keycloak's preferred_username


def test_a_sign_in_begun_on_one_instance_is_finished_on_the_other(cluster, keycloak, latet_server, tmp_path):
    set_up(latet_server, keycloak, tmp_path)
    assert signed_in(sign_in(cluster, keycloak, "emily.carter", begin=cluster.a, back=cluster.b))
    assert me(cluster.b)[0] == 200 and me(cluster.a)[0] == 200
    cluster.a.cookies.clear()
    assert signed_in(sign_in(cluster, keycloak, "daniel.okafor", begin=cluster.b, back=cluster.a))
    assert me(cluster.a)[1]["edition"]["roles"] == ["auditor", "user"]


def test_a_person_disabled_in_keycloak_cannot_sign_in_again_and_keeps_the_session_they_have(
    cluster, keycloak, latet_server, tmp_path
):
    set_up(latet_server, keycloak, tmp_path)
    browser = Browser()
    assert signed_in(sign_in(cluster, keycloak, "daniel.okafor", browser=browser))
    keycloak.set_enabled("daniel.okafor", False)
    try:
        again = sign_in(cluster, keycloak, "daniel.okafor", browser=browser)  # Keycloak's own session in its cookie
        fresh = sign_in(cluster, keycloak, "daniel.okafor")  # a browser of their own, the password typed
    finally:
        keycloak.set_enabled("daniel.okafor", True)
    assert isinstance(again, AtKeycloak) and isinstance(fresh, AtKeycloak), (again, fresh)
    print(f"Keycloak, signed in already: {again.status} {again.said!r}; a new sign-in: {fresh.status} {fresh.said!r}")
    # Latet is not told: the session opened before lasts its 12 hours on every instance, and the account stays enabled
    # until an administrator disables it (without SCIM).
    assert me(cluster.a)[0] == 200 and me(cluster.b)[0] == 200
    [daniel] = _people(cluster).values()
    assert not cluster.server.identities.current(daniel.id).disabled


def test_a_wrong_client_secret_is_refused_with_the_words_latet_gives(cluster, keycloak, latet_server, tmp_path, caplog):
    tested = set_up(latet_server, keycloak, tmp_path, secret=secrets.token_urlsafe(32))
    assert tested.exit_code == 0  # the test reads the discovery document and the keys: only a sign-in checks the secret
    with caplog.at_level(logging.WARNING, logger="latet.ee.oidc"):
        answer = sign_in(cluster, keycloak, "emily.carter")
    assert refused(answer) == "failed"
    print(f"A wrong client secret: the sign-in page says {SAID['failed']!r}")
    logged = [r.getMessage() for r in caplog.records if r.name == "latet.ee.oidc"]
    assert "single sign-on refused: the token endpoint answered 401 (unauthorized_client)" in logged, logged
    assert _people(cluster) == {}
    assert me(cluster.a)[0] == 401
