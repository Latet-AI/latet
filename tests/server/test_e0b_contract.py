"""What E0c takes from E0b, checked before E0c builds on it: the names of notes/enterprise-interfaces.md, the bodies
of the machines' calls and of the administration that E0c's client and commands read (the plan's Ruling 1), and what
E0c relies on: probes asked by address, the administration over HTTPS only, nothing of an identity opened with its
route key alone, and no dashboard and no computer for an API key."""

import asyncio
import base64
import inspect
import json
import os
import re
import subprocess
import sys

import httpx
import pytest
from starlette.testclient import TestClient

from latet.config import Config
from latet.runtime import digest
from latet.server import admin as admin_module
from latet.server import identities as identities_module
from latet.server import machines
from latet.server.app import create_server_app, open_server
from latet.server.db import Database, SchemaMismatch, check_schema
from latet.server.master import MasterKey, MasterKeyError
from latet.server.settings import ServerSettings, load_server_settings
from latet.server.tokens import ADMIN_PREFIX, KEY_PREFIX, TOKEN_PREFIX, route_key

from .support import close_test_server, listening, open_test_server

pytestmark = pytest.mark.server

MACHINE = "/_latet/server/v1"
ADMIN = f"{MACHINE}/admin"
LAPTOP = {"usernames": ["emily"], "hostname": "emily-laptop", "full_name": "Emily Carter", "emails": []}
# This machine's addresses: no password and no TLS are asked of PostgreSQL and Valkey here.
HERE = {"database_url": "postgresql://latet@127.0.0.1:5432/latet", "valkey_url": "redis://127.0.0.1:6379/0"}
# A host that is not this machine, and a password: what no refusal of the settings repeats.
OFF, PASSWORD = "cache.acme.lan", "hunter2-p4ss"


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=server.settings.url) as c:
        yield c


@pytest.fixture
def admin(server):
    return {"x-latet-admin": server.admin.issue("contract")}


@pytest.fixture
def clocked_server(schema, prefix, clock):
    found = open_test_server(schema, prefix, clock=clock)
    try:
        yield found
    finally:
        close_test_server(found)


@pytest.fixture
def no_variables(monkeypatch):
    """None of the server's variables set: the settings of a test are its file's, and what the test sets."""
    for name in [name for name in os.environ if name.startswith(("LATET_SERVER_", "LATET_MASTER_KEY"))]:
        monkeypatch.delenv(name)


def _person(client, server):
    _, code = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    return client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"}).json(), code


def _cli(enrolled):
    """What the computer's CLI sends on a sign-in link and its identity: its token and its computer key (R16)."""
    return {"x-latet-token": enrolled["token"], "x-latet-computer-key": enrolled["computer_key"]}


def _key(client, admin):
    """An API key made by the administration, and the path of its settings."""
    answer = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin)
    assert answer.status_code == 201
    key = answer.json()
    return key, f"{ADMIN}/identities/{key['identity']['id']}/settings"


def _settings(tmp_path, **given):
    """server.yaml of ``given`` over this machine's addresses (JSON is YAML), loaded as `latet server` loads it."""
    path = tmp_path / "server.yaml"
    path.write_text(json.dumps({"url": "https://latet.acme.lan", **HERE, **given}))
    return load_server_settings(path)


def _gone(client, route, mine):
    """What an address that was an identity's answers on its bridge (asked with ``mine``) and on its proxy: 403, and the
    words of an address nobody has, so that nothing says it ever was one."""
    for method, rest in (("GET", "_latet/engine/v1/ping"), ("POST", "anthropic/v1/messages")):
        was = client.request(method, f"/u/{route}/{rest}", headers=mine, json={})
        never = client.request(method, f"/u/{'a' * 26}/{rest}", headers=mine, json={})
        assert (was.status_code, was.json()) == (never.status_code, never.json()) and was.status_code == 403, method


# --- the server's core: settings, master key, schema, probes ----------------------------------------------------------


def test_the_settings_come_from_a_file_with_their_variables_over_it(tmp_path, monkeypatch, no_variables):
    path = tmp_path / "server.yaml"
    path.write_text(
        "url: https://latet.acme.lan\n"
        "database_url: postgresql://latet@127.0.0.1:5432/latet\n"
        "valkey_url: redis://127.0.0.1:6379/0\n"
    )
    monkeypatch.setenv("LATET_SERVER_URL", "https://latet.example.lan")
    monkeypatch.setenv("LATET_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    settings = load_server_settings(path)
    assert (settings.url, settings.valkey_url, settings.port) == (
        "https://latet.example.lan",
        "redis://127.0.0.1:6379/0",
        5283,
    )
    assert settings.master_key_file == tmp_path / "master.key"
    wanted = {"master_key_file", "certificate", "key", "host", "log_format", "log_level", "metrics_token_file"}
    assert wanted <= set(ServerSettings.model_fields)


@pytest.mark.parametrize(
    ("given", "says", "unless"),
    [
        pytest.param(
            {"valkey_url": f"rediss://{OFF}:6379/0"},
            "Valkey needs a password",
            {"valkey_unauthenticated": True},
            id="a valkey off this machine without a password",
        ),
        pytest.param(
            {"valkey_url": f"redis://:{PASSWORD}@{OFF}:6379/0"},
            "is not a TLS address",
            {"valkey_private_network": True},
            id="a valkey off this machine that is not rediss",
        ),
        pytest.param(
            {"valkey_url": f"rediss://:{PASSWORD}@{OFF}:6379/0?ssl_cert_reqs=none"},
            "turns the certificate check off",
            {"valkey_private_network": True},
            id="a valkey off this machine with its checks off",
        ),
        pytest.param(
            {"database_url": f"postgresql://latet:{PASSWORD}@{OFF}:5432/latet"},
            "does not ask for a TLS connection that checks the server",
            {"database_private_network": True},
            id="a postgresql off this machine without a checked tls",
        ),
    ],
)
def test_an_address_off_this_machine_is_refused_without_its_password_or_checked_tls_unless_settings_say_so(
    tmp_path, no_variables, given, says, unless
):
    setting = next(iter(unless))
    with pytest.raises(ValueError, match=says) as refused:
        _settings(tmp_path, **given)
    words = str(refused.value)
    assert type(refused.value) is ValueError  # a plain one: its words and nothing else
    assert f"LATET_SERVER_{setting.upper()}" in words  # it says how to tell the server that the network is its own
    assert OFF not in words and PASSWORD not in words  # nothing of the address
    assert getattr(_settings(tmp_path, **given, **unless), setting) is True  # what E0c's examples set


def test_an_address_off_this_machine_is_taken_over_checked_tls_with_a_password(tmp_path, no_variables):
    off = f"postgresql://latet:{PASSWORD}@{OFF}:5432/latet"
    for database in (
        f"{off}?sslmode=verify-full&sslrootcert=system",
        f"{off}?sslmode=verify-ca&sslrootcert=/etc/ssl/ca.pem",
    ):
        settings = _settings(tmp_path, database_url=database, valkey_url=f"rediss://:{PASSWORD}@{OFF}:6379/0")
        assert (settings.database_url, settings.valkey_url.partition(":")[0]) == (database, "rediss")


def test_the_master_key_is_32_bytes_in_base64_and_its_absence_is_said(server, tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_MASTER_KEY", raising=False)
    generated = MasterKey.generate()
    assert len(base64.b64decode(generated, validate=True)) == 32
    key = tmp_path / "master.key"
    key.write_text(generated + "\n")
    settings = server.settings.model_copy(update={"master_key_file": key})
    sealed = MasterKey.load(settings).seal(b"x", "contract")
    assert MasterKey.load(settings).open(sealed, "contract") == b"x"
    with pytest.raises(MasterKeyError, match="LATET_MASTER_KEY_FILE"):
        MasterKey.load(server.settings.model_copy(update={"master_key_file": None}))


def test_open_server_takes_the_settings_and_opens_the_schema_and_prefix_a_server_runs_on():
    parameters = inspect.signature(open_server).parameters
    assert next(iter(parameters)) == "settings" and {"clock", "schema", "prefix", "data"} <= set(parameters)
    assert (parameters["schema"].default, parameters["prefix"].default) == ("latet", "latet:")


def test_the_schema_is_the_codes_and_both_services_answer(server):
    check_schema(server.db)
    assert server.db.ping() is True and server.bus.ping() is True


def test_a_schema_that_is_not_the_codes_is_refused_with_the_command_that_brings_it_there(settings, schema):
    db = Database(settings.database_url, schema=schema)  # never migrated
    try:
        with pytest.raises(SchemaMismatch, match="latet server migrate"):
            check_schema(db)
    finally:
        db.close()


def test_health_and_ready_answer_whatever_the_host_and_say_nothing_else(client):
    for path in ("/_latet/health", "/_latet/ready"):
        assert client.get(path, headers={"host": "10.0.0.7:5283"}).status_code == 200, path
    health = client.get("/_latet/health").json()  # the sign-in page reads `server` and `docs`, nothing else
    assert health == {"ok": True, "server": True, "docs": health["docs"]} and isinstance(health["docs"], bool)
    assert client.get("/_latet/ready").json() == {"ready": True}  # the detail is for the log and the metrics
    assert client.get(f"{MACHINE}/whoami", headers={"host": "10.0.0.7:5283"}).status_code == 403  # nothing else is


# --- the machines' calls ----------------------------------------------------------------------------------------------


def test_a_person_enrols_and_asks_as_e0c_reads_it(client, server):
    enrolled, code = _person(client, server)
    url, token = server.settings.url, enrolled["token"]
    assert token.startswith(TOKEN_PREFIX)
    assert re.fullmatch(r"latet_ck_[A-Za-z0-9_-]{43}", enrolled["computer_key"])
    assert {"id", "name", "kind"} <= set(enrolled["identity"])
    assert (enrolled["identity"]["name"], enrolled["identity"]["kind"]) == ("Emily Carter", "person")
    base = f"{url}/u/{route_key(token)}"
    assert (enrolled["url"], enrolled["base"]) == (url, base)
    assert (enrolled["anthropic"], enrolled["openai"]) == (f"{base}/anthropic", f"{base}/openai/v1")
    mine = {"x-latet-token": token}
    who = client.get(f"{MACHINE}/whoami", headers=mine).json()
    assert who["id"] == enrolled["identity"]["id"] and who["version"] and who["url"] == url
    assert (who["name"], who["kind"], who["base"]) == ("Emily Carter", "person", base)
    assert (who["anthropic"], who["openai"]) == (enrolled["anthropic"], enrolled["openai"])
    for alone in (client.post(f"{MACHINE}/login-code", headers=mine), client.put(f"{MACHINE}/identity", headers=mine)):
        assert alone.status_code == 403 and alone.json()["reason"] == "computer_key_required"
    made = client.post(f"{MACHINE}/login-code", headers=_cli(enrolled)).json()
    assert made["code"] and made["url"] == f"{url}/_latet/ui/login?code={made['code']}"
    put = client.put(f"{MACHINE}/identity", json=LAPTOP, headers=_cli(enrolled))
    assert (put.status_code, put.json()) == (200, {"ok": True})
    assert client.get(f"/u/{route_key(token)}/_latet/engine/v1/ping", headers=mine).json()["ok"] is True
    forgotten = client.delete(f"{MACHINE}/token", headers=mine)
    assert (forgotten.status_code, forgotten.json()) == (200, {"ok": True})
    assert client.get(f"{MACHINE}/whoami", headers=mine).status_code == 401
    spent = client.post(f"{MACHINE}/enrol", json={"code": code, "label": "again"})
    assert spent.status_code == 401 and "used or late" in spent.json()["error"]


def test_the_route_key_alone_opens_no_dashboard_and_no_cli_route(client, server):
    enrolled, _ = _person(client, server)
    space = f"/u/{route_key(enrolled['token'])}"
    for method, path in (
        ("POST", "/_latet/admin/login-code"),
        ("POST", "/_latet/admin/reload"),
        ("POST", "/_latet/admin/destinations"),
        ("POST", "/_latet/admin/user-name"),
        ("GET", "/_latet/api/v1/vault"),
        ("GET", "/_latet/engine/v1/ping"),
    ):
        answer = client.request(method, f"{space}{path}", json={})
        assert answer.status_code in (401, 403, 404), (path, answer.status_code)


def test_an_api_key_has_no_dashboard_and_no_computer(client, admin):
    key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()["key"]
    theirs = {"x-latet-token": key}
    assert client.post(f"{MACHINE}/login-code", headers=theirs).status_code == 403
    assert client.put(f"{MACHINE}/identity", json=LAPTOP, headers=theirs).status_code == 403
    assert client.delete(f"{MACHINE}/token", headers=theirs).status_code == 403


def test_a_disabled_persons_token_and_address_are_refused(client, server, admin):
    enrolled, _ = _person(client, server)
    token, person = enrolled["token"], enrolled["identity"]["id"]
    mine = {"x-latet-token": token}
    assert client.get(f"{MACHINE}/whoami", headers=mine).status_code == 200  # it opened before
    assert client.get(f"/u/{route_key(token)}/_latet/engine/v1/ping", headers=mine).status_code == 200
    off = client.patch(f"{ADMIN}/identities/{person}", json={"disabled": True}, headers=admin)
    assert off.status_code == 200 and off.json()["identity"]["disabled"] is True
    for refused in (client.get(f"{MACHINE}/whoami", headers=mine), client.post(f"{MACHINE}/login-code", headers=mine)):
        assert (refused.status_code, refused.json()) == (401, {"error": "unauthorized"})
    _gone(client, route_key(token), mine)


def test_an_identity_past_its_body_limit_or_accumulated_names_is_refused_unchanged(client, server):
    enrolled, _ = _person(client, server)
    mine = _cli(enrolled)
    person = server.identities.get(enrolled["identity"]["id"])
    assert person is not None
    runtime = client.portal.call(server.spaces.runtime, person)
    before = runtime.config.identity
    # A valid contribution with trailing whitespace: raising the body bound would accept it and change identity.
    raw = json.dumps({"usernames": ["oversized-contribution"]}).encode() + b" " * machines.IDENTITY_BYTES
    huge = client.put(f"{MACHINE}/identity", content=raw, headers=mine)
    assert huge.status_code == 413 and huge.json()["error"]
    assert runtime.config.identity == before
    names = [f"computer-{n}" for n in range(machines.NAMES_KEPT)]
    filled = client.put(f"{MACHINE}/identity", json={"usernames": names}, headers=mine)
    assert (filled.status_code, filled.json()) == (200, {"ok": True})
    assert runtime.config.identity.usernames == names
    before = runtime.config.identity
    full = client.put(f"{MACHINE}/identity", json={"usernames": ["one-more"]}, headers=mine)
    assert full.status_code == 409 and full.json()["error"] and "one-more" not in full.text
    assert runtime.config.identity == before


async def test_a_concurrent_identity_update_is_refused_with_one_second_then_can_retry(server, monkeypatch):
    person, code = server.identities.add_person("Emily Carter")
    async with listening(server):
        transport = httpx.ASGITransport(app=create_server_app(server))
        async with httpx.AsyncClient(transport=transport, base_url=server.settings.url, trust_env=False) as client:
            enrolled = await client.post(f"{MACHINE}/enrol", json={"code": code, "label": "laptop"})
            mine = _cli(enrolled.json())
            entered, release = asyncio.Event(), asyncio.Event()
            runtime = server.spaces.runtime
            entries = 0

            async def held(identity):
                nonlocal entries
                assert identity.id == person.id
                entries += 1
                assert entries == 1, "a concurrent identity update entered the held runtime"
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
                return await runtime(identity)

            with monkeypatch.context() as patch:
                patch.setattr(server.spaces, "runtime", held)
                first = asyncio.create_task(client.put(f"{MACHINE}/identity", json=LAPTOP, headers=mine))
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    refused = await asyncio.wait_for(
                        client.put(f"{MACHINE}/identity", json={"usernames": ["retry"]}, headers=mine), 10
                    )
                    assert entries == 1
                    assert refused.status_code == 429 and refused.json()["error"]
                    assert refused.headers["retry-after"] == "1"
                finally:
                    release.set()
                    completed = await asyncio.wait_for(first, 30)
                assert (completed.status_code, completed.json()) == (200, {"ok": True})
            retried = await client.put(f"{MACHINE}/identity", json={"usernames": ["retry"]}, headers=mine)
            assert (retried.status_code, retried.json()) == (200, {"ok": True})
            assert "retry" in (await runtime(person)).config.identity.usernames


def test_a_rotated_keys_old_secret_and_address_are_refused(client, admin):
    key, _ = _key(client, admin)
    old, mine = key["key"], {"x-latet-token": key["key"]}
    assert client.get(f"{MACHINE}/whoami", headers=mine).status_code == 200  # it opened before
    assert client.get(f"/u/{route_key(old)}/_latet/engine/v1/ping", headers=mine).status_code == 200
    rotated = client.post(f"{ADMIN}/identities/{key['identity']['id']}/rotate", headers=admin).json()
    refused = client.get(f"{MACHINE}/whoami", headers=mine)
    assert (refused.status_code, refused.json()) == (401, {"error": "unauthorized"})
    _gone(client, route_key(old), mine)
    fresh = {"x-latet-token": rotated["key"]}  # the key's new secret opens what the old one did
    assert client.get(f"{MACHINE}/whoami", headers=fresh).json()["id"] == key["identity"]["id"]
    assert client.get(f"/u/{route_key(rotated['key'])}/_latet/engine/v1/ping", headers=fresh).status_code == 200


def test_a_refused_call_answers_as_e0c_reads_it(client, server, admin):
    enrolled, _ = _person(client, server)
    url, token = server.settings.url, enrolled["token"]
    unknown = [
        client.get(f"{MACHINE}/whoami", headers={"x-latet-token": f"{TOKEN_PREFIX}{'0' * 43}"}),
        client.get(f"{ADMIN}/status"),
        client.get(f"{ADMIN}/status", headers={"x-latet-admin": f"{ADMIN_PREFIX}{'0' * 43}"}),
    ]
    for answer in unknown:
        assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
    for answer in (*unknown, client.get(f"{MACHINE}/whoami", headers={"x-latet-token": token})):
        assert answer.headers["cache-control"] == "no-store" and answer.headers["x-content-type-options"] == "nosniff"
    # A web page, as a browser says it is (an origin, or how the page stands to the site): refused by the machines'
    # calls, by the administration and on an identity's address, so E0c's clients send neither header.
    calls = (
        (f"{MACHINE}/whoami", {"x-latet-token": token}),
        (f"{ADMIN}/status", admin),
        (f"/u/{route_key(token)}/_latet/engine/v1/ping", {"x-latet-token": token}),
    )
    for page in ({"origin": url}, {"sec-fetch-site": "same-origin"}, {"sec-fetch-site": "cross-site"}):
        for path, headers in calls:
            answer = client.get(path, headers={**headers, **page})
            assert answer.status_code == 403 and answer.json()["error"], (path, page)


# --- the administration -----------------------------------------------------------------------------------------------


def test_the_administration_answers_as_e0c_reads_it(client, server, admin):
    url = server.settings.url
    added = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Daniel Okafor"}, headers=admin)
    assert added.status_code == 201
    person = added.json()["identity"]["id"]
    assert added.json()["code"] and added.json()["identity"]["kind"] == "person"
    created = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin)
    assert created.status_code == 201
    key = created.json()
    assert key["key"].startswith(KEY_PREFIX) and key["identity"]["kind"] == "key"
    base = f"{url}/u/{route_key(key['key'])}"
    assert (key["base"], key["anthropic"], key["openai"]) == (base, f"{base}/anthropic", f"{base}/openai/v1")
    listed = client.get(f"{ADMIN}/identities", params={"kind": "person"}, headers=admin).json()["identities"]
    wanted = {"id", "name", "email", "kind", "disabled", "created", "last_seen", "source", "external_id"}
    assert wanted | {"tokens", "tampered_tokens"} <= set(listed[0])
    assert (listed[0]["tokens"], listed[0]["tampered_tokens"]) == (0, 0)  # how many computers: none yet
    changed = client.patch(f"{ADMIN}/identities/{person}", json={"disabled": True}, headers=admin).json()
    assert changed["identity"]["disabled"]
    assert client.post(f"{ADMIN}/identities/{person}/code", headers=admin).json()["code"]
    assert client.post(f"{ADMIN}/identities/{key['identity']['id']}/code", headers=admin).status_code == 409
    assert client.post(f"{ADMIN}/identities/{person}/rotate", headers=admin).status_code == 409
    one = client.get(f"{ADMIN}/identities/{person}", headers=admin).json()
    assert (one["identity"]["id"], one["tokens"]) == (person, [])
    assert client.get(f"{ADMIN}/identities/{person}/tokens", headers=admin).json()["tokens"] == []
    assert client.delete(f"{ADMIN}/identities/{person}/tokens", headers=admin).json()["revoked"] == 0
    rotated = client.post(f"{ADMIN}/identities/{key['identity']['id']}/rotate", headers=admin).json()
    assert rotated["key"].startswith(KEY_PREFIX) and rotated["key"] != key["key"]
    assert rotated["base"] == f"{url}/u/{route_key(rotated['key'])}" and rotated["base"] != key["base"]
    assert rotated["anthropic"] == f"{rotated['base']}/anthropic" and rotated["anthropic"] != key["anthropic"]
    assert rotated["openai"] == f"{rotated['base']}/openai/v1" and rotated["openai"] != key["openai"]
    status = client.get(f"{ADMIN}/status", headers=admin).json()
    assert {"version", "url", "instance", "identities", "spaces", "vault", "audit", "database", "valkey"} <= set(status)
    assert status["edition"] == [] and status["url"] == url and status["instance"] == server.instance
    assert status["identities"]["people"] == 1
    assert status["identities"]["keys"] == 1
    assert status["identities"]["enabled"] == 1  # the person is disabled
    assert set(status["vault"]) == {"limit", "near"} and status["audit"]["chain"] in ("ok", "broken", "unknown")
    assert (status["database"], status["valkey"]) == (True, True)
    entries = client.get(f"{ADMIN}/log", params={"action": "person.added"}, headers=admin).json()["entries"]
    shape = {"id", "ts", "actor", "action", "target", "details", "address", "prev", "hash"}
    assert entries and shape <= set(entries[0])
    assert entries[0]["target"] == person and entries[0]["actor"].startswith("admin:")
    removed = client.delete(f"{ADMIN}/identities/{person}", headers=admin)
    assert (removed.status_code, removed.json()) == (200, {"ok": True})


@pytest.mark.parametrize("reverse_ids", [False, True])
def test_a_persons_computers_are_listed_and_revoked_one_by_one_or_all(clocked_server, monkeypatch, reverse_ids):
    server = clocked_server
    admin = {"x-latet-admin": server.admin.issue("contract")}
    real = identities_module.new_id
    ids = ["t_aaaaaaaaaa", "t_bbbbbbbbbb"]
    token_ids = iter(reversed(ids) if reverse_ids else ids)
    monkeypatch.setattr(identities_module, "new_id", lambda prefix: next(token_ids) if prefix == "t_" else real(prefix))
    with TestClient(create_server_app(server), base_url=server.settings.url) as client:
        _computers_listed_and_revoked(client, admin)


def _computers_listed_and_revoked(client, admin):
    added = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Daniel Okafor"}, headers=admin).json()
    path = f"{ADMIN}/identities/{added['identity']['id']}"
    assert client.post(f"{MACHINE}/enrol", json={"code": added["code"], "label": "laptop"}).status_code == 200
    code = client.post(f"{path}/code", headers=admin).json()["code"]  # a new code ends the one before it
    assert client.post(f"{MACHINE}/enrol", json={"code": code, "label": "desktop"}).status_code == 200
    tokens = client.get(f"{path}/tokens", headers=admin).json()["tokens"]
    assert sorted(t["label"] for t in tokens) == ["desktop", "laptop"]
    assert tokens[0]["created"] == tokens[1]["created"]  # either legal id tie-break order is exercised
    assert all({"id", "label", "created", "last_used", "tampered"} <= set(t) for t in tokens)
    assert client.delete(f"{path}/tokens", params={"token": "latet_pat_nope"}, headers=admin).status_code == 400
    laptop = next(t for t in tokens if t["label"] == "laptop")
    assert client.delete(f"{path}/tokens", params={"token": laptop["id"]}, headers=admin).json() == {"revoked": 1}
    assert [t["label"] for t in client.get(f"{path}/tokens", headers=admin).json()["tokens"]] == ["desktop"]
    assert client.delete(f"{path}/tokens", headers=admin).json() == {"revoked": 1}
    assert client.get(f"{path}/tokens", headers=admin).json()["tokens"] == []


def test_a_revoke_naming_another_persons_token_id_revokes_nothing(client, server, admin):
    emily, _ = _person(client, server)
    added = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Daniel Okafor"}, headers=admin).json()
    daniel = client.post(f"{MACHINE}/enrol", json={"code": added["code"], "label": "desktop"}).json()["token"]
    theirs = f"{ADMIN}/identities/{added['identity']['id']}/tokens"
    [their_token] = client.get(theirs, headers=admin).json()["tokens"]
    revoked = client.delete(
        f"{ADMIN}/identities/{emily['identity']['id']}/tokens", params={"token": their_token["id"]}, headers=admin
    )
    assert (revoked.status_code, revoked.json()) == (200, {"revoked": 0})  # what the administrator is told
    assert [t["id"] for t in client.get(theirs, headers=admin).json()["tokens"]] == [their_token["id"]]
    for token in (daniel, emily["token"]):  # neither person's computer lost its token
        assert client.get(f"{MACHINE}/whoami", headers={"x-latet-token": token}).status_code == 200
    assert client.get(f"{ADMIN}/log", params={"action": "token.revoked"}, headers=admin).json()["entries"] == []


def test_a_token_whose_row_does_not_hold_is_flagged_and_counted_apart(client, server, admin):
    enrolled, _ = _person(client, server)
    person = enrolled["identity"]["id"]
    [token] = client.get(f"{ADMIN}/identities/{person}/tokens", headers=admin).json()["tokens"]
    assert token["label"] == "laptop" and token["tampered"] is False
    with server.db.connection() as conn:  # written by hand, without the server's key
        conn.execute("UPDATE tokens SET tag = 'forged' WHERE id = %s", (token["id"],))
    [flagged] = client.get(f"{ADMIN}/identities/{person}/tokens", headers=admin).json()["tokens"]
    assert (flagged["id"], flagged["tampered"]) == (token["id"], True)  # listed, never hidden
    [listed] = client.get(f"{ADMIN}/identities", params={"kind": "person"}, headers=admin).json()["identities"]
    assert (listed["tokens"], listed["tampered_tokens"]) == (1, 1)


def test_the_log_is_read_newest_first_a_page_at_a_time_and_verified_with_its_head(clocked_server, clock):
    server = clocked_server
    admin = {"x-latet-admin": server.admin.issue("contract")}
    other = {"x-latet-admin": server.admin.issue("other contract")}
    with TestClient(create_server_app(server), base_url=server.settings.url) as client:
        _log_filters_and_head(client, admin, other, clock)


def _log_filters_and_head(client, admin, other, clock):
    for headers in (admin, other, admin):
        clock.now += 10
        assert client.get(f"{ADMIN}/status", headers=headers).status_code == 200
    log = f"{ADMIN}/log"
    calls = client.get(log, params={"action": "status.read"}, headers=admin).json()["entries"]
    assert len(calls) == 3 and [e["id"] for e in calls] == sorted((e["id"] for e in calls), reverse=True)
    newest, middle, oldest = calls
    assert oldest["ts"] < middle["ts"] < newest["ts"]
    newer = client.get(log, params={"action": "status.read", "since": middle["ts"]}, headers=admin).json()
    assert [e["id"] for e in newer["entries"]] == [newest["id"], middle["id"]]  # `since` is included
    assert newest["actor"] == oldest["actor"] and newest["actor"] != middle["actor"]
    selected = client.get(log, params={"action": "status.read", "actor": middle["actor"]}, headers=admin).json()
    assert [e["id"] for e in selected["entries"]] == [middle["id"]]
    older = client.get(log, params={"action": "status.read", "until": middle["ts"]}, headers=admin).json()
    assert [e["id"] for e in older["entries"]] == [oldest["id"]]  # `until` is excluded
    two = client.get(log, params={"action": "status.read", "limit": 2}, headers=admin).json()
    assert [e["id"] for e in two["entries"]] == [newest["id"], middle["id"]]
    assert client.get(log, params={"limit": 10_000}, headers=admin).status_code == 200
    for limit in (10_001, 0):
        refused = client.get(log, params={"limit": limit}, headers=admin)
        assert refused.status_code == 400 and refused.json()["error"], limit
    verified = client.get(f"{log}/verify", headers=admin).json()
    assert verified["ok"] is True and verified["broken_at"] is None and verified["checked"] >= 3
    kept = {e["id"]: e["hash"] for e in client.get(log, headers=admin).json()["entries"]}
    assert set(verified["head"]) == {"id", "hash"} and kept[verified["head"]["id"]] == verified["head"]["hash"]


def test_the_administration_answers_over_https_only(server, admin):
    plain = server.settings.url.replace("https://", "http://", 1)
    with TestClient(create_server_app(server), base_url=plain) as client:
        assert client.get(f"{ADMIN}/status", headers=admin).status_code == 403


def test_an_administration_token_past_its_day_is_refused_and_the_log_says_so(clocked_server, clock):
    server = clocked_server
    late = {"x-latet-admin": server.admin.issue("a day", days=1)}
    admin = {"x-latet-admin": server.admin.issue("query", days=7)}
    with TestClient(create_server_app(server), base_url=server.settings.url) as client:
        assert client.get(f"{ADMIN}/status", headers=late).status_code == 200
        clock.now += 2 * 86400
        answer = client.get(f"{ADMIN}/status", headers=late)
        assert (answer.status_code, answer.json()) == (401, {"error": "unauthorized"})
        entries = client.get(
            f"{ADMIN}/log", params={"action": "status.read", "since": clock.now}, headers=admin
        ).json()["entries"]
    [entry] = entries
    assert entry["actor"] == "unknown" and entry["details"] == {"reason": "expired", "status": 401}


def test_the_shared_settings_are_written_whole_with_an_etag(client, admin):
    read = client.get(f"{ADMIN}/shared", headers=admin).json()
    config, etag = read["settings"], read["etag"]
    assert etag == digest(Config.model_validate(config))  # latet.runtime.digest of the settings
    config["history"] = "metadata"  # the settings the server shares (rules for everyone are an edition's)
    written = client.put(f"{ADMIN}/shared", json={**config, "etag": etag}, headers=admin)
    assert written.status_code == 200 and written.json()["etag"] != etag
    assert written.json()["settings"]["history"] == "metadata" and written.json()["kept_rules"] == {}
    stale = client.put(f"{ADMIN}/shared", json={**config, "etag": etag}, headers=admin)
    assert stale.status_code == 409 and stale.json()["error"]


def test_an_api_keys_own_settings_are_written_whole_with_an_etag(client, admin):
    _, path = _key(client, admin)
    read = client.get(path, headers=admin).json()
    config, etag = read["settings"], read["etag"]
    assert etag == digest(Config.model_validate(config))
    config["profile"] = "max"
    written = client.put(path, json={**config, "etag": etag}, headers=admin)
    assert written.status_code == 200 and written.json()["etag"] != etag
    assert client.get(path, headers=admin).json()["settings"]["profile"] == "max"
    stale = client.put(path, json={**config, "etag": etag}, headers=admin)
    assert stale.status_code == 409 and stale.json()["error"]


def test_a_refusal_of_settings_says_which_fields_in_details_and_never_what_they_hold(client, admin):
    _, path = _key(client, admin)
    read = client.get(path, headers=admin).json()
    config = {**read["settings"], "llm_review": {**read["settings"]["llm_review"], "api_key_file": "/etc/latet/key"}}
    refused = client.put(path, json={**config, "etag": read["etag"]}, headers=admin)
    assert refused.status_code == 400 and refused.json()["error"]
    details = refused.json()["details"]  # a list of texts, one for each field
    assert isinstance(details, list) and all(isinstance(d, str) for d in details)
    assert details[0].startswith("llm_review.api_key_file") and "/etc/latet/key" not in refused.text
    invalid = client.put(f"{ADMIN}/shared", json={"profile": "not a profile"}, headers=admin)
    assert invalid.status_code == 400 and invalid.json()["details"][0].startswith("profile")
    assert "not a profile" not in invalid.text


# --- what a client is told when the server will not answer now --------------------------------------------------------


def test_every_429_of_a_limit_says_how_long_to_wait(client, server, monkeypatch):
    for module in (machines, admin_module):  # both import the limit
        monkeypatch.setattr(module, "LOOKUP_LIMIT", 2)
    monkeypatch.setattr(machines, "ENROL_LIMIT", 1)
    monkeypatch.setattr(machines, "LOGIN_CODES", 1)
    enrolled, _ = _person(client, server)
    mine = _cli(enrolled)
    guesses = [client.get(f"{MACHINE}/whoami", headers={"x-latet-token": f"{TOKEN_PREFIX}{n:043d}"}) for n in range(3)]
    assert [a.status_code for a in guesses] == [401, 401, 429]  # a token past its address's limit
    assert 1 <= int(guesses[-1].headers["retry-after"]) <= 600 and "retry-after" not in guesses[0].headers
    assert client.get(f"{MACHINE}/whoami", headers=mine).status_code == 200  # the colleague who shares the address
    tried = [client.post(f"{MACHINE}/enrol", json={"code": "nope", "label": "x"}) for _ in range(2)]
    assert [a.status_code for a in tried] == [401, 429] and tried[-1].headers["retry-after"] == "600"
    assert client.post(f"{MACHINE}/login-code", headers=mine).status_code == 200
    links = client.post(f"{MACHINE}/login-code", headers=mine)
    assert links.status_code == 429 and links.headers["retry-after"] == "60"
    elsewhere = TestClient(create_server_app(server), base_url=server.settings.url, client=("203.0.113.7", 50000))
    tokens = [{"x-latet-admin": f"{ADMIN_PREFIX}{n:043d}"} for n in range(3)]
    calls = [elsewhere.get(f"{ADMIN}/status", headers=headers) for headers in tokens]
    assert [a.status_code for a in calls] == [401, 401, 429]  # the administration, an address of its own
    assert 1 <= int(calls[-1].headers["retry-after"]) <= 600


def test_a_server_whose_threads_are_busy_says_when_to_try_again(server, admin, monkeypatch):
    address, code, label = "203.0.113.77", "e0c-busy-code-sentinel", "e0c-busy-label-sentinel"
    with TestClient(create_server_app(server), base_url=server.settings.url, client=(address, 50000)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(machines, "credentials_busy", lambda: True)  # what a flood of unknown credentials brings
            enrolment = client.post(f"{MACHINE}/enrol", json={"code": code, "label": label})
            unused = client.get(f"{ADMIN}/status", headers=admin)  # a token no instance has found valid yet
        for answer in (enrolment, unused):
            assert answer.status_code == 503 and answer.headers["retry-after"] == "1" and answer.json()["error"]
        entries = client.get(f"{ADMIN}/log", params={"action": "calls.refused"}, headers=admin).json()["entries"]
    assert entries  # a fresh Refusals writes its first busy refusal immediately
    for entry in entries:
        details = entry["details"]
        assert type(details["count"]) is int and details["count"] >= 1
        assert details["reason"] == "busy"
        rendered = json.dumps(details)
        assert all(value not in rendered for value in (address, code, label, admin["x-latet-admin"]))


# --- the space of an identity, two instances, a computer without the extra --------------------------------------------


async def test_a_spaces_runtime_knows_its_identity_and_its_server(server):
    identity, _ = server.identities.add_person("Emily Carter")
    async with listening(server):  # a space is built only while the instance hears Valkey (E0b's NotListening)
        runtime = await server.spaces.runtime(identity)
        assert runtime.identity == identity.ref()
        assert runtime.gateway.on_server is not None and runtime.gateway.on_server.url == server.settings.url


def test_two_instances_of_one_server_share_what_lasts(instances):
    first, second = instances
    identity, _ = first.identities.add_person("Emily Carter")
    assert second.identities.get(identity.id) is not None


def test_a_computer_imports_latet_server_tokens_without_the_server_extra():
    code = "import sys; sys.modules['psycopg'] = None; sys.modules['redis'] = None; import latet.server.tokens"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
