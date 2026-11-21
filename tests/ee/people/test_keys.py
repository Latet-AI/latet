"""API keys from the dashboard: made by administrators, controlled by people or groups, whose dashboard then works in
the key's space (ee/keys.py)."""

import asyncio
import itertools
import json
import logging
import threading
import time

import pytest

from latet.api.rest import COOKIE
from latet.config import LlmReview
from latet.ee import keys
from latet.ee.people import disable_person
from latet.ee.roles import Role
from latet.ee.server import publish
from latet.engine.stored import StoredVault
from latet.gateway.routes import Upstream
from latet.server.app import STREAMS_PER_IDENTITY
from latet.server.master import root_key
from latet.server.spaces import IdentityUnavailable, NotListening
from latet.server.tokens import route_key
from latet.server.vault import PgVaultStore

from ...e2e.fake_upstream import FakeUpstream
from ...unit.test_reviewer import FakeServer, reviewer
from .fake_provider import SSO
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
    on_trial,
    paused,
    settle,
    sign_in_as,
)

pytestmark = SERVER_TESTS


@pytest.fixture
def emily(cluster):
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    sign_in_as(cluster, emily)
    return emily


def _controlled_by_platform(cluster):
    """Billing CI, controlled by the group Platform, Daniel Okafor in it and signed in."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    cluster.people.add_member(platform.id, daniel.id, actor=BY_HAND)
    cluster.people.add_controller(key.id, platform.id, actor=BY_HAND)
    sign_in_as(cluster, daniel)
    return key, daniel, platform


def test_an_administrator_makes_a_key_shown_once_with_the_address_its_program_uses(cluster, emily):
    r = cluster.a.post(f"{API}/ee/keys", headers=UI, json={"name": "Billing CI"})
    assert r.status_code == 201
    made = r.json()
    assert made["secret"].startswith("latet_key_") and made["address"] == f"{URL}/u/{route_key(made['secret'])}/"
    assert cluster.server.identities.by_secret(made["secret"]).id == made["key"]["id"]
    listed = cluster.a.get(f"{API}/ee/keys").json()
    assert listed["manage"] is True and [key["name"] for key in listed["keys"]] == ["Billing CI"]
    assert made["secret"] not in json.dumps(listed)
    rotated = cluster.a.post(f"{API}/ee/keys/{made['key']['id']}/rotate", headers=UI).json()
    assert rotated["secret"] != made["secret"]
    assert cluster.server.identities.by_secret(rotated["secret"]).id == made["key"]["id"]
    entries = sorted(cluster.server.audit.query(actor=emily.id), key=lambda entry: entry.id)
    assert [entry.action for entry in entries] == ["key.added", "key.rotated"]


def test_only_administrators_make_keys_and_a_person_sees_only_the_keys_they_control(cluster):
    key, _daniel, _platform = _controlled_by_platform(cluster)
    cluster.server.identities.add_key("Nightly import")
    assert cluster.a.post(f"{API}/ee/keys", headers=UI, json={"name": "Mine"}).status_code == 403
    listed = cluster.a.get(f"{API}/ee/keys").json()
    assert listed["manage"] is False
    assert [(view["id"], view["name"], view["controllers"]) for view in listed["keys"]] == [(key.id, "Billing CI", [])]


def test_the_control_of_a_key_is_given_to_people_and_groups_and_shown_to_administrators(cluster, emily):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    cluster.a.post(f"{API}/ee/keys/{key.id}/controllers", headers=UI, json={"subject": "Platform"})
    answer = cluster.a.post(
        f"{API}/ee/keys/{key.id}/controllers", headers=UI, json={"subject": "emily.carter@acme.com"}
    )
    assert answer.json()["controllers"] == [
        {"kind": "group", "id": platform.id, "name": "Platform"},
        {"kind": "person", "id": emily.id, "name": "Emily Carter"},
    ]
    assert answer.json()["subject"] == {"kind": "person", "id": emily.id, "name": "Emily Carter"}  # whom it named
    gone = cluster.a.delete(f"{API}/ee/keys/{key.id}/controllers/{platform.id}", headers=UI).json()
    assert gone["controllers"] == [{"kind": "person", "id": emily.id, "name": "Emily Carter"}]
    listed = cluster.a.get(f"{API}/ee/keys").json()["keys"]
    assert listed[0]["controllers"] == [{"kind": "person", "id": emily.id, "name": "Emily Carter"}]
    actions = [entry.action for entry in cluster.server.audit.query(action="key.controller.*")]
    assert sorted(actions) == ["key.controller.added", "key.controller.added", "key.controller.removed"]


def test_a_controller_works_in_the_keys_space_as_the_key_and_a_change_there_is_in_the_log(cluster):
    key, daniel, _platform = _controlled_by_platform(cluster)
    space = f"{API}/ee/keys/{key.id}/space"
    me = cluster.b.get(f"{space}/me").json()
    assert me["server"]["identity"] == {"id": key.id, "name": "Billing CI", "kind": "key"}
    assert cluster.a.get(f"{space}/exchanges").status_code == 200
    assert cluster.a.get(f"{space}/config").status_code == 200
    assert cluster.a.delete(f"{space}/exchanges", headers=UI).json() == {"ok": True}
    [entry] = cluster.server.audit.query(action="key.space.changed")
    assert (entry.actor, entry.target, entry.details) == (daniel.id, key.id, {"method": "DELETE", "path": "/exchanges"})


def test_a_person_who_does_not_control_the_key_does_not_open_its_space(cluster):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    sign_in_as(cluster, add_person(cluster, "Priya Shah", "priya.shah@acme.com"))
    refused = cluster.a.get(f"{API}/ee/keys/{key.id}/space/me")
    assert refused.status_code == 403 and refused.json()["space"] == "closed"
    nobody = cluster.a.get(f"{API}/ee/keys/u_abcdefghij/space/me")  # no such key: told as one she does not control
    assert nobody.status_code == 403 and "u_abcdefghij" not in nobody.text


def test_control_taken_away_closes_the_space_at_the_next_request_on_the_other_instance(cluster):
    key, daniel, platform = _controlled_by_platform(cluster)
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/me").status_code == 200
    cluster.people.remove_member(platform.id, daniel.id, actor=BY_HAND)
    refused = cluster.b.get(f"{space}/me")
    assert refused.status_code == 403 and refused.json()["space"] == "closed"


def test_an_administrator_opens_any_keys_space_but_not_a_disabled_ones(cluster, emily):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/me").status_code == 200
    assert cluster.a.post(f"{API}/ee/keys/{key.id}/disable", headers=UI).json() == {"ok": True}
    refused = cluster.a.get(f"{space}/me")
    assert refused.status_code == 409 and refused.json() == {
        "error": "this API key is disabled: an administrator enables it again first",
        "space": "closed",
    }
    assert cluster.a.post(f"{API}/ee/keys/{key.id}/enable", headers=UI).json() == {"ok": True}
    assert cluster.a.get(f"{space}/me").status_code == 200
    actions = [entry.action for entry in cluster.server.audit.query(actor=emily.id)]
    assert sorted(actions) == ["key.disabled", "key.enabled"]


def test_a_keys_space_reaches_none_of_the_persons_enterprise_pages(cluster, emily):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/ee/people").status_code == 403  # in the key's space the key asks: it has no role
    assert cluster.a.get(f"{space}/me").json()["edition"]["pages"] == []
    # The routes that let anyone signed in through are a person's, never a key's (Task 3's carry).
    for path in ("ee/keys", "ee/policy", f"ee/keys/{key.id}/space/me", "ee/license"):
        refused = cluster.a.get(f"{space}/{path}")
        assert refused.status_code == 403 and refused.json() == {
            "error": "an API key's space reaches none of the Enterprise pages"
        }, path
    assert cluster.a.post(f"{space}/ee/keys", headers=UI, json={"name": "Mine"}).status_code == 403
    assert [key.name for key in cluster.server.identities.all(kind="key")] == ["Billing CI"]
    assert cluster.server.audit.query(action="key.space.changed") == []  # refused there: nothing was changed


def test_once_a_trial_ended_a_keys_space_closes_and_while_paused_it_opens(cluster):
    on_trial(cluster)
    key, daniel, _platform = _controlled_by_platform(cluster)
    end_trial(cluster)
    refused = cluster.a.get(f"{API}/ee/keys/{key.id}/space/me")
    assert refused.status_code == 403 and (refused.json()["code"], refused.json()["space"]) == ("trial-ended", "closed")
    assert cluster.a.get(f"{API}/ee/keys").json()["keys"][0]["id"] == key.id  # still listed: read, not opened
    paused(cluster)
    sign_in_as(cluster, daniel)
    assert cluster.a.get(f"{API}/ee/keys/{key.id}/space/me").status_code == 200


# --- a key's tokens, each with what it may do (Latet 0.10.0) ----------------------------------------------------------


def test_a_rotation_in_the_dashboard_keeps_what_the_token_may_do_and_names_none_of_several(cluster, emily):
    """0.10.0's tokens: the dashboard rotates the key's token as `latet server keys rotate` does, keeping its name,
    what it may do and whether it serves agents (never a token that may do more); a key with several has the one meant
    rotated there (409 here, nothing done), and the entry names the token that stands and the one it replaced."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    [first] = cluster.server.identities.tokens(key.id)
    rotated = cluster.a.post(f"{API}/ee/keys/{key.id}/rotate", headers=UI)
    assert rotated.status_code == 200 and rotated.json()["address"].startswith(f"{URL}/u/")
    [now] = cluster.server.identities.tokens(key.id)
    assert now.id != first.id and (now.label, now.permissions, now.agents) == (first.label, first.permissions, True)
    [entry] = cluster.server.audit.query(action="key.rotated")
    assert entry.details == {"token": now.id, "replaced": first.id}
    with cluster.server.identities.changing() as changes:
        cluster.server.identities.add_token(key.id, "Reports", ["scan"], within=changes)
    several = cluster.a.post(f"{API}/ee/keys/{key.id}/rotate", headers=UI)
    assert several.status_code == 409 and "latet server keys rotate --token" in several.json()["error"]
    assert {t.id for t in cluster.server.identities.tokens(key.id)} >= {now.id}
    assert len(cluster.server.audit.query(action="key.rotated")) == 1
    cluster.server.identities.revoke_one(key.id, now.id)
    [scan] = cluster.server.identities.tokens(key.id)
    alone = cluster.a.post(f"{API}/ee/keys/{key.id}/rotate", headers=UI).json()
    assert alone["address"] is None  # a token added to the key serves no agents, and keeps scan alone
    [kept] = cluster.server.identities.tokens(key.id)
    assert kept.id != scan.id and kept.permissions == frozenset({"scan"}) and not kept.agents


# --- a key's vault in its space: PostgreSQL's, never the working set in memory (Latet 0.10.0's key vaults) -----------

ADMIN = "/_latet/server/v1/admin"
APPS = "/_latet/apps/v1"
IBAN = "GB82WEST12345698765432"
MAILS = ("emily.carter@acme.com", "daniel.okafor@acme.com")


def test_a_keys_vault_in_its_space_is_postgresqls_and_a_value_forgotten_anywhere_is_never_revealed(
    cluster, monkeypatch
):
    """0.10.0 keeps an API key's vault in PostgreSQL and a working set of it in each instance's memory: the Vault page
    of the key's space pages, counts and searches PostgreSQL's (never memory's), and a value is revealed as PostgreSQL
    holds it now. A value forgotten by `latet server keys forget`, or on an instance whose message this one has not
    heard yet, is never revealed; the administration log has the forget as the server records it and each request of
    the key's space as E3 records it, by its route's template."""
    key, secret = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.add_controller(key.id, daniel.id, actor=BY_HAND)
    sign_in_as(cluster, daniel)
    text = f"write to {MAILS[0]} or {MAILS[1]}, IBAN {IBAN}"
    masked = cluster.a.post(f"{APPS}/mask", json={"texts": [text]}, headers={"authorization": f"Bearer {secret}"})
    assert masked.status_code == 200, masked.text
    space = f"{API}/ee/keys/{key.id}/space"
    vault = cluster.server.spaces._spaces[key.id].runtime.built.vault
    assert isinstance(vault, StoredVault)
    held = {e.original: e for e in vault.entities()}
    assert set(held) == {*MAILS, IBAN, "acme.com"}  # the addresses bring their domain along
    vault.entities = lambda: []  # the page never reads the working set in memory: here it would find nothing
    page = cluster.a.get(f"{space}/vault").json()
    assert page["total"] == 4 and page["types"] == {"EMAIL": 2, "IBAN": 1, "DOMAIN": 1}
    assert {item["id"] for item in page["items"]} == {e.id for e in held.values()}
    # A search finds a value by the word the vault finds it by (an address's local part), never by reading the whole
    # vault: a part of that word alone finds nothing.
    found = cluster.b.get(f"{space}/vault", params={"q": "Daniel.Okafor"}).json()
    assert [item["id"] for item in found["items"]] == [held[MAILS[1]].id] and found["total"] == 1
    assert cluster.b.get(f"{space}/vault", params={"q": MAILS[1]}).json()["total"] == 1  # the value itself
    assert cluster.b.get(f"{space}/vault", params={"q": "okafor"}).json()["total"] == 0
    # A search is bounded: 200 characters, and the values its words open (SEARCHED_AT_MOST), else 400 in fixed words.
    too_long = cluster.b.get(f"{space}/vault", params={"q": "x" * 201})
    assert too_long.status_code == 400 and "x" * 20 not in too_long.text
    with monkeypatch.context() as bounded:
        bounded.setattr("latet.engine.stored.SEARCHED_AT_MOST", 1)
        too_many = cluster.b.get(f"{space}/vault", params={"q": " ".join(MAILS)})
        assert (too_many.status_code, too_many.json()) == (
            400,
            {"error": "this search names too many of the vault's values: search for a longer one"},
        )
    assert cluster.b.get(f"{space}/vault", params={"q": "iban"}).json()["total"] == 1  # its type
    assert cluster.b.get(f"{space}/vault", params={"q": held[IBAN].id}).json()["total"] == 1  # its id
    reveal = cluster.b.post(f"{space}/vault/{held[MAILS[1]].id}/reveal", headers=UI)
    assert reveal.json() == {"original": MAILS[1]}
    # Forgotten as another instance forgets it, its message not heard here yet: still in A's memory, never revealed.
    store = PgVaultStore(cluster.server.db, key.id, root_key(cluster.server.db, cluster.server.master, key.id))
    assert store.forget_values([IBAN]).ids == [held[IBAN].id]
    assert vault.in_memory(held[IBAN].id) is not None
    gone = cluster.a.post(f"{space}/vault/{held[IBAN].id}/reveal", headers=UI)
    assert (gone.status_code, gone.json()) == (404, {"error": "not found"})
    assert cluster.a.get(f"{space}/vault").json()["total"] == 3
    # Forgotten by its administrator, on instance B: revealed on neither.
    forgot = cluster.b.post(
        f"{ADMIN}/identities/{key.id}/forget", json={"values": [MAILS[0]]}, headers=as_admin(cluster)
    )
    assert forgot.json() == {"forgotten": 1, "unknown": 0}
    for client in (cluster.a, cluster.b):
        assert client.post(f"{space}/vault/{held[MAILS[0]].id}/reveal", headers=UI).status_code == 404
    [entry] = cluster.server.audit.query(action="key.forgotten")
    assert entry.actor.startswith("admin:") and entry.target == key.id and MAILS[0] not in repr(entry)
    # The key's history cleared in its space, then a forget by the applications API's rules again: each holds.
    assert cluster.a.delete(f"{space}/exchanges", headers=UI).json() == {"ok": True}
    again = cluster.a.post(
        f"{ADMIN}/identities/{key.id}/forget", json={"values": [MAILS[1]]}, headers=as_admin(cluster)
    )
    assert again.json() == {"forgotten": 1, "unknown": 0}
    assert cluster.b.get(f"{space}/vault").json()["types"] == {"DOMAIN": 1}
    assert len(cluster.server.audit.query(action="key.forgotten")) == 2
    recorded = sorted(cluster.server.audit.query(action="key.space.changed"), key=lambda e: e.id)
    assert len(recorded) == 5 and {(e.actor, e.target) for e in recorded} == {(daniel.id, key.id)}
    assert all(e.details == {"method": "POST", "path": "/vault/***/reveal"} for e in recorded[:4])
    assert recorded[4].details == {"method": "DELETE", "path": "/exchanges"}
    assert not any(value in repr(recorded) for value in (*MAILS, IBAN))


def _rows(cluster, key_id):
    """How many rows the key's history and its local reviewer's remembered answers have."""
    with cluster.server.db.connection() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE identity_id = %s", (key_id,)).fetchone()[0]
            for table in ("exchanges", "blocks", "reviews")
        }


@pytest.fixture
def upstream(monkeypatch):
    """OpenAI's route of every space built in the test leads to a stub model on 127.0.0.1 that says back what it got."""
    fake = FakeUpstream().start()
    monkeypatch.setattr("latet.gateway.app.DEFAULT_UPSTREAMS", (Upstream("openai", "/openai", fake.url),))
    yield fake
    fake.stop()


def test_a_forget_in_a_keys_space_forgets_as_latet_server_keys_forget_does(cluster, upstream):
    """A value of a key's vault forgotten from its space (the person's Forget) goes as `latet server keys forget`
    takes it: with the rows of the key's history that name it and the local reviewer's remembered answer that found it,
    so that the text that held it brings it back with no remembered answer and the vault gives it a new entry (a new
    generation); `key.forgotten` by the person, in the forget's own transaction, names no value, and the space records
    no change of its own for it."""
    key, secret = cluster.server.identities.add_key("Billing CI")
    daniel = add_person(cluster, "Daniel Okafor", "daniel.okafor@acme.com")
    cluster.people.add_controller(key.id, daniel.id, actor=BY_HAND)
    sign_in_as(cluster, daniel)
    bearer = {"authorization": f"Bearer {secret}"}
    text = "mia moglie Maria ha la chemio giovedì"
    assert cluster.a.post(f"{APPS}/mask", json={"texts": ["build the space"]}, headers=bearer).status_code == 200
    runtime = cluster.server.spaces._spaces[key.id].runtime
    runtime.mediator.llm_review = LlmReview(enabled=True, model="fake-model")
    runtime.mediator.reviewer = reviewer(FakeServer(), runtime.mediator.llm_review, memory=runtime.store)
    assert "Maria" not in cluster.a.post(f"{APPS}/mask", json={"texts": [text]}, headers=bearer).text
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"{text}: write to Maria"}]}
    turn = cluster.a.post(
        f"/u/{route_key(secret)}/openai/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-x"}
    )
    assert turn.status_code == 200 and "Maria" in turn.text  # restored in the answer
    assert runtime.recorder is not None and runtime.recorder.drain(timeout=10)
    store = PgVaultStore(cluster.server.db, key.id, root_key(cluster.server.db, cluster.server.master, key.id))
    maria = store.by_original("Maria")
    assert maria is not None
    before = _rows(cluster, key.id)
    assert before["exchanges"] >= 1 and before["reviews"] >= 1
    space = f"{API}/ee/keys/{key.id}/space"
    [exchange] = cluster.a.get(f"{space}/exchanges").json()["items"]
    assert cluster.a.get(f"{space}/exchanges/{exchange['id']}").status_code == 200
    assert cluster.a.delete(f"{space}/vault/{maria.id}", headers=UI).json() == {"ok": True}
    after = _rows(cluster, key.id)
    assert after["exchanges"] < before["exchanges"] and after["reviews"] < before["reviews"]
    assert cluster.a.get(f"{space}/exchanges/{exchange['id']}").status_code == 404  # its row named the value
    for client in (cluster.a, cluster.b):
        assert client.post(f"{space}/vault/{maria.id}/reveal", headers=UI).status_code == 404
    [entry] = cluster.server.audit.query(action="key.forgotten")
    assert (entry.actor, entry.target) == (daniel.id, key.id)
    assert entry.details == {"values": 1, "forgotten": 1, "unknown": 0, "source": "space"}
    assert "Maria" not in repr(entry) and maria.id not in repr(entry)
    changed = [e.details for e in cluster.server.audit.query(action="key.space.changed")]
    assert changed == [{"method": "POST", "path": "/vault/***/reveal"}] * 2  # the forget: with the change, not before
    assert "Maria" not in cluster.a.post(f"{APPS}/mask", json={"texts": [text]}, headers=bearer).text
    again = store.by_original("Maria")
    assert again is not None and again.id != maria.id and again.surrogate != maria.surrogate  # a new generation


# --- what the administration log and the request log keep of a key's space (F7, F8) --------------------------------


def test_a_change_in_a_keys_space_is_recorded_by_its_routes_template_never_an_id(cluster):
    """F7: a project's id is a hash of its folder, which a list of usual folders gives back; a value's id names it."""
    key, daniel, _platform = _controlled_by_platform(cluster)
    space = f"{API}/ee/keys/{key.id}/space"
    project = "3f2a9c1e5b7d4a60"
    cluster.a.put(f"{space}/projects/{project}/personal", headers=UI, json={"rules": {}})
    # A value forgotten is recorded by the key's app, in the forget's transaction (`key.forgotten`): none here, 404.
    assert cluster.a.delete(f"{space}/vault/e_7k2mq4", headers=UI).status_code == 404
    entries = sorted(cluster.server.audit.query(action="key.space.changed"), key=lambda entry: entry.id)
    assert [entry.details for entry in entries] == [{"method": "PUT", "path": "/projects/***/personal"}]
    assert all(entry.actor == daniel.id and entry.target == key.id for entry in entries)
    assert all(said not in repr(entries) for said in (project, "e_7k2mq4"))
    assert cluster.server.audit.query(action="key.forgotten") == []


def test_a_path_no_route_of_the_dashboard_serves_is_answered_here_and_neither_recorded_nor_handed_on(
    cluster, monkeypatch
):
    """Fix round 1: what reaches only the dashboard's catch-all changes nothing in the key's space."""
    key, _daniel, _platform = _controlled_by_platform(cluster)
    space = f"{API}/ee/keys/{key.id}/space"
    spaces, handed = cluster.server.spaces, []
    built = spaces.app

    async def watched(identity):
        app = await built(identity)
        if identity.id != key.id:
            return app

        async def handed_on(scope, receive, send):
            handed.append((scope["method"], scope["path"]))
            await app(scope, receive, send)

        return handed_on

    monkeypatch.setattr(spaces, "app", watched)
    for unknown in (
        cluster.a.post(f"{space}/approvals/b_91xz/images/0", headers=UI),  # that route is GET's alone
        cluster.a.delete(f"{space}/b_91xz/forget", headers=UI),
        cluster.a.get(f"{space}/b_91xz"),
    ):
        assert (unknown.status_code, unknown.json()) == (404, {"error": "not found"})
        assert unknown.headers["cache-control"] == "no-store"
    assert handed == [] and cluster.server.audit.query(action="key.space.changed") == []
    assert cluster.a.post(f"{space}/terms", headers=UI, json={"value": "Project Heron"}).status_code == 200
    assert handed == [("POST", f"{API}/terms")]
    [entry] = cluster.server.audit.query(action="key.space.changed")
    assert entry.details == {"method": "POST", "path": "/terms"}


def test_a_change_the_dashboards_guard_refuses_is_not_recorded(cluster):
    """F8: the entry is written only once the dashboard's guard (its header for a change) lets the request through."""
    key, _daniel, _platform = _controlled_by_platform(cluster)
    refused = cluster.a.delete(f"{API}/ee/keys/{key.id}/space/exchanges")  # no X-Latet-UI
    assert refused.status_code == 403 and refused.json() == {"error": "missing X-Latet-UI header"}
    assert cluster.server.audit.query(action="key.space.changed") == []


@pytest.mark.parametrize(
    ("raised", "status", "error"),
    [
        (NotListening("not heard"), 503, "this API key's space is not available here now: try again in a moment"),
        (IdentityUnavailable("gone"), 409, "this API key is disabled or gone"),
    ],
)
def test_a_keys_space_that_cannot_be_built_closes_and_records_nothing(cluster, monkeypatch, raised, status, error):
    """F8: the key's app is built first; what building it raises is answered, the space closed, nothing recorded."""
    key, _daniel, _platform = _controlled_by_platform(cluster)
    spaces = cluster.server.spaces
    built = spaces.app

    async def unbuilt(identity):
        if identity.id == key.id:
            raise raised
        return await built(identity)

    monkeypatch.setattr(spaces, "app", unbuilt)
    answer = cluster.a.delete(f"{API}/ee/keys/{key.id}/space/exchanges", headers=UI)
    assert (answer.status_code, answer.json()) == (status, {"error": error, "space": "closed"})
    assert answer.headers["cache-control"] == "no-store"
    assert cluster.server.audit.query(action="key.space.changed") == []


def test_a_change_whose_entry_cannot_be_written_is_not_handed_to_the_keys_space(cluster, monkeypatch):
    key, _daniel, _platform = _controlled_by_platform(cluster)
    space = f"{API}/ee/keys/{key.id}/space"
    record = cluster.server.audit.record

    def refusing(actor, action, *args, **kwargs):
        if action == "key.space.changed":
            raise RuntimeError("the administration log at 10.0.0.5 does not take it")
        return record(actor, action, *args, **kwargs)

    assert cluster.a.post(f"{space}/terms", headers=UI, json={"value": "Project Heron"}).status_code == 200
    monkeypatch.setattr(cluster.server.audit, "record", refusing)
    answer = cluster.a.post(f"{space}/terms", headers=UI, json={"value": "Project Falcon"})
    assert answer.status_code == 503 and "10.0.0.5" not in answer.text
    config = json.dumps(cluster.a.get(f"{space}/config").json())
    assert "Project Heron" in config and "Project Falcon" not in config


# --- the routes' words, and the answers read in a change's transaction ------------------------------------------------


def test_a_path_names_a_key_and_a_controller_by_its_id_alone_and_no_refusal_quotes_what_was_sent(cluster, emily):
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    cluster.people.add_controller(key.id, platform.id, actor=BY_HAND)
    for refused in (
        cluster.a.post(f"{API}/ee/keys/Billing CI/rotate", headers=UI),
        cluster.a.post(f"{API}/ee/keys/u_abcdefghij/disable", headers=UI),
        cluster.a.delete(f"{API}/ee/keys/{key.id}/controllers/Platform", headers=UI),
        cluster.a.post(f"{API}/ee/keys/{key.id}/controllers", headers=UI, json={"subject": "nobody-4f7c1e"}),
        cluster.a.delete(f"{ADMIN_EE}/keys/{key.id}/controllers/{emily.id}", headers=as_admin(cluster)),
    ):
        assert refused.status_code == 404, refused.text
        assert all(said not in refused.text for said in ("Billing", "u_abcdefghij", "Platform", "nobody", emily.id))
    assert cluster.people.controllers(key.id) == [platform.id]
    for body in ({"subject": None}, {"subject": ["x"]}, {}, {"subject": key.id}):
        answer = cluster.a.post(f"{API}/ee/keys/{key.id}/controllers", headers=UI, json=body)
        assert answer.status_code in (400, 404) and key.id not in answer.text  # a key controls no key
    for name in (None, "", " ", "x" * 201, "\N{RIGHT-TO-LEFT OVERRIDE}"):
        assert cluster.a.post(f"{API}/ee/keys", headers=UI, json={"name": name}).status_code == 400
    assert [key.name for key in cluster.server.identities.all(kind="key")] == ["Billing CI"]


def test_what_a_controllers_change_answers_is_read_in_its_transaction(cluster, emily, monkeypatch):
    """Task 8's rule: a change whose answer cannot be read is not made."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    controllers = cluster.people.controllers

    def unread(*args, **kwargs):
        raise RuntimeError("not read")

    with monkeypatch.context() as patched:
        patched.setattr(cluster.people, "controllers", unread)
        added = cluster.a.post(f"{API}/ee/keys/{key.id}/controllers", headers=UI, json={"subject": platform.id})
    assert added.status_code == 500 and controllers(key.id) == []
    cluster.people.add_controller(key.id, platform.id, actor=BY_HAND)
    with monkeypatch.context() as patched:
        patched.setattr(cluster.people, "controllers", unread)
        removed = cluster.a.delete(f"{API}/ee/keys/{key.id}/controllers/{platform.id}", headers=UI)
    assert removed.status_code == 500 and controllers(key.id) == [platform.id]
    assert cluster.server.audit.query(action="key.controller.*", actor=emily.id) == []


def test_latet_server_lists_every_key_with_its_controllers_and_gives_their_control(cluster):
    """L2: the administration's list is asked as the administration (every action), never as no one."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    platform = cluster.people.add_group("Platform", actor=BY_HAND)
    given = cluster.a.post(
        f"{ADMIN_EE}/keys/{key.id}/controllers", headers=as_admin(cluster), json={"subject": "Platform"}
    )
    assert given.status_code == 200, given.text
    listed = cluster.a.get(f"{ADMIN_EE}/keys", headers=as_admin(cluster)).json()
    assert listed["manage"] is True
    assert listed["keys"][0]["controllers"] == [{"kind": "group", "id": platform.id, "name": "Platform"}]
    [entry] = cluster.server.audit.query(action="key.controller.added")
    assert entry.actor.startswith("admin:") and entry.target == key.id


def test_a_keys_space_is_no_company_wide_change_while_the_control_of_a_key_is(cluster):
    """O1: while the server asks for the company sign-in, a session `latet dashboard` opened still works in a key's
    space (a controller's work as the user role), and gives no control of a key."""
    key, daniel, _platform = _controlled_by_platform(cluster)
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    cluster.server.edition.settings_store.set_section("sso", {**SSO, "company_changes": "sso"}, actor=BY_HAND)
    settle(lambda: all(server.edition.settings().sso is not None for server in cluster.servers))
    sign_in_as(cluster, daniel)
    assert cluster.a.delete(f"{API}/ee/keys/{key.id}/space/exchanges", headers=UI).json() == {"ok": True}
    sign_in_as(cluster, emily)
    refused = cluster.a.post(f"{API}/ee/keys/{key.id}/controllers", headers=UI, json={"subject": emily.id})
    assert refused.status_code == 403 and refused.json()["code"] == "company-sign-in"


# --- an administrator's way into a key's space is a company-wide act (the final review's F1) --------------------------

BY_COMPANY_SIGN_IN = {
    "error": "sign in with your company account to open the space of an API key you do not control",
    "code": "company-sign-in",
    "space": "closed",
}


def _session(cluster, person, via):
    """The person's dashboard session, opened as ``via`` says: `latet dashboard`'s one-time code (``code``), or the
    company's single sign-on (``sso``)."""
    cluster.a.cookies.set(COOKIE, cluster.server.sign_ins.open(person.id, via=via), domain=HOST, path="/_latet")


def _company_changes(cluster, value):
    """The sign-in settings with ``company_changes`` set to ``value``, known to both instances."""
    cluster.server.edition.settings_store.set_section("sso", {**SSO, "company_changes": value}, actor=BY_HAND)

    def known(server):
        sso = server.edition.settings().sso
        return sso is not None and sso.company_changes == value

    settle(lambda: all(known(server) for server in cluster.servers))


def _builds(cluster, monkeypatch, key):
    """Each build of the key's app, on either instance: what a request refused before it never reaches."""
    builds: list[str] = []
    for server in cluster.servers:
        spaces = server.spaces

        async def noted(identity, built=spaces.app):
            if identity.id == key.id:
                builds.append(identity.id)
            return await built(identity)

        monkeypatch.setattr(spaces, "app", noted)
    return builds


def test_an_administrators_code_session_opens_no_keys_space_they_do_not_control_while_the_company_sign_in_is_asked(
    cluster, monkeypatch
):
    """F1: through keys.manage alone, a key's space shows every key's history with its originals restored and its
    vault: a company-wide act, which a session `latet dashboard`'s one-time code opened (an agent at the administrator's
    computer can ask for one) does not make while company_changes is sso. Nothing of the key's is built, read or
    recorded; the company sign-in's session of the same administrator opens it."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    _company_changes(cluster, "sso")
    builds = _builds(cluster, monkeypatch, key)
    _session(cluster, emily, "code")
    space = f"{API}/ee/keys/{key.id}/space"
    for refused in (
        cluster.a.get(f"{space}/exchanges"),
        cluster.b.get(f"{space}/vault"),
        cluster.a.get(f"{space}/config"),
        cluster.a.delete(f"{space}/exchanges", headers=UI),
        cluster.b.post(f"{space}/vault/e_7k2mq4/reveal", headers=UI),
    ):
        assert (refused.status_code, refused.json()) == (403, BY_COMPANY_SIGN_IN)
        assert refused.headers["cache-control"] == "no-store"
    assert builds == [] and cluster.server.audit.query(action="key.space.changed") == []
    assert cluster.a.get(f"{API}/ee/keys").json()["manage"] is True  # the keys are listed still: their spaces closed
    _session(cluster, emily, "sso")
    assert cluster.b.get(f"{space}/exchanges").status_code == 200
    assert cluster.a.delete(f"{space}/exchanges", headers=UI).json() == {"ok": True}
    [entry] = cluster.server.audit.query(action="key.space.changed")
    assert (entry.actor, entry.target) == (emily.id, key.id)


@pytest.mark.parametrize("through", ["themselves", "a group"])
def test_an_administrator_who_controls_the_key_works_in_its_space_from_a_code_session(cluster, through):
    """O1 kept: a controller's way in is their own work as the user role, an administrator's too, whatever the session."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    if through == "themselves":
        cluster.people.add_controller(key.id, emily.id, actor=BY_HAND)
    else:
        platform = cluster.people.add_group("Platform", actor=BY_HAND)
        cluster.people.add_member(platform.id, emily.id, actor=BY_HAND)
        cluster.people.add_controller(key.id, platform.id, actor=BY_HAND)
    _company_changes(cluster, "sso")
    _session(cluster, emily, "code")
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/exchanges").status_code == 200
    assert cluster.b.delete(f"{space}/exchanges", headers=UI).json() == {"ok": True}


@pytest.mark.parametrize("setting", ["any-session", None])
def test_an_administrators_code_session_opens_any_keys_space_where_the_company_sign_in_is_not_asked(cluster, setting):
    """company_changes: any-session, or no single sign-on set up at all (no such setting): any administrator's session."""
    key, _secret = cluster.server.identities.add_key("Billing CI")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    if setting is not None:
        _company_changes(cluster, setting)
    _session(cluster, emily, "code")
    space = f"{API}/ee/keys/{key.id}/space"
    assert cluster.a.get(f"{space}/exchanges").status_code == 200
    assert cluster.b.delete(f"{space}/exchanges", headers=UI).json() == {"ok": True}


def test_an_administrators_stream_in_a_keys_space_ends_once_the_company_sign_in_is_asked(cluster, monkeypatch):
    """The stream's watch asks again as the request came: from the same session, under the settings as PostgreSQL has
    them now (O2 with F1)."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 0.2)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    _company_changes(cluster, "any-session")
    _session(cluster, emily, "code")
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        _company_changes(cluster, "sso")
        assert ended.wait(5), "the stream did not end"
    finally:
        task.cancel()
    assert task.exception(timeout=5) is None


def test_a_streams_session_is_weighed_at_once_when_the_sign_in_settings_change_through_their_route(
    cluster, monkeypatch
):
    """M4 (E4's Task 10 review): the sign-in's change names `sso`, which every instance hears on ee:people, so a stream
    open in a key's space on the other instance asks again at once, whatever the clock (the checks every
    WATCH_SECONDS put far off)."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 3600.0)
    key, _secret = cluster.server.identities.add_key("Billing CI")
    emily = add_person(cluster, "Emily Carter", "emily.carter@acme.com", Role.ADMIN)
    _company_changes(cluster, "any-session")
    _session(cluster, emily, "code")
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        body = {"sso": {**SSO, "company_changes": "sso"}}
        changed = cluster.a.put(
            f"{ADMIN_EE}/sign-in", headers=as_admin(cluster), json=body
        )  # `latet server sign-in set`
        assert changed.status_code == 200, changed.text
        assert ended.wait(5), "the stream did not end"
    finally:
        task.cancel()
    assert task.exception(timeout=5) is None


# --- a stream open in a key's space (O2) -----------------------------------------------------------------------------


def _streaming(client, key, path=None):
    """The Live Reviewer's stream of the key's space (or ``path``), opened as a dashboard keeps one open on
    ``client``'s instance, with the session the browser holds: what it was sent, an event set once it ended, and its
    task (cancelled by the test, should it not end)."""
    path = path or f"{API}/ee/keys/{key.id}/space/approvals/stream"
    session = client.cookies.get(COOKIE, domain=HOST, path="/_latet")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", HOST.encode()), (b"cookie", f"{COOKIE}={session}".encode())],
        "client": ("203.0.113.7", 50000), "server": (HOST, 443),
    }  # fmt: skip
    sent: list[dict] = []
    ended = threading.Event()
    asked = []

    async def receive():
        if not asked:
            asked.append(True)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # the viewer stays
        raise AssertionError("unreachable")

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            ended.set()

    return sent, ended, client.portal.start_task_soon(client.app, scope, receive, send)


def _open(sent, ended):
    settle(lambda: any(message["type"] == "http.response.body" and message.get("body") for message in list(sent)))
    assert not ended.is_set()
    [start] = [message for message in sent if message["type"] == "http.response.start"]
    assert start["status"] == 200


@pytest.mark.parametrize(
    "lost", ["control", "disabled", "disabled-alone", "key-disabled", "key-removed", "controller-removed"]
)
def test_a_stream_open_in_a_keys_space_ends_once_a_change_heard_takes_the_space_away(cluster, monkeypatch, lost):
    """O2 (M1): the change is heard on the other instance (ee:people, identities) and the stream there ends within
    ASKED_AT_MOST_EVERY, whatever the clock (the checks every WATCH_SECONDS put far off). Gate 4 (M1): each listener
    forgets what changed first, then wakes the streams, so a key disabled or removed (the identities message alone) and
    a person's own control taken away (their generation, and ee:people naming them) end it as a group's change does."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 3600.0)
    key, daniel, platform = _controlled_by_platform(cluster)
    if lost == "controller-removed":  # Daniel's own control, not the group's
        cluster.people.remove_member(platform.id, daniel.id, actor=BY_HAND)
        cluster.people.add_controller(key.id, daniel.id, actor=BY_HAND)
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        if lost == "control":
            cluster.people.remove_member(platform.id, daniel.id, actor=BY_HAND)  # through instance A
        elif lost == "disabled":
            disable_person(cluster.people, daniel.id, actor=BY_HAND)
        elif lost == "disabled-alone":
            cluster.server.identities.disable(daniel.id)  # E0b's, told on `identities` alone: no change of People's
        elif lost == "key-disabled":
            cluster.server.identities.disable(key.id)  # E0b's, told on `identities` alone
        elif lost == "key-removed":
            cluster.server.identities.remove(key.id)
        else:
            cluster.people.remove_controller(key.id, daniel.id, actor=BY_HAND)
        assert ended.wait(keys.ASKED_AT_MOST_EVERY + 2), "the stream did not end"
    finally:
        task.cancel()
    if lost not in ("key-disabled", "key-removed"):  # those close the key's whole space too (E0b), which ends its body
        assert task.exception(timeout=5) is None  # ended there, not cancelled here


def test_a_stream_open_in_a_keys_space_ends_within_the_bound_once_a_trial_ended(cluster, monkeypatch):
    """O2: a trial's end is heard by no message: the stream asks again every WATCH_SECONDS."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 0.2)
    on_trial(cluster)
    key, _daniel, _platform = _controlled_by_platform(cluster)
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        end_trial(cluster)
        assert ended.wait(5), "the stream did not end"
    finally:
        task.cancel()
    assert task.exception(timeout=5) is None


def test_a_stream_open_in_a_keys_space_stays_while_nothing_takes_it_away(cluster, monkeypatch):
    monkeypatch.setattr(keys, "WATCH_SECONDS", 0.2)
    key, _daniel, _platform = _controlled_by_platform(cluster)
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        cluster.people.add_group("Payments", actor=BY_HAND)  # a change heard, which takes nothing away
        assert not ended.wait(1)
    finally:
        task.cancel()


def test_a_persons_streams_in_a_keys_space_count_with_their_own_under_one_cap(cluster, monkeypatch):
    """Fix round 1: a stream of a key's space is the person's, held and counted by the server's gate as their own
    streams are (STREAMS_PER_IDENTITY on an instance, then 429), not as one of their ordinary requests."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 3600.0)
    key, _daniel, _platform = _controlled_by_platform(cluster)
    own, in_space = f"{API}/approvals/stream", f"{API}/ee/keys/{key.id}/space/approvals/stream"
    half = STREAMS_PER_IDENTITY // 2
    paths = [own] * half + [in_space] * (STREAMS_PER_IDENTITY - half)
    opened = [_streaming(cluster.b, key, path) for path in paths]
    try:
        for sent, ended, _task in opened:
            _open(sent, ended)
        for path in (in_space, own):
            sent, ended, task = _streaming(cluster.b, key, path)
            opened.append((sent, ended, task))
            assert ended.wait(5), f"{path} was not refused"
            [start] = [message for message in sent if message["type"] == "http.response.start"]
            body = json.loads(b"".join(message.get("body", b"") for message in sent if "body" in message))
            assert start["status"] == 429 and "dashboard streams" in body["error"], path
    finally:
        for *_, task in opened:
            task.cancel()


def test_a_burst_of_changes_heard_asks_a_stream_in_a_keys_space_again_once_a_second_at_most(cluster, monkeypatch):
    """Fix round 1: the messages of a SCIM burst wake the stream's watch each time; it asks again (People, PostgreSQL,
    the dashboard's two threads) once a second at most. The times are taken where the watch asks (``_lost``, on its
    event loop), never where the check runs in the pool, which a busy computer may delay."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 3600.0)
    key, _daniel, _platform = _controlled_by_platform(cluster)
    asked: list[float] = []
    lost = keys._Served._lost

    async def counted(self, person, key_id, via):
        asked.append(time.monotonic())
        return await lost(self, person, key_id, via)

    monkeypatch.setattr(keys._Served, "_lost", counted)
    sent, ended, task = _streaming(cluster.b, key)
    try:
        _open(sent, ended)
        burst = time.monotonic() + 2.5
        while time.monotonic() < burst:  # a message every 20 ms, heard on instance B
            publish(cluster.server.bus, "ee:people", {"changed": []})
            time.sleep(0.02)
        time.sleep(1.2)
        assert not ended.is_set()
    finally:
        task.cancel()
    assert 2 <= len(asked) <= 4, asked  # heard, and asked again a second apart at most: not once a message
    assert all(later - earlier >= 0.9 for earlier, later in itertools.pairwise(asked)), asked


def _failing_space(cluster, monkeypatch, key, when):
    """The key's app on instance B, a stream that fails with words naming an address: by itself once it began
    (``alone``), or as it is ended (``ending``)."""
    spaces = cluster.servers[1].spaces
    built = spaces.app

    async def failing(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"retry: 3000\n\n", "more_body": True})
        if when == "alone":
            raise RuntimeError("the stream's store at 10.0.0.5 failed")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("the stream's store at 10.0.0.5 failed as it closed") from None

    async def app(identity):
        return failing if identity.id == key.id else await built(identity)

    monkeypatch.setattr(spaces, "app", app)


@pytest.mark.parametrize("when", ["alone", "ending"])
def test_a_stream_of_a_keys_space_that_fails_is_ended_and_said_by_its_type(cluster, monkeypatch, caplog, when):
    """Fix round 1: what a stream's task raised is taken and said by its type alone, whether it failed by itself or as
    the watch ended it; the stream ends with its last, empty part."""
    monkeypatch.setattr(keys, "WATCH_SECONDS", 3600.0)
    key, daniel, platform = _controlled_by_platform(cluster)
    _failing_space(cluster, monkeypatch, key, when)
    with caplog.at_level(logging.WARNING, logger="latet.ee.keys"):
        sent, ended, task = _streaming(cluster.b, key)
        try:
            if when == "ending":
                _open(sent, ended)
                cluster.people.remove_member(platform.id, daniel.id, actor=BY_HAND)
            assert ended.wait(5), "the stream did not end"
        finally:
            task.cancel()
        assert task.exception(timeout=5) is None
        settle(lambda: "RuntimeError" in caplog.text)
    assert "10.0.0.5" not in caplog.text
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [200]
