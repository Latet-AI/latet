"""Keycloak in front of a directory, as enterprise/sign-in.mdx's "Keycloak in front of Active Directory" says, tried with
OpenLDAP (the directory of test_openldap.py), not Active Directory: in the realm's User federation an LDAP provider whose
Connection URL begins `ldaps://`, the directory's certificate authority in Keycloak's truststore, a read-only bind
account, Edit mode READ_ONLY, the people's branch in Users DN and the user name attribute (OpenLDAP's `uid`, where
Active Directory's is `sAMAccountName`); on its Mappers tab a `group-ldap-mapper` with the groups' branch, Mode
READ_ONLY and Preserve group inheritance off. Set through Keycloak's admin REST API with the settings the page names.
Latet then connects to that realm as the page's Keycloak item says. Keycloak reaches the directory on a Docker network
of the two containers' own, `latet-prov-net`, by the name its certificate carries. Opt-in: LATET_PROVIDER_TESTS=1,
with quay.io/keycloak/keycloak:26.8.2 and debian:trixie-slim pulled by hand (README.md)."""

from __future__ import annotations

import pytest

from . import docker
from .conftest import PROVIDER_TESTS
from .test_keycloak import AtKeycloak, me, realm_file, refused, running_keycloak, set_up, sign_in, signed_in
from .test_openldap import KEYCLOAK, NETWORK_NAME, SUFFIX, running_directory

pytestmark = PROVIDER_TESTS

NETWORK = "latet-prov-net"


def _federate(keycloak, password: str) -> None:
    """The realm's User federation: the LDAP provider and its group mapper, each setting as the page names it."""
    with keycloak.admin() as api:
        realm = api.get("").json()
        provider = {
            "name": "acme-directory",
            "providerId": "ldap",
            "providerType": "org.keycloak.storage.UserStorageProvider",
            "parentId": realm["id"],
            "config": {
                "enabled": ["true"],
                "vendor": ["other"],  # OpenLDAP; the page's Active Directory is "ad"
                "connectionUrl": [f"ldaps://{NETWORK_NAME}:636"],  # Connection URL
                "startTls": ["false"],
                "useTruststoreSpi": ["always"],
                "authType": ["simple"],
                "bindDn": [KEYCLOAK],  # Bind DN
                "bindCredential": [password],  # Bind credentials
                "editMode": ["READ_ONLY"],  # Edit mode
                "usersDn": [f"ou=people,{SUFFIX}"],  # Users DN
                "usernameLDAPAttribute": ["uid"],  # Username LDAP attribute (sAMAccountName on Active Directory)
                "rdnLDAPAttribute": ["uid"],
                "uuidLDAPAttribute": ["entryUUID"],
                "userObjectClasses": ["inetOrgPerson"],
                "searchScope": ["1"],
                "importEnabled": ["true"],
                "syncRegistrations": ["false"],
            },
        }
        made = api.post("/components", json=provider)
        assert made.status_code == 201, made.text
        provider_id = made.headers["location"].rsplit("/", 1)[1]
        mapper = {
            "name": "groups",
            "providerId": "group-ldap-mapper",  # Add mapper, of the type group-ldap-mapper
            "providerType": "org.keycloak.storage.ldap.mappers.LDAPStorageMapper",
            "parentId": provider_id,
            "config": {
                "groups.dn": [f"ou=groups,{SUFFIX}"],  # LDAP groups DN
                "group.name.ldap.attribute": ["cn"],
                "group.object.classes": ["groupOfNames"],
                "membership.ldap.attribute": ["member"],
                "membership.attribute.type": ["DN"],
                "membership.user.ldap.attribute": ["uid"],
                "mode": ["READ_ONLY"],  # Mode
                "preserve.group.inheritance": ["false"],  # Preserve group inheritance
                # OpenLDAP keeps no LDAP_MATCHING_RULE_IN_CHAIN: the page's ..._RECURSIVELY is Active Directory's
                "user.roles.retrieve.strategy": ["LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"],
                "ignore.missing.groups": ["false"],
                "drop.non.existing.groups.during.sync": ["false"],
            },
        }
        made = api.post("/components", json=mapper)
        assert made.status_code == 201, made.text
        # With the vendor "other", Keycloak's own "first name" mapper reads `cn`, the whole name, where Active
        # Directory's reads givenName: Latet would name Emily "Emily Carter Carter". Pointed at givenName, as on AD.
        mappers = api.get("/components", params={"parent": provider_id}).json()
        [first] = [m for m in mappers if m["name"] == "first name"]
        first["config"]["ldap.attribute"] = ["givenName"]
        api.put(f"/components/{first['id']}", json=first).raise_for_status()


@pytest.fixture(scope="module")
def federated(tmp_path_factory):
    """OpenLDAP and a Keycloak whose realm has no users or groups of its own: its people come from the directory."""
    realm = realm_file()
    realm.pop("users")
    realm.pop("groups")
    docker._docker("network", "rm", NETWORK, check=False)
    docker._docker("network", "create", NETWORK)
    try:
        with running_directory(tmp_path_factory) as directory:
            docker._docker("network", "connect", "--alias", NETWORK_NAME, NETWORK, directory.container.name)
            with running_keycloak(
                tmp_path_factory, realm, name="keycloak-ldap", truststore=directory.authority.pem()
            ) as keycloak:
                docker._docker("network", "connect", NETWORK, keycloak.container.name)
                _federate(keycloak, directory.passwords["keycloak"])
                person = directory.passwords["person"]
                keycloak.passwords.update({uid: person for uid in ("emily.carter", "daniel.okafor", "priya.raman")})
                yield keycloak, directory
    finally:
        docker._docker("network", "rm", NETWORK, check=False)


TEAMS = {"Orion Payments": ("Orion Payments", None), "Halcyon Research": ("Halcyon Research", "auditor")}


def test_people_of_the_directory_sign_in_through_keycloak_with_their_directory_groups(
    cluster, federated, latet_server, tmp_path
):
    keycloak, _directory = federated
    tested = set_up(latet_server, keycloak, tmp_path, teams=TEAMS)
    assert tested.exit_code == 0, tested.output

    assert signed_in(sign_in(cluster, keycloak, "emily.carter"))  # her directory password, checked by Keycloak
    [emily] = cluster.server.identities.all(kind="person")
    # No e-mail: Keycloak's Trust Email is off, so its ID token says email_verified false, and Latet keeps none.
    assert (emily.name, emily.email) == ("Emily Carter", None)
    assert [g.name for g in cluster.people.groups_of(emily.id)] == ["Orion Payments"]
    assert cluster.people.account(emily.id).user_name == "emily.carter"  # the directory's uid

    cluster.a.cookies.clear()
    assert signed_in(sign_in(cluster, keycloak, "daniel.okafor", begin=cluster.b, back=cluster.a))
    assert me(cluster.a)[1]["edition"]["roles"] == ["auditor", "user"]  # Halcyon Research's role

    cluster.a.cookies.clear()
    assert refused(sign_in(cluster, keycloak, "priya.raman")) == "not-allowed"  # not in the directory's latet-users
    wrong = sign_in(cluster, keycloak, "emily.carter", password="not-her-password")
    assert isinstance(wrong, AtKeycloak), wrong
    print(f"A wrong directory password, at Keycloak: {wrong.said!r}")
    with keycloak.admin() as api:
        [user] = api.get("/users", params={"username": "emily.carter", "exact": "true"}).json()
        assert user.get("federationLink"), user  # Keycloak's user is the directory's
        groups = [g["path"] for g in api.get(f"/users/{user['id']}/groups").json()]
    print(f"Emily's groups at Keycloak, from the directory: {groups}")
    assert sorted(groups) == ["/Orion Payments", "/latet-users"]
