"""The administration of a Latet server: an administration token, over HTTPS, never from a web page; people and API
keys made, changed, disabled, rotated and removed; an API key's settings, which the administration approves as it sets
them; the shared settings; the log and the state; every call in the administration log, refusals and failures too, and
nothing secret in it. Refused calls are limited by address; a body that is not settings changes nothing; settings are
written whole, their etag compared under the lock every change takes; and no call holds the event loop."""

import asyncio
import contextlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import redis
from starlette.testclient import TestClient

from latet.config import Config, Profile, TermConfig
from latet.engine.detectors.terms import TERM_LONGEST
from latet.gateway.sigv4 import AwsCredentials
from latet.logline import Paced
from latet.runtime import digest
from latet.server import admin as admin_module
from latet.server import egress, machines
from latet.server import shared as shared_module
from latet.server import space_settings as space_settings_module
from latet.server.admin import ADMIN_THREADS, LOG_MOST, _Call, _Turns
from latet.server.app import create_server_app
from latet.server.live import ValkeyAwsSessions
from latet.server.logs import REFUSED
from latet.server.machines import CREDENTIAL_THREADS, KNOWN_SECONDS, Known, bucket_of, off_loop
from latet.server.master import root_key
from latet.server.shared import SETTING_KEYS, SharedSettings
from latet.server.space_settings import PgSettings
from latet.server.tokens import route_key

from .sharing import share_rules
from .support import URL, close_test_server, open_test_server

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
# What a name shows as nothing: a zero-width space, a right-to-left override, a left-to-right isolate.
ZWSP, RLO, LRI = chr(0x200B), chr(0x202E), chr(0x2066)


@pytest.fixture(autouse=True)
def _egress(monkeypatch):
    """Each test its own lookups under way, and no test resolves ipv4only.arpa (the system's resolver is not stopped
    by pytest-socket): the NAT64 prefixes count as discovered, none found."""
    monkeypatch.setattr(egress, "_LOOKUPS", egress._Lookups())
    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))


@pytest.fixture
def admin(server):
    return {"x-latet-admin": server.admin.issue("first administrator")}


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


@pytest.fixture
def bare(server):
    """The app with no lifespan, so no ear: what is ended on this instance is the call's own doing, not a message's."""
    return TestClient(create_server_app(server), base_url=URL)


def _actor(server, admin):
    return f"admin:{server.admin.check(admin['x-latet-admin'])}"


def _key(client, admin, name="Billing CI"):
    """An API key made by the administration, and the path of its settings."""
    key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": name}, headers=admin).json()
    return key, f"{ADMIN}/identities/{key['identity']['id']}/settings"


def _runtime(server, identity_id):
    return server.spaces._spaces[identity_id].runtime


def test_only_an_administration_token_over_https_and_never_from_a_web_page(server, client, admin):
    assert client.get(f"{ADMIN}/status").status_code == 401
    assert client.get(f"{ADMIN}/status", headers={"x-latet-admin": "latet_adm_nope"}).status_code == 401
    assert client.get(f"{ADMIN}/status", headers={**admin, "origin": URL}).status_code == 403
    in_clear = TestClient(create_server_app(server), base_url="http://latet.acme.lan")  # no lifespan: refused first
    assert in_clear.get(f"{ADMIN}/status", headers=admin).status_code == 403
    status = client.get(f"{ADMIN}/status", headers=admin).json()
    assert (status["url"], status["database"], status["valkey"], status["edition"]) == (URL, True, True, [])
    assert status["identities"] == {"people": 0, "keys": 0, "enabled": 0}
    calls = server.audit.query(action="status.read")
    assert sorted(e.details["status"] for e in calls) == [200, 401, 401, 403, 403]
    assert {e.actor for e in calls if e.details["status"] != 200} == {"unknown"}
    assert [e.actor for e in calls if e.details["status"] == 200] == [_actor(server, admin)]


def test_a_page_that_says_it_is_one_is_refused_and_answers_are_never_kept(server, client, admin):
    assert client.get(f"{ADMIN}/status", headers={**admin, "sec-fetch-site": "same-origin"}).status_code == 403
    for answer in (client.get(f"{ADMIN}/status", headers=admin), client.get(f"{ADMIN}/status")):
        assert answer.headers["cache-control"] == "no-store"  # a code, a key: no cache on the way keeps one
        assert answer.headers["x-content-type-options"] == "nosniff"


def test_people_and_api_keys_are_made_changed_disabled_rotated_and_removed(server, client, admin):
    made = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter",
                                                    "email": "emily.carter@acme.com"}, headers=admin)  # fmt: skip
    assert made.status_code == 201
    emily, code = made.json()["identity"], made.json()["code"]
    assert server.identities.enrol(code, "laptop") is not None
    key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
    assert key["key"].startswith("latet_key_") and key["base"] == f"{URL}/u/{route_key(key['key'])}"
    assert client.post(f"{ADMIN}/identities", json={"kind": "robot", "name": "x"}, headers=admin).status_code == 400
    listed = client.get(f"{ADMIN}/identities?kind=person", headers=admin).json()["identities"]
    assert [(i["name"], i["email"], i["tokens"]) for i in listed] == [("Emily Carter", "emily.carter@acme.com", 1)]
    path = f"{ADMIN}/identities/{emily['id']}"
    changed = client.patch(path, json={"name": "Emily J. Carter", "disabled": True}, headers=admin).json()
    assert (changed["identity"]["name"], changed["identity"]["disabled"]) == ("Emily J. Carter", True)
    assert client.patch(path, json={"disabled": False}, headers=admin).json()["identity"]["disabled"] is False
    rotated = client.post(f"{ADMIN}/identities/{key['identity']['id']}/rotate", headers=admin).json()
    assert rotated["key"] != key["key"] and server.identities.by_secret(key["key"]) is None
    assert client.post(f"{path}/rotate", headers=admin).status_code == 409
    assert client.post(f"{ADMIN}/identities/{key['identity']['id']}/code", headers=admin).status_code == 409
    assert client.post(f"{path}/code", headers=admin).json()["code"]
    tokens = client.get(f"{path}/tokens", headers=admin).json()["tokens"]
    assert [t["label"] for t in tokens] == ["laptop"]
    assert client.delete(f"{path}/tokens?token={tokens[0]['id']}", headers=admin).json() == {"revoked": 1}
    assert client.delete(f"{ADMIN}/identities/{key['identity']['id']}", headers=admin).json() == {"ok": True}
    assert client.get(f"{ADMIN}/identities/{key['identity']['id']}", headers=admin).status_code == 404
    done = [e.action for e in reversed(server.audit.query(actor=_actor(server, admin)))]
    assert done == [
        "person.added", "key.added", "identity.add", "identities.list", "person.disabled", "person.enabled",
        "key.rotated", "key.rotate", "code.issue", "code.issued", "tokens.list", "token.revoked", "key.removed",
        "identity.read",
    ]  # fmt: skip
    told = repr([(e.target, e.details) for e in server.audit.query()])
    for secret in (code, key["key"], rotated["key"], "emily.carter@acme.com", "Emily"):
        assert secret not in told


def test_a_person_is_not_added_with_an_email_an_enabled_person_has(server, client, admin):
    """Two people with one address (known item 1): the second is refused, whatever the address's case, in words that
    name the person who has it and never the address; the refusal is in the log. A name may be given twice, and the
    address again once its person is disabled."""
    people = f"{ADMIN}/identities"
    emily = client.post(people, json={"kind": "person", "name": "Emily Carter", "email": "emily.carter@acme.com"},
                        headers=admin).json()["identity"]  # fmt: skip
    again = client.post(people, json={"kind": "person", "name": "Emily C.", "email": " Emily.Carter@ACME.com"},
                        headers=admin)  # fmt: skip
    assert again.status_code == 409 and emily["id"] in again.json()["error"]
    assert "emily.carter" not in again.json()["error"].lower()
    listed = client.get(f"{people}?kind=person", headers=admin).json()["identities"]
    assert [i["id"] for i in listed] == [emily["id"]]
    [refused] = [e for e in server.audit.query(action="identity.add") if e.details["status"] == 409]
    assert "emily.carter" not in repr(refused).lower()
    same_name = client.post(people, json={"kind": "person", "name": "Emily Carter"}, headers=admin)
    assert same_name.status_code == 201
    client.patch(f"{people}/{emily['id']}", json={"disabled": True}, headers=admin)
    given_again = client.post(people, json={"kind": "person", "name": "Emily Carter", "email": "emily.carter@acme.com"},
                              headers=admin)  # fmt: skip
    assert given_again.status_code == 201
    second = given_again.json()["identity"]
    # The first one enabled again would make two: refused, naming the one who has the address now; still disabled.
    enabled = client.patch(f"{people}/{emily['id']}", json={"disabled": False}, headers=admin)
    assert enabled.status_code == 409 and second["id"] in enabled.json()["error"]
    assert server.identities.current(emily["id"]).disabled is True
    # Her address changed and enabled in one call: the address checked as it will be.
    moved = client.patch(f"{people}/{emily['id']}", json={"email": "emily.c@acme.com", "disabled": False},
                         headers=admin)  # fmt: skip
    assert moved.status_code == 200 and moved.json()["identity"]["disabled"] is False


def test_an_enabled_persons_address_is_not_given_to_another_by_a_change(server, client, admin):
    """A change of a person's address to one an enabled person has is refused, whatever its case, as an add is; a
    person disabled in the same call may take it, and so may a disabled person."""
    people = f"{ADMIN}/identities"
    emily = client.post(people, json={"kind": "person", "name": "Emily Carter", "email": "emily.carter@acme.com"},
                        headers=admin).json()["identity"]  # fmt: skip
    daniel = client.post(people, json={"kind": "person", "name": "Daniel Okafor", "email": "daniel@acme.com"},
                         headers=admin).json()["identity"]  # fmt: skip
    taken = client.patch(f"{people}/{daniel['id']}", json={"email": "Emily.Carter@acme.com"}, headers=admin)
    assert taken.status_code == 409 and emily["id"] in taken.json()["error"]
    assert "emily.carter" not in taken.json()["error"].lower()
    assert server.identities.current(daniel["id"]).email == "daniel@acme.com"
    renamed = client.patch(f"{people}/{daniel['id']}", json={"name": "Daniel O.", "email": "daniel@acme.com"},
                           headers=admin)  # fmt: skip
    assert renamed.status_code == 200  # his own address again: no one else's
    parked = client.patch(f"{people}/{daniel['id']}", json={"email": "emily.carter@acme.com", "disabled": True},
                          headers=admin)  # fmt: skip
    assert parked.status_code == 200 and parked.json()["identity"]["disabled"] is True


def test_an_address_change_and_an_enable_at_once_leave_one_enabled_person_with_it(server):
    """Adding a person, changing another's address and enabling a third, all with one address at the same moment on
    instances of their own: one enabled person has it in the end (one lock for the address, as PostgreSQL folds it)."""
    from latet.server.identities import EmailTaken

    identities = server.identities
    daniel, _ = identities.add_person("Daniel Okafor", "daniel@acme.com")
    olivia, _ = identities.add_person("Olivia Brandt", "EMILY.CARTER@acme.com")
    identities.disable(olivia.id)
    barrier = threading.Barrier(3)

    def run(work) -> None:
        barrier.wait(10)
        with contextlib.suppress(EmailTaken):
            work()

    works = [
        lambda: identities.add_person("Emily Carter", "emily.carter@acme.com"),
        lambda: identities.update(daniel.id, email="Emily.Carter@acme.com"),
        lambda: identities.enable(olivia.id),
    ]
    threads = [threading.Thread(target=run, args=(work,)) for work in works]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    address = "emily.carter@acme.com"
    enabled = [i for i in identities.all("person") if not i.disabled and (i.email or "").lower() == address]
    assert len(enabled) == 1


def test_two_people_added_at_once_with_one_email_are_one(server):
    """Two administrators adding the same address at the same moment, on two instances: one is added, the other
    refused (the check and the insert are made under one lock for the address)."""
    from latet.server.identities import EmailTaken

    outcomes: list[str] = []

    def add() -> None:
        try:
            server.identities.add_person("Emily Carter", "emily.carter@acme.com")
            outcomes.append("added")
        except EmailTaken:
            outcomes.append("refused")

    threads = [threading.Thread(target=add) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes) == ["added", "refused", "refused", "refused"]


def test_disabling_a_person_ends_their_dashboard_and_stops_their_token(server, bare, admin):
    person, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    session = server.sign_ins.open(person.id)  # their dashboard, signed in from a link
    assert server.identities.by_secret(token) is not None and server.sign_ins.subject_of(session) == person.id
    bare.patch(f"{ADMIN}/identities/{person.id}", json={"disabled": True}, headers=admin)
    assert server.sign_ins.subject_of(session) is None  # ended by the call itself, before it answers
    assert server.identities.by_secret(token) is None
    bare.patch(f"{ADMIN}/identities/{person.id}", json={"disabled": False}, headers=admin)
    assert server.sign_ins.subject_of(session) is None  # their sessions ended: a new link signs them in again
    assert server.identities.by_secret(token) is not None  # their computer's token works again


def test_an_api_keys_settings_are_set_by_an_administrator_and_a_persons_are_theirs(server, client, admin):
    share_rules(server)  # a key's own terms over terms set for everyone: an edition that shares them, as E1/E6 will
    server.shared.set(Config(terms=[TermConfig(value="Project Falcon", type="PROJECT")]), actor="admin:a_elsewhere0")
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    assert [t["value"] for t in settings["terms"]] == ["Project Falcon"]
    assert client.put(path, json={**settings, "etag": "changed-meanwhile"}, headers=admin).status_code == 409
    mine = {**settings, "terms": [*settings["terms"], {"value": "Mercury Labs", "type": "CLIENT"}]}
    changed = client.put(path, json=mine, headers=admin)
    assert changed.status_code == 200
    assert [t["value"] for t in changed.json()["settings"]["terms"]] == ["Project Falcon", "Mercury Labs"]
    dropped = client.put(path, json={**settings, "terms": []}, headers=admin)
    assert dropped.status_code == 409 and "1 term" in dropped.json()["error"] and "Falcon" not in dropped.text
    on_server = {**settings, "upstreams": [{"name": "mine", "url": "https://127.0.0.1:8443"}]}
    assert client.put(path, json=on_server, headers=admin).status_code == 400
    person, _ = server.identities.add_person("Emily Carter")
    assert client.get(f"{ADMIN}/identities/{person.id}/settings", headers=admin).status_code == 409
    [entry] = server.audit.query(action="settings.changed")
    assert (entry.target, entry.details["keys"]) == (key["identity"]["id"], ["terms"])
    assert "Mercury" not in repr(entry.details)


def test_what_the_administration_sets_for_an_api_key_is_approved_with_it(server, client, admin):
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    lowered = {**settings, "profile": "secrets"}  # lowers protection: a person would be asked, a key has no one
    assert client.put(path, json=lowered, headers=admin).status_code == 200
    runtime = _runtime(server, key["identity"]["id"])
    assert runtime.config.profile == "secrets" and runtime.settings.reviewed().profile == "secrets"


def test_an_api_keys_settings_whose_approval_cannot_be_kept_say_so(server, client, admin, monkeypatch):
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    runtime = _runtime(server, key["identity"]["id"])

    def lost(_config):
        raise OSError("the settings of u_x could not be read or kept in the database (OperationalError)")

    monkeypatch.setattr(runtime.settings, "save_reviewed", lost)
    answer = client.put(path, json={**settings, "profile": "max"}, headers=admin)
    assert answer.status_code == 503 and "OperationalError" not in answer.text  # its words, never the store's


def test_an_api_keys_settings_not_kept_are_not_run_either(server, client, admin, monkeypatch):
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    runtime = _runtime(server, key["identity"]["id"])

    def lost(_config):
        raise OSError("the settings of u_x could not be read or kept in the database (OperationalError)")

    monkeypatch.setattr(runtime.settings, "save", lost)  # applied in memory first (E0a's order), then not kept
    assert client.put(path, json={**settings, "profile": "secrets"}, headers=admin).status_code == 503
    assert client.get(path, headers=admin).json()["settings"]["profile"] == "balanced"  # made again from PostgreSQL


def test_a_destination_whose_name_does_not_answer_is_waited_for_a_bounded_time(server, client, admin, monkeypatch):
    never = threading.Event()
    monkeypatch.setattr(egress, "RESOLVE_SECONDS", 0.5)
    monkeypatch.setattr(egress, "resolve", lambda _host, _port: never.wait(30) and [])
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    mine = {**settings, "upstreams": [{"name": "mine", "url": "https://llm.slow.example"}]}
    started = time.monotonic()
    try:
        assert client.put(path, json=mine, headers=admin).status_code == 200  # judged when the server connects
        assert time.monotonic() - started < 5
    finally:
        never.set()


def test_an_api_keys_settings_changed_by_another_instance_are_refused_as_stale(server, client, admin):
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]  # its space here reads its settings now
    identity_id = key["identity"]["id"]
    elsewhere = PgSettings(server.db, server.shared, identity_id, root_key(server.db, server.master, identity_id))
    elsewhere.save(elsewhere.load().model_copy(update={"terms": [TermConfig(value="Changed elsewhere")]}))
    stale = client.put(path, json={**settings, "profile": "secrets"}, headers=admin)  # a lowering, refused
    assert stale.status_code == 409 and "read them again" in stale.json()["error"]
    assert identity_id not in server.spaces._spaces  # nothing refused in force here, not even for a moment after
    now = client.get(path, headers=admin).json()["settings"]  # made again from PostgreSQL
    assert ([t["value"] for t in now["terms"]], now["profile"]) == (["Changed elsewhere"], "balanced")
    [entry] = server.audit.query(action="settings.change")
    assert entry.details["status"] == 409


def test_no_settings_name_a_file_or_a_variable_of_the_servers_for_the_reviewers_key(server, client, admin):
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    assert settings["llm_review"]["api_key_file"] is None
    for named in ({"api_key_file": "/etc/latet/master.key"}, {"api_key_env": "LATET_MASTER_KEY"}):
        reviewer = {**settings["llm_review"], **named}
        [name] = named
        shared, own = {"llm_review": reviewer}, {**settings, "llm_review": reviewer}
        for where, given in ((f"{ADMIN}/shared", shared), (path, own)):
            refused = client.put(where, json=given, headers=admin)
            assert refused.status_code == 400
            assert [d.split(":")[0] for d in refused.json()["details"]] == [f"llm_review.{name}"]
            assert "master" not in refused.text.lower()  # named by the setting, never by what it names
    with pytest.raises(ValueError, match=r"llm_review\.api_key_file"):
        server.shared.set(Config.model_validate({"llm_review": {"api_key_file": "/srv/key"}}), actor="admin:a_x")
    assert client.put(f"{ADMIN}/shared", json={"llm_review": {"api_key_env": "LATET_LLM_API_KEY"}},
                      headers=admin).status_code == 200  # fmt: skip
    assert client.put(path, json=settings, headers=admin).status_code == 200  # the default names nothing new


def test_an_api_keys_settings_naming_more_projects_than_a_server_keeps_are_refused(server, client, admin):
    """Past the cap of projects (Task 12) a save is the request's own fault, answered 400 like settings that are not
    valid, never 409 like a change that drops a shared item; nothing is kept."""
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    projects = [{"path": f"/srv/project-{i}"} for i in range(shared_module.PROJECTS_LIMIT + 1)]
    refused = client.put(path, json={**settings, "projects": projects}, headers=admin)
    assert refused.status_code == 400 and str(shared_module.PROJECTS_LIMIT) in refused.json()["error"]
    assert client.get(path, headers=admin).json()["settings"]["projects"] == settings["projects"]


def test_a_term_too_long_to_be_matched_in_a_part_is_refused_by_place_and_nothing_is_kept(server, client, admin):
    """In an API key's settings and its folders': 400 by place and the limit, never the term, and nothing kept (S-03).
    The shared settings take no terms at all: rules for everyone are the Enterprise Edition's."""
    long = "Falcon" + "x" * TERM_LONGEST
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    for wrong, place in (
        ({**settings, "terms": [{"value": long}]}, "terms.0"),
        ({**settings, "projects": [{"path": "/srv/billing", "terms": [{"value": long}]}]}, "projects.0.terms.0"),
    ):
        refused = client.put(path, json=wrong, headers=admin)
        assert refused.status_code == 400 and place in refused.text and str(TERM_LONGEST) in refused.text
        assert "xxxx" not in refused.text
    assert client.get(path, headers=admin).json()["settings"] == settings
    refused = client.put(f"{ADMIN}/shared", json={"terms": [{"value": "Orion"}, {"value": long}]}, headers=admin)
    assert refused.status_code == 400 and refused.json()["details"] == ["terms"] and "xxxx" not in refused.text
    assert "xxxx" not in repr([e.details for e in server.audit.query()])


def test_an_api_keys_settings_too_large_to_keep_are_refused(server, client, admin, monkeypatch):
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    monkeypatch.setattr(space_settings_module, "SETTINGS_LIMIT", 1)
    assert client.put(path, json={**settings, "profile": "max"}, headers=admin).status_code == 413


def test_an_api_keys_etag_is_compared_under_the_lock_its_change_takes(server, client, admin, monkeypatch):
    key, path = _key(client, admin)
    read = client.get(path, headers=admin).json()
    runtime = _runtime(server, key["identity"]["id"])
    first = runtime.update_config

    def meanwhile(change, **kwargs):  # another change lands while this one waits for the lock
        first(lambda current: current.model_copy(update={"profile": Profile.MAX}))
        return first(change, **kwargs)

    monkeypatch.setattr(runtime, "update_config", meanwhile)
    mine = {**read["settings"], "terms": [{"value": "Mercury Labs", "type": "CLIENT"}], "etag": read["etag"]}
    assert client.put(path, json=mine, headers=admin).status_code == 409
    assert [t.value for t in runtime.config.terms] == [] and runtime.config.profile == Profile.MAX


def test_the_shared_settings_refuse_what_is_each_identitys_and_say_what_changed(server, client, admin):
    refused = client.put(f"{ADMIN}/shared", json={"identity": {"usernames": ["emily"]}, "capture_dir": "/tmp/raw"},
                         headers=admin)  # fmt: skip
    assert refused.status_code == 400
    assert [d.split(":")[0] for d in refused.json()["details"]] == ["identity", "capture_dir"]
    done = client.put(f"{ADMIN}/shared", json={"history": "metadata", "retention_days": 3}, headers=admin)
    assert (
        done.status_code == 200
        and client.get(f"{ADMIN}/shared", headers=admin).json()["settings"]["history"] == "metadata"
    )
    [entry] = server.audit.query(action="shared.changed")
    assert sorted(entry.details["keys"]) == ["history", "retention_days"]
    read = client.get(f"{ADMIN}/shared", headers=admin).json()  # written whole, with the etag read (E0c's commands)
    full = {**read["settings"], "history": "full"}
    assert client.put(f"{ADMIN}/shared", json={**full, "etag": "changed-meanwhile"}, headers=admin).status_code == 409
    written = client.put(f"{ADMIN}/shared", json={**full, "etag": read["etag"]}, headers=admin).json()
    assert written["settings"]["history"] == "full" and written["etag"] != read["etag"]


def test_the_shared_etag_is_compared_under_the_lock_every_change_takes(server, client, admin, monkeypatch):
    read = client.get(f"{ADMIN}/shared", headers=admin).json()
    first = server.shared.set

    def meanwhile(config, **kwargs):  # another administrator's change commits while this one waits for the lock
        first(Config(history="off"), actor="admin:a_elsewhere0")
        return first(config, **kwargs)

    monkeypatch.setattr(server.shared, "set", meanwhile)
    mine = {**read["settings"], "history": "metadata", "etag": read["etag"]}
    assert client.put(f"{ADMIN}/shared", json=mine, headers=admin).status_code == 409
    server.shared.forget()
    assert server.shared.get().history == "off"
    assert server.audit.query(action="shared.changed") == []


def test_a_shared_change_written_blind_names_exactly_what_it_changed(server, bare, admin, monkeypatch):
    first, landed = server.shared.set, []
    elsewhere = SharedSettings(server.db, server.bus, server.master)  # another instance's: this one's copy stays

    def once(config, **kwargs):  # another instance's change lands between this one's reading and its lock, once
        if not landed:
            landed.append(elsewhere.set(Config(history="off"), actor="admin:a_elsewhere0"))
        return first(config, **kwargs)

    monkeypatch.setattr(server.shared, "set", once)
    done = bare.put(f"{ADMIN}/shared", json={"retention_days": 3, "history": "off"}, headers=admin)
    assert done.status_code == 200
    [entry] = server.audit.query(action="shared.changed")
    assert entry.details["keys"] == ["retention_days"]  # read again once the other change landed: only its own


def test_a_shared_change_and_its_entry_are_kept_together_or_not_at_all(server, client, admin, monkeypatch):
    first = server.audit.record

    def refused(*args, conn=None, **kwargs):
        if conn is not None:
            raise RuntimeError("the log refuses this entry")
        return first(*args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    answer = client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin)
    assert answer.status_code == 500
    server.shared.forget()
    assert server.shared.get().history != "metadata"  # the change was rolled back with its entry
    [entry] = server.audit.query(action="shared.change")
    assert (entry.details["status"], entry.actor) == (500, _actor(server, admin))


def test_the_shared_settings_are_answered_as_postgresql_has_them(server, bare, admin):
    assert bare.get(f"{ADMIN}/shared", headers=admin).json()["settings"]["history"] == "full"
    assert server.shared.get().history == "full"  # this instance keeps a copy
    elsewhere = SharedSettings(server.db, server.bus, server.master)
    elsewhere.set(Config(history="metadata"), actor="admin:a_elsewhere0")  # its announcement is not heard here
    assert server.shared.get().history == "full"  # the copy here is behind
    read = bare.get(f"{ADMIN}/shared", headers=admin).json()
    assert read["settings"]["history"] == "metadata" and read["etag"] == digest(Config(history="metadata"))
    assert server.shared.get().history == "full"  # the read kept nothing, and forgot nothing


def test_shared_settings_that_do_not_open_are_replaced_whole_and_nothing_of_them_is_read(server, client, admin,
                                                                                         caplog):  # fmt: skip
    with server.db.connection() as conn:  # written by hand
        conn.execute(
            "INSERT INTO settings (owner, kind, data, changed) VALUES ('', 'shared', %s, 0)"
            " ON CONFLICT (owner, kind) DO UPDATE SET data = EXCLUDED.data",
            (b'{"terms": [{"value": "Mercury Labs"}]}',),
        )
    with caplog.at_level(logging.DEBUG):
        read = client.get(f"{ADMIN}/shared", headers=admin)
        assert read.status_code == 500 and "without an etag" in read.json()["error"] and "Mercury" not in read.text
        mine = {"history": "metadata", "etag": digest(Config())}
        stale = client.put(f"{ADMIN}/shared", json=mine, headers=admin)  # what it read cannot be what is there
        assert stale.status_code == 409 and "without an etag" in stale.json()["error"]
        assert client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin).status_code == 200
    assert client.get(f"{ADMIN}/shared", headers=admin).json()["settings"]["history"] == "metadata"
    [entry] = server.audit.query(action="shared.changed")
    assert entry.details == {"unreadable": True, "status": 200}
    assert "Mercury" not in caplog.text


def test_shared_settings_too_large_to_keep_are_refused(server, client, admin, monkeypatch):
    monkeypatch.setattr(shared_module, "SETTINGS_LIMIT", 1)
    answer = client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin)
    assert answer.status_code == 413 and "more than a server keeps" in answer.json()["error"]
    assert client.get(f"{ADMIN}/shared", headers=admin).json()["settings"]["history"] == "full"
    [entry] = server.audit.query(action="shared.change")
    assert entry.details == {"status": 413}


def test_bodies_answers_and_the_log_are_made_in_the_administrations_threads(server, client, admin, monkeypatch):
    threads = []
    observed_ones = (
        (admin_module, "_parsed"), (admin_module, "_settings_answer"), (admin_module, "_shared_answer"),
        (server.audit, "read_page"),
    )  # fmt: skip
    for target, name in observed_ones:
        real = getattr(target, name)

        def observed(*args, name=name, real=real, **kwargs):
            threads.append((name, threading.current_thread().name))
            return real(*args, **kwargs)

        monkeypatch.setattr(target, name, observed)
    assert client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin).status_code == 200
    assert client.get(f"{ADMIN}/shared", headers=admin).status_code == 200
    _, path = _key(client, admin)
    assert client.get(path, headers=admin).status_code == 200
    answer = client.get(f"{ADMIN}/log?limit={LOG_MOST}", headers=admin)
    assert answer.status_code == 200
    page = answer.json()
    assert page["entries"] and "next_cursor" in page
    assert sum(operation == "read_page" for operation, _ in threads) == 1
    # An older page uses its cursor with the same original filters, including the exclusive until.
    assert client.get(f"{ADMIN}/log?limit={LOG_MOST + 1}", headers=admin).status_code == 400
    assert sum(operation == "read_page" for operation, _ in threads) == 1  # invalid input starts no page work
    assert {operation for operation, _ in threads} == {"_parsed", "_settings_answer", "_shared_answer", "read_page"}
    assert len(threads) >= 6 and all(name.startswith("latet-admin") for _, name in threads), threads


def test_a_call_printed_names_neither_its_address_nor_its_details():
    call = _Call("tokens.revoke", "203.0.113.9", "u_emilycarte", details={"token": "t_abcdefghij"})
    shown = repr(call)
    assert "203.0.113.9" not in shown and "t_abcdefghij" not in shown and "tokens.revoke" in shown


async def test_a_turn_is_kept_only_while_it_is_held_or_waited_for():
    turns, order = _Turns(), []

    async def save(key, n):
        async with turns.turn(key):
            order.append((key, n, "in"))
            await asyncio.sleep(0.01)
            order.append((key, n, "out"))

    await asyncio.gather(*(save("u_emilycarte", n) for n in range(3)), *(save(f"u_key{n:07d}", n) for n in range(50)))
    assert [(n, step) for key, n, step in order if key == "u_emilycarte"] == [
        (0, "in"), (0, "out"), (1, "in"), (1, "out"), (2, "in"), (2, "out"),
    ]  # fmt: skip
    assert len(turns) == 0  # nothing kept of the 51 identities saved
    holding, release = asyncio.Event(), asyncio.Event()

    async def hold():
        async with turns.turn("u_emilycarte"):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await holding.wait()
    waiting = asyncio.create_task(save("u_emilycarte", 9))
    await asyncio.sleep(0.01)
    waiting.cancel()  # a waiter that gives up leaves nothing behind
    with contextlib.suppress(asyncio.CancelledError):
        await waiting
    release.set()
    await holder
    assert len(turns) == 0


CANARY = "Zanzibar Holdings 4417"


def test_the_shared_settings_take_no_rules_for_everyone_and_keep_those_an_earlier_version_kept(server, client, admin):
    """Rules for everyone are the Enterprise Edition's: a change of the shared settings that holds one is refused, by
    the rules' names, and changes nothing. Rules an earlier version kept in them are answered apart, counted on the
    server's status (never a value), kept by every change of the settings, and deleted only when asked."""
    asked = {"terms": [{"value": CANARY}], "profile": "max", "history": "metadata"}
    refused = client.put(f"{ADMIN}/shared", json=asked, headers=admin)
    assert refused.status_code == 400 and "Enterprise Edition" in refused.json()["error"]
    assert refused.json()["details"] == ["profile", "terms"] and CANARY not in refused.text
    assert server.audit.query(action="shared.changed") == [] and server.shared.stored() == Config()
    server.shared.set(Config(terms=[TermConfig(value=CANARY)], profile=Profile.MAX), actor="admin:a_elsewhere0")
    read = client.get(f"{ADMIN}/shared", headers=admin).json()
    assert set(read["settings"]) == set(SETTING_KEYS) and read["kept_rules"]["profile"] == "max"
    assert [t["value"] for t in read["kept_rules"]["terms"]] == [CANARY]
    written = client.put(
        f"{ADMIN}/shared", json={**read["settings"], "history": "metadata", "etag": read["etag"]}, headers=admin
    ).json()
    assert written["settings"]["history"] == "metadata" and set(written["kept_rules"]) == {"terms", "profile"}
    assert server.shared.stored().profile == Profile.MAX and server.shared.get().profile == Profile.BALANCED
    status = client.get(f"{ADMIN}/status", headers=admin).json()
    [line] = status["edition"]
    assert line["status"] == "warn" and "1 term" in line["detail"] and "profile" in line["detail"]
    assert CANARY not in json.dumps(status)
    assert client.put(f"{ADMIN}/shared", json={"forget_kept_rules": "yes"}, headers=admin).status_code == 400
    forgot = client.put(
        f"{ADMIN}/shared",
        json={**written["settings"], "etag": written["etag"], "forget_kept_rules": True},
        headers=admin,
    ).json()
    assert forgot["kept_rules"] == {} and forgot["settings"]["history"] == "metadata"
    assert server.shared.stored().terms == [] and server.shared.stored().profile == Profile.BALANCED
    keys = [sorted(e.details.get("keys") or []) for e in server.audit.query(action="shared.changed")]
    assert ["profile", "terms"] in keys and CANARY not in repr([e.details for e in server.audit.query()])
    assert client.get(f"{ADMIN}/status", headers=admin).json()["edition"] == []


def test_a_shared_change_that_changes_nothing_is_recorded_as_such(server, client, admin):
    read = client.get(f"{ADMIN}/shared", headers=admin).json()
    assert client.put(f"{ADMIN}/shared", json={**read["settings"], "etag": read["etag"]}, headers=admin).json() == read
    assert [e.action for e in server.audit.query(action="shared.*")] == ["shared.change", "shared.read"]


@pytest.mark.parametrize(
    "body",
    [b"", b'{"terms": [{"value": "Proj', b"[]", b"null", b'"terms"', b"7", b'{"profile": NaN}', b"[" * 100_000],
    ids=["none", "truncated", "list", "null", "text", "number", "nan", "deep"],
)
def test_a_body_that_is_not_settings_changes_nothing(server, client, admin, body):
    client.put(f"{ADMIN}/shared", json={"history": "metadata", "retention_days": 3}, headers=admin)
    _, path = _key(client, admin)
    shared, own = client.get(f"{ADMIN}/shared", headers=admin).json(), client.get(path, headers=admin).json()
    json_body = {**admin, "content-type": "application/json"}
    assert client.put(f"{ADMIN}/shared", content=body, headers=json_body).status_code == 400
    assert client.put(path, content=body, headers=json_body).status_code == 400
    assert (
        client.get(f"{ADMIN}/shared", headers=admin).json() == shared and client.get(path, headers=admin).json() == own
    )


def test_an_explicit_empty_object_changes_nothing(server, client, admin):
    client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin)
    assert client.put(f"{ADMIN}/shared", json={}, headers=admin).json()["settings"]["history"] == "metadata"


def test_a_shared_write_changes_what_it_names_alone_and_refuses_a_key_that_is_no_setting(server, client, admin):
    """A write that names some of the shared settings changes those alone: the destination set before stays. A key that
    is no setting (a typo, or a value pasted by mistake) is refused, counted and never quoted, and nothing is written."""
    destination = {"name": "llm", "url": "https://llm.acme.com"}
    assert client.put(f"{ADMIN}/shared", json={"upstreams": [destination]}, headers=admin).status_code == 200
    done = client.put(f"{ADMIN}/shared", json={"history": "metadata"}, headers=admin)
    assert done.status_code == 200, done.text
    assert [(d["name"], d["url"]) for d in done.json()["settings"]["upstreams"]] == [("llm", "https://llm.acme.com")]
    assert done.json()["settings"]["history"] == "metadata"
    refused = client.put(f"{ADMIN}/shared", json={"histroy": "off", "Globex Corp": 1}, headers=admin)
    assert refused.status_code == 400 and "2 keys" in refused.text
    assert "histroy" not in refused.text and "Globex" not in refused.text
    assert client.get(f"{ADMIN}/shared", headers=admin).json()["settings"]["history"] == "metadata"


def test_an_api_keys_write_changes_what_it_names_alone_and_refuses_a_key_that_is_no_setting(server, client, admin):
    _, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    assert client.put(path, json={**settings, "terms": [{"value": "Mercury Labs"}]}, headers=admin).status_code == 200
    done = client.put(path, json={"profile": "max"}, headers=admin)
    assert done.status_code == 200, done.text
    assert [t["value"] for t in done.json()["settings"]["terms"]] == ["Mercury Labs"]
    assert done.json()["settings"]["profile"] == "max"
    refused = client.put(path, json={"profil": "secrets"}, headers=admin)
    assert refused.status_code == 400 and "1 key" in refused.text and "profil" not in refused.text
    assert client.get(path, headers=admin).json()["settings"]["profile"] == "max"


def test_settings_that_are_not_valid_are_refused_by_place_and_never_quoted(server, client, admin, caplog):
    _, path = _key(client, admin)
    bad = {"allow_patterns": ["(?P<client>Mercury Labs"], "terms": [{"value": 7}], "Globex Corp": 1}
    shared_bad = {"retention_days": "Mercury Labs", "upstreams": [{"name": 7}], "Globex Corp": 1}
    with caplog.at_level(logging.DEBUG):
        for where, given, places in (
            (f"{ADMIN}/shared", shared_bad, ("retention_days", "upstreams.0")),
            (path, bad, ("allow_patterns.0", "terms.0.value")),
        ):
            refused = client.put(where, json=given, headers=admin)
            assert refused.status_code == 400
            assert all(place in refused.text for place in places), refused.text
            for value in ("Mercury", "Globex"):
                assert value not in refused.text
    assert "Mercury" not in caplog.text and "Globex" not in caplog.text
    told = repr([(e.action, e.details) for e in server.audit.query()])
    assert "Mercury" not in told and "Globex" not in told


def test_the_log_is_read_by_action_and_time(server, client, admin):
    client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter"}, headers=admin)
    entries = client.get(f"{ADMIN}/log?action=person.*", headers=admin).json()["entries"]
    assert [e["action"] for e in entries] == ["person.added"]
    assert client.get(f"{ADMIN}/log?since=soon", headers=admin).status_code == 400


@pytest.mark.parametrize(
    "query", ["since=nan", "until=inf", "since=-inf", "since=1e999", "limit=0", "limit=-1", "limit=1.5", "since="]
)
def test_the_log_is_read_with_finite_times_and_a_limit_from_one(server, client, admin, query):
    assert client.get(f"{ADMIN}/log?{query}", headers=admin).status_code == 400


def test_an_expired_administration_token_is_refused_and_the_refusal_says_so(schema, prefix, clock):
    server = open_test_server(schema, prefix, clock=clock)
    try:
        expiring = {"x-latet-admin": server.admin.issue("a day", days=1)}
        with TestClient(create_server_app(server), base_url=URL) as client:
            assert client.get(f"{ADMIN}/status", headers=expiring).status_code == 200
            clock.now += 2 * 86400
            assert client.get(f"{ADMIN}/status", headers=expiring).status_code == 401
        refused = [e for e in server.audit.query(action="status.read") if e.details["status"] == 401]
        assert [e.details.get("reason") for e in refused] == ["expired"]
    finally:
        close_test_server(server)


def test_the_log_verifies_and_the_status_says_how_full_the_vaults_are(server, client, admin):
    client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter"}, headers=admin)
    client.get(f"{ADMIN}/identities", headers=admin)
    checked = client.get(f"{ADMIN}/log/verify", headers=admin).json()
    assert checked["ok"] is True and checked["checked"] >= 2 and checked["broken_at"] is None
    assert checked["head"]["id"] == server.audit.query(limit=1)[0].id - 1  # the newest before its own entry
    status = client.get(f"{ADMIN}/status", headers=admin).json()
    assert status["vault"] == {"limit": server.settings.vault_limit, "near": []}
    assert status["audit"]["chain"] in ("ok", "unknown")
    assert [e.action for e in server.audit.query(action="log.verify")] == ["log.verify"]


def test_a_walk_of_the_log_that_fails_is_a_failed_check(server, client, admin, monkeypatch):
    def broken():
        raise RuntimeError("a NOT NULL dropped by hand")

    monkeypatch.setattr(server.audit, "verify", broken)
    checked = client.get(f"{ADMIN}/log/verify", headers=admin)
    assert checked.status_code == 200 and checked.json()["ok"] is False  # never "unchecked"


# --- what the log keeps -------------------------------------------------------------------------------------------


def test_a_token_named_in_a_query_reaches_no_log(server, client, admin):
    person, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    path = f"{ADMIN}/identities/{person.id}/tokens"
    for given in (token, f"t_aaaaaaaaaa{token}", ""):  # a secret, an id with one after it, nothing ("every token"?)
        assert client.delete(f"{path}?token={given}", headers=admin).status_code == 400
    assert server.identities.by_secret(token) is not None
    for given in (token, f"{person.id}{token}"):
        assert client.get(f"{ADMIN}/identities/{given}", headers=admin).status_code == 404
    told = repr([(e.target, e.details, e.address) for e in server.audit.query()])
    assert token not in told and "latet_pat_" not in told


def test_a_name_that_is_no_name_is_refused_and_recorded(server, client, admin):
    for given in ({"first": "Emily"}, ["Emily"], 7, None, ZWSP + RLO + LRI):
        made = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": given}, headers=admin)
        assert made.status_code == 400
    assert client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter", "email": 7},
                       headers=admin).status_code == 400  # fmt: skip
    assert client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI", "email": "ci@acme.com"},
                       headers=admin).status_code == 400  # fmt: skip
    emily, _ = server.identities.add_person("Emily Carter")
    for given in ({"name": ZWSP}, {"name": ["Emily"]}, {"email": 7}, {"disabled": "yes"}, {"disable": True}):
        assert client.patch(f"{ADMIN}/identities/{emily.id}", json=given, headers=admin).status_code == 400
    assert server.identities.get(emily.id).name == "Emily Carter" and not server.identities.get(emily.id).disabled
    statuses = [e.details["status"] for e in server.audit.query(action="identity.*")]
    assert statuses == [400] * 12


def test_reading_an_identity_forgets_nothing_the_instance_keeps(server, bare, admin):
    emily, code = server.identities.add_person("Emily Carter")
    _, token, _ = server.identities.enrol(code, "laptop")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    assert server.identities.by_secret(token) is not None and server.identities.holds(secret=token)
    generation = server.identities._generation
    paths = (daniel.id, emily.id, f"{emily.id}/tokens", f"{emily.id}/settings")
    statuses = [bare.get(f"{ADMIN}/identities/{path}", headers=admin).status_code for path in paths]
    assert statuses == [200, 200, 200, 409]
    assert server.identities._generation == generation and server.identities.holds(secret=token)


def test_a_removal_or_a_code_that_finds_no_one_is_recorded_as_doing_nothing(server, client, admin, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter")
    daniel, _ = server.identities.add_person("Daniel Okafor")
    assert client.delete(f"{ADMIN}/identities/{emily.id}", headers=admin).json() == {"ok": True}
    assert client.delete(f"{ADMIN}/identities/{emily.id}", headers=admin).status_code == 404  # removed already
    monkeypatch.setattr(server.identities, "remove", lambda _identity_id, **_: False)  # removed meanwhile, elsewhere
    assert client.delete(f"{ADMIN}/identities/{daniel.id}", headers=admin).status_code == 404
    monkeypatch.setattr(server.identities, "code", lambda _identity_id, **_: None)
    assert client.post(f"{ADMIN}/identities/{daniel.id}/code", headers=admin).status_code == 404
    done = [(e.action, e.details["status"]) for e in reversed(server.audit.query(actor=_actor(server, admin)))]
    assert done == [("person.removed", 200), ("identity.remove", 404), ("identity.remove", 404), ("code.issue", 404)]


def test_a_patch_that_changes_nothing_is_recorded_as_such(server, client, admin):
    made = client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Emily Carter",
                                                    "email": "emily.carter@acme.com"}, headers=admin)  # fmt: skip
    path = f"{ADMIN}/identities/{made.json()['identity']['id']}"
    for body in ({}, {"name": "Emily Carter"}, {"email": "emily.carter@acme.com"}, {"disabled": False}):
        assert client.patch(path, json=body, headers=admin).status_code == 200
    assert client.patch(path, json={"name": "Emily J. Carter"}, headers=admin).status_code == 200
    done = [e.action for e in reversed(server.audit.query(action="*")) if e.actor == _actor(server, admin)]
    assert done == ["person.added", *["identity.change"] * 4, "person.updated"]
    [renamed] = server.audit.query(action="person.updated")
    assert renamed.details["fields"] == ["name"] and "Emily" not in repr(renamed.details)


def test_a_patch_names_the_fields_it_changed_never_a_rename_made_meanwhile(server, client, admin, monkeypatch):
    emily, _ = server.identities.add_person("Emily Carter", "emily.carter@acme.com")
    update = server.identities.update

    def renamed_meanwhile(identity_id, **kwargs):
        with server.db.connection() as conn:  # another administrator's rename, committed just before this change
            conn.execute("UPDATE identities SET name = 'Emily J. Carter' WHERE id = %s", (identity_id,))
        return update(identity_id, **kwargs)

    monkeypatch.setattr(server.identities, "update", renamed_meanwhile)
    path = f"{ADMIN}/identities/{emily.id}"
    assert client.patch(path, json={"email": "ej@acme.com"}, headers=admin).status_code == 200
    [changed] = server.audit.query(action="person.updated")
    assert changed.details["fields"] == ["email"]  # the rename is the other administrator's, and their entry's


# --- what ends at once ------------------------------------------------------------------------------------------


def test_revoking_ends_the_persons_sessions_at_once_whatever_the_count(server, bare, admin):
    person, code = server.identities.add_person("Emily Carter")
    server.identities.enrol(code, "laptop")
    first = server.sign_ins.open(person.id)
    assert bare.delete(f"{ADMIN}/identities/{person.id}/tokens", headers=admin).json() == {"revoked": 1}
    assert server.sign_ins.subject_of(first) is None
    again = server.sign_ins.open(person.id)  # a session no token stands behind any more
    assert bare.delete(f"{ADMIN}/identities/{person.id}/tokens", headers=admin).json() == {"revoked": 0}
    assert server.sign_ins.subject_of(again) is None


def test_rotating_a_key_ends_its_aws_sessions_at_once(server, bare, admin):
    key, _ = server.identities.add_key("Billing CI")
    aws = ValkeyAwsSessions(server.bus, server.master, key.id)
    session = aws.add_session(AwsCredentials("AKIAEXAMPLE", "secret-of-billing", None))
    assert bare.post(f"{ADMIN}/identities/{key.id}/rotate", headers=admin).status_code == 200
    assert not aws.has_session(session)


def test_removing_an_identity_ends_its_sessions_at_once(server, bare, admin):
    person, _ = server.identities.add_person("Emily Carter")
    session = server.sign_ins.open(person.id)
    assert bare.delete(f"{ADMIN}/identities/{person.id}", headers=admin).json() == {"ok": True}
    assert server.sign_ins.subject_of(session) is None


@pytest.mark.parametrize("change", ["revoke", "rotate", "disable", "remove"])
async def test_a_change_that_closes_a_space_closes_it_here_before_it_answers(server, admin, change):
    server.listening.set()  # spaces are built here and no message is heard: only the call itself can close one
    try:
        if change == "rotate":
            identity, _ = server.identities.add_key("Billing CI")
        else:
            identity, code = server.identities.add_person("Emily Carter")
            server.identities.enrol(code, "laptop")
        await server.spaces.runtime(identity)
        assert identity.id in server.spaces._spaces
        one = f"{ADMIN}/identities/{identity.id}"
        method, path, body = {
            "revoke": ("DELETE", f"{one}/tokens", None),
            "rotate": ("POST", f"{one}/rotate", None),
            "disable": ("PATCH", one, {"disabled": True}),
            "remove": ("DELETE", one, None),
        }[change]
        async with _from(server, "203.0.113.9") as client:
            answer = await client.request(method, path, json=body, headers=admin)
        assert answer.status_code == 200 and identity.id not in server.spaces._spaces  # Ruling 22, at once here
    finally:
        await server.spaces.close()


# --- every call recorded, whatever happens ----------------------------------------------------------------------


def test_a_call_that_fails_is_recorded_with_its_status_and_says_nothing_of_its_error(server, client, admin,
                                                                                     monkeypatch, caplog):  # fmt: skip
    emily, _ = server.identities.add_person("Emily Carter")

    def broken(_identity_id):
        raise RuntimeError("the words of Emily Carter's driver")

    monkeypatch.setattr(server.identities, "tokens", broken)
    with caplog.at_level(logging.DEBUG):
        failed = client.get(f"{ADMIN}/identities/{emily.id}", headers=admin)
    assert failed.status_code == 500 and "Emily" not in failed.text
    [entry] = server.audit.query(action="identity.read")
    assert (entry.actor, entry.target, entry.details["status"]) == (_actor(server, admin), emily.id, 500)
    assert "Emily" not in caplog.text and "RuntimeError" in caplog.text


def test_a_call_that_cannot_be_recorded_withholds_its_answer(server, client, admin, monkeypatch, caplog):
    emily, _ = server.identities.add_person("Emily Carter")
    first = server.audit.record

    def refused(actor, action, *args, **kwargs):
        if action == "tokens.list":
            raise RuntimeError("the log is gone")
        return first(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    with caplog.at_level(logging.ERROR):
        answer = client.get(f"{ADMIN}/identities/{emily.id}/tokens", headers=admin)
    assert answer.status_code == 500 and "tokens" not in answer.json()
    assert "tokens.list" in caplog.text and "RuntimeError" in caplog.text and "the log is gone" not in caplog.text


def test_a_change_whose_entry_cannot_be_written_is_not_made(server, client, admin, monkeypatch, caplog):
    emily, first_code = server.identities.add_person("Emily Carter")
    key, _ = server.identities.add_key("Billing CI")
    kept = server.audit.record

    def refused(actor, action, *args, conn=None, **kwargs):
        if conn is not None:  # an entry written last in its change's transaction
            raise RuntimeError("the log is gone")
        return kept(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    one = f"{ADMIN}/identities/{emily.id}"
    with caplog.at_level(logging.ERROR):
        answers = [
            client.post(f"{ADMIN}/identities", json={"kind": "person", "name": "Daniel Okafor"}, headers=admin),
            client.post(f"{one}/code", headers=admin),
            client.patch(one, json={"name": "Emily J. Carter", "disabled": True}, headers=admin),
            client.post(f"{ADMIN}/identities/{key.id}/rotate", headers=admin),
            client.delete(one, headers=admin),
        ]
    assert [a.status_code for a in answers] == [500] * 5
    assert not any(field in a.json() for a in answers for field in ("code", "key", "identity"))
    assert "RuntimeError" in caplog.text and "the log is gone" not in caplog.text
    assert [(i.name, i.disabled) for i in server.identities.all()] == [("Billing CI", False), ("Emily Carter", False)]
    assert server.identities.enrol(first_code, "laptop") is not None  # no new code: the one before still holds
    done = [(e.action, e.details) for e in reversed(server.audit.query(actor=_actor(server, admin)))]
    assert done == [
        ("identity.add", {"status": 500}), ("code.issue", {"status": 500}), ("identity.change", {"status": 500}),
        ("key.rotate", {"status": 500}), ("identity.remove", {"status": 500}),
    ]  # fmt: skip


def test_an_api_keys_approval_and_its_entry_are_kept_together_or_not_at_all(server, client, admin, monkeypatch):
    key, path = _key(client, admin)
    settings = client.get(path, headers=admin).json()["settings"]
    runtime = _runtime(server, key["identity"]["id"])
    kept, failing = server.audit.record, {"settings.changed"}

    def refused(actor, action, *args, **kwargs):
        if action in failing:
            raise RuntimeError("the log is gone")
        return kept(actor, action, *args, **kwargs)

    monkeypatch.setattr(server.audit, "record", refused)
    assert client.put(path, json={**settings, "profile": "secrets"}, headers=admin).status_code == 503
    assert runtime.settings.reviewed().profile == "balanced"  # its approval was not kept, with its entry
    [entry] = server.audit.query(action="settings.change")
    assert entry.details == {"keys": ["profile"], "approved": False, "status": 503}  # applied, not approved
    failing.clear()
    assert client.put(path, json={**settings, "profile": "secrets"}, headers=admin).status_code == 200
    [changed] = server.audit.query(action="settings.changed")  # written with the approval, and once
    assert changed.details == {"keys": ["profile"], "status": 200}
    assert len(server.audit.query(action="settings.change")) == 1


# --- refused calls, limited by address ----------------------------------------------------------------------------


def _counted(server, monkeypatch):
    """Every lookup of an administration token from now on, from any thread."""
    lookups = []
    looked = server.admin.lookup
    monkeypatch.setattr(server.admin, "lookup", lambda secret: lookups.append(secret) or looked(secret))
    return lookups


def _from(server, where):
    """An asynchronous client of the app with no lifespan, whose calls come from the address ``where``: many calls at
    once, as a flood makes them."""
    transport = httpx.ASGITransport(app=create_server_app(server), client=(where, 40000))
    return httpx.AsyncClient(transport=transport, base_url=URL)


def _guesses(n, tag=0):
    return [{"x-latet-admin": f"latet_adm_{tag:010d}{i:033d}"} for i in range(n)]


async def _lookups_free():
    """Every thread of the lookups' pool answers at once, all of them together: none is held by slower work (a save, an
    entry of the log, an end of sessions, a store call), which is the administration's pool's."""
    barrier = threading.Barrier(CREDENTIAL_THREADS, timeout=3)
    await asyncio.wait_for(asyncio.gather(*(off_loop(barrier.wait) for _ in range(CREDENTIAL_THREADS))), 5)


def test_refused_calls_are_limited_by_address_with_no_lookup_and_one_entry(server, admin, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    lookups = _counted(server, monkeypatch)
    with TestClient(create_server_app(server), base_url=URL) as client:
        answers = [client.get(f"{ADMIN}/status", headers=g) for g in _guesses(5)]
        assert [a.status_code for a in answers] == [401, 401, 401, 429, 429]
        assert answers[-1].headers["retry-after"] == "60" and "retry-after" not in answers[0].headers
        assert len(lookups) == 3  # none beyond the limit
        assert client.get(f"{ADMIN}/status", headers=admin).status_code == 429  # the address is limited, whoever asks
        elsewhere = TestClient(create_server_app(server), base_url=URL, client=("10.0.3.7", 50000))
        assert elsewhere.get(f"{ADMIN}/status", headers=admin).status_code == 200
    entries = server.audit.query(action="status.read")
    assert sorted(e.details["status"] for e in entries) == [200, 401, 401, 401, 429]  # one entry for the window
    [limited] = [e for e in entries if e.details["status"] == 429]
    assert (limited.actor, limited.address, limited.details["reason"]) == ("unknown", "testclient", "limited")


async def test_a_burst_of_refused_calls_makes_no_more_lookups_than_the_limit(server, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    lookups = _counted(server, monkeypatch)
    async with _from(server, "198.51.100.7") as client:
        answers = await asyncio.gather(*(client.get(f"{ADMIN}/status", headers=g) for g in _guesses(200)))
    statuses = [a.status_code for a in answers]
    assert len(lookups) <= 3 and statuses.count(401) == len(lookups) and statuses.count(429) == 200 - len(lookups)
    entries = await asyncio.to_thread(server.audit.query, action="status.read")
    assert len(entries) <= 3 + 1  # its refusals, and one entry for the window


def test_a_valid_token_costs_its_address_nothing(server, client, admin, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    for guess in _guesses(2):
        assert client.get(f"{ADMIN}/status", headers=guess).status_code == 401
    misses = server.bus.rate_key(machines.miss_key("testclient"))
    counted = server.bus.get(misses)
    for _ in range(10):
        assert client.get(f"{ADMIN}/status", headers=admin).status_code == 200
    assert server.bus.get(misses) == counted == b"2"  # each slot given back
    assert [client.get(f"{ADMIN}/status", headers=g).status_code for g in _guesses(2, 1)] == [401, 429]


def test_an_address_past_its_limit_here_asks_no_thread_nor_valkey_for_its_calls(server, client, monkeypatch):
    """S1: once the limit has said an address is past it, this instance refuses its calls with no round trip until its
    window ends, whatever thread is free: a token it does not know (429, nothing looked up), and a call refused before
    its token is read, whose entry is the window's, written when the limit said so."""
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 2)
    assert [client.get(f"{ADMIN}/status", headers=g).status_code for g in _guesses(3)] == [401, 401, 429]
    assert [client.get(f"{ADMIN}/status").status_code for _ in range(3)] == [401] * 3  # no token: a count of its own
    lookups, slots, slot = _counted(server, monkeypatch), [], server.bus.slot
    monkeypatch.setattr(server.bus, "slot", lambda *args: slots.append(args) or slot(*args))
    entries = len(server.audit.query(action="status.read"))
    assert [client.get(f"{ADMIN}/status", headers=g).status_code for g in _guesses(3, 1)] == [429] * 3
    assert [client.get(f"{ADMIN}/status").status_code for _ in range(3)] == [401] * 3
    assert lookups == [] and slots == []  # nothing asked of PostgreSQL or Valkey
    assert len(server.audit.query(action="status.read")) == entries  # said by the window's entries already


@pytest.mark.parametrize(
    ("client", "bucket"),
    [("2001:db8:0:1::7", "2001:db8:0:1::/64"), ("2001:db8:0:1:ffff:1:2:3", "2001:db8:0:1::/64"),
     ("::ffff:198.51.100.7", "198.51.100.7"), ("198.51.100.7", "198.51.100.7"), ("fe80::1%eth0", "fe80::/64"),
     ("testclient", "testclient"), ("unknown", "unknown")],
)  # fmt: skip
def test_an_address_is_counted_by_its_bucket(client, bucket):
    assert bucket_of(client) == bucket


def test_the_addresses_of_one_ipv6_64_share_one_limit_and_those_of_one_48_another(server, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    lookups = _counted(server, monkeypatch)

    def tried(where, n, tag):
        app = TestClient(create_server_app(server), base_url=URL, client=(where, 50000))
        return [app.get(f"{ADMIN}/status", headers=g).status_code for g in _guesses(n, tag)]

    for i in range(5):
        tried(f"2001:db8:0:1::{i + 1:x}", 2, i)
    assert len(lookups) == 3  # one bucket: the /64
    assert tried("2001:db8:0:2::1", 1, 8) == [429]  # another /64 of the same /48: the /48 counts as one host (R3)
    assert tried("2001:db8:1:2::1", 4, 9) == [401, 401, 401, 429]  # another /48 is another bucket
    assert tried("::ffff:198.51.100.7", 3, 10) == [401, 401, 401]
    assert tried("198.51.100.7", 1, 11) == [429]  # the same IPv4 address, however it is written


@pytest.mark.parametrize("held", ["lookup", "entry"])
async def test_a_flood_of_unknown_tokens_waits_on_the_administrations_threads_alone(server, monkeypatch, held):
    entered, release = [], threading.Event()

    def holding(real, when=lambda *_args: True):
        def wait(*args, **kwargs):
            if when(*args):
                entered.append(args)
                release.wait(15)
            return real(*args, **kwargs)

        return wait

    if held == "lookup":
        monkeypatch.setattr(server.admin, "checked", holding(server.admin.checked))
    else:  # the entries of the refusals, each the first of its address
        monkeypatch.setattr(
            server.audit, "record", holding(server.audit.record, lambda actor, *_rest: actor == "unknown")
        )
    clients = [_from(server, f"198.51.100.{n + 1}") for n in range(64)]  # as many addresses: no limit stops them
    calls = [asyncio.ensure_future(c.get(f"{ADMIN}/status", headers=_guesses(1, n)[0])) for n, c in enumerate(clients)]
    try:
        for _ in range(500):
            if len(entered) >= CREDENTIAL_THREADS:
                break
            await asyncio.sleep(0.01)
        threads = CREDENTIAL_THREADS if held == "lookup" else ADMIN_THREADS  # the lookups' pool, or the entries'
        for _ in range(100):  # time for any more to be held, were they held anywhere else
            if len(entered) > threads:
                break
            await asyncio.sleep(0.01)
        assert len(entered) == threads  # every thread of their pool held, and no other
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 2) == "free"  # the default ones are not
        if held == "entry":  # nor the lookups', which only what answers in milliseconds holds
            assert await asyncio.wait_for(off_loop(lambda: "free"), 2) == "free"
    finally:
        release.set()
        answers = await asyncio.gather(*calls)
        for c in clients:
            await c.aclose()
    assert {a.status_code for a in answers} == {401}


async def test_another_administrators_call_is_not_held_by_a_burst(server, admin, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    assert CREDENTIAL_THREADS > 3  # an address's lookups never hold every thread of the credentials
    release = threading.Event()
    checked = server.admin.checked

    def held(secret):
        if secret != admin["x-latet-admin"]:  # the burst's lookups wait; the administrator's does not
            release.wait(15)
        return checked(secret)

    monkeypatch.setattr(server.admin, "checked", held)
    async with _from(server, "198.51.100.7") as burst, _from(server, "203.0.113.9") as mine:
        calls = [asyncio.ensure_future(burst.get(f"{ADMIN}/status", headers=g)) for g in _guesses(200)]
        try:
            answer = await asyncio.wait_for(mine.get(f"{ADMIN}/status", headers=admin), 10)
            assert answer.status_code == 200  # while the burst's lookups are still held
        finally:
            release.set()
            statuses = [a.status_code for a in await asyncio.gather(*calls)]
    assert statuses.count(401) == 3 and statuses.count(429) == 197


def test_a_credential_found_valid_is_kept_by_a_keyed_hash_for_an_hour_at_most(monkeypatch):
    clock = [0.0]
    known = Known(clock=lambda: clock[0])
    secret = "latet_adm_" + "x" * 43
    assert not known.holds(secret)
    known.keep(secret)
    assert known.holds(secret) and secret not in repr(vars(known))  # a keyed hash, never the credential
    clock[0] += KNOWN_SECONDS - 1
    assert known.holds(secret)
    clock[0] += 1
    assert not known.holds(secret)  # an hour at most
    known.keep(secret)
    known.forget(secret)
    assert not known.holds(secret)
    monkeypatch.setattr("latet.server.machines.KNOWN_SIZE", 2)
    for n in range(3):
        known.keep(f"latet_adm_{n}")
    assert not known.holds("latet_adm_0") and known.holds("latet_adm_2")  # bounded: the oldest goes first
    assert Known()._hash(secret) != known._hash(secret)  # each memory's key its own


def test_a_credential_found_valid_on_one_instance_skips_the_limit_on_another(server, monkeypatch, caplog):
    key = server.master.derive(b"known")
    here, there = Known(server.bus, key, kind="admin"), Known(server.bus, key, kind="admin")  # two instances
    secret = "latet_adm_" + "x" * 43
    assert not there.holds(secret)
    here.keep(secret)
    assert there.holds(secret)  # found valid here, known there from its first try
    assert not Known(server.bus, key, kind="secret").holds(secret)  # each kind of credential apart
    [name] = [n.decode() for n in server.bus.client.scan_iter(match=server.bus.key("known:*"))]
    assert "xxxx" not in name and server.bus.client.get(name) == b"1"  # a keyed hash, never the credential
    assert KNOWN_SECONDS * 1000 - 10_000 < server.bus.client.pttl(name) <= KNOWN_SECONDS * 1000  # an hour's life
    there.forget(secret)
    assert not Known(server.bus, key, kind="admin").holds(secret)  # forgotten for every instance

    def down(*args, **kwargs):
        raise redis.ConnectionError("Error 10061 connecting to 10.0.3.8:6379")

    for name in ("get", "set", "delete"):
        monkeypatch.setattr(server.bus, name, down)
    monkeypatch.setattr("latet.server.machines.KNOWN_UNANSWERED", Paced())  # whatever an earlier test made happen
    with caplog.at_level(logging.WARNING, logger="latet.server.machines"):
        here.keep(secret)  # Valkey does not answer: this instance's memory answers alone
        assert here.holds(secret) and not Known(server.bus, key, kind="admin").holds(secret)
        here.forget(secret)
        assert not here.holds(secret)
    said = [r.getMessage() for r in caplog.records if r.name == "latet.server.machines"]
    assert len(said) == 1 and "ConnectionError" in said[0] and "10.0.3.8" not in caplog.text  # once a minute, all told


def test_an_administrator_known_here_is_never_locked_out_by_strangers_at_their_address(server, admin, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    lookups = _counted(server, monkeypatch)
    client = TestClient(create_server_app(server), base_url=URL)
    assert client.get(f"{ADMIN}/status", headers=admin).status_code == 200  # found valid here
    strangers = [client.get(f"{ADMIN}/status", headers=g) for g in _guesses(4)]  # behind the same address
    assert [a.status_code for a in strangers] == [401, 401, 401, 429] and strangers[-1].headers["retry-after"] == "60"
    looked = len(lookups)
    assert client.get(f"{ADMIN}/status", headers=admin).status_code == 200  # past the full limit
    assert len(lookups) == looked + 1  # looked up all the same
    token_id = server.admin.check(admin["x-latet-admin"])
    assert server.admin.revoke(token_id)
    misses = server.bus.rate_key(machines.miss_key("testclient"))
    counted = int(server.bus.get(misses))
    assert client.get(f"{ADMIN}/status", headers=admin).status_code == 401  # revoked: refused...
    assert int(server.bus.get(misses)) == counted + 1  # ...and counted
    assert client.get(f"{ADMIN}/status", headers=admin).status_code == 429  # and limited as any other from now on


async def test_a_save_waiting_on_its_identitys_lock_holds_no_other_call(server, admin):
    server.listening.set()  # spaces are built here
    try:
        key, _ = server.identities.add_key("Billing CI")
        runtime = await server.spaces.runtime(key)
        held, release = threading.Event(), threading.Event()

        def hold():  # the key's runtime lock, held a while by a change of its own
            with runtime._lock:
                held.set()
                release.wait(20)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        path, settings = f"{ADMIN}/identities/{key.id}/settings", runtime.config.model_dump(mode="json")
        async with _from(server, "203.0.113.9") as mine, _from(server, "198.51.100.7") as stranger:
            saves = [asyncio.ensure_future(mine.put(path, json=settings, headers=admin)) for _ in range(40)]
            try:
                await asyncio.sleep(0.5)  # the saves at the lock, as far as each gets
                started = time.monotonic()
                status = await asyncio.wait_for(mine.get(f"{ADMIN}/status", headers=admin), 5)
                guess = await asyncio.wait_for(stranger.get(f"{ADMIN}/status", headers=_guesses(1)[0]), 5)
                assert (status.status_code, guess.status_code) == (200, 401)
                assert time.monotonic() - started < 3  # neither waited for the lock, nor for the saves' threads
                await _lookups_free()  # the save that waits holds a thread of the administration's alone
            finally:
                release.set()
                answers = await asyncio.gather(*saves)
        holder.join(5)
        assert {a.status_code for a in answers} == {200}  # each in its turn
    finally:
        await server.spaces.close()


async def test_an_end_of_sessions_that_waits_holds_no_lookup_thread(server, admin, monkeypatch):
    person, _ = server.identities.add_person("Emily Carter")
    entered, release = threading.Event(), threading.Event()
    ended = server._ended

    def slow(identity_id):
        entered.set()
        release.wait(15)
        return ended(identity_id)

    monkeypatch.setattr(server, "_ended", slow)
    async with _from(server, "203.0.113.9") as mine:
        revoking = asyncio.ensure_future(mine.delete(f"{ADMIN}/identities/{person.id}/tokens", headers=admin))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            await _lookups_free()
        finally:
            release.set()
            assert (await revoking).status_code == 200


def test_a_page_never_locks_an_administrator_out(server, admin, monkeypatch):
    monkeypatch.setattr("latet.server.admin.LOOKUP_LIMIT", 3)
    lookups = _counted(server, monkeypatch)
    page = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}
    client = TestClient(create_server_app(server), base_url=URL)
    in_clear = TestClient(create_server_app(server), base_url="http://latet.acme.lan")  # the same address
    refused = [client.get(f"{ADMIN}/status", headers=page).status_code for _ in range(10)]
    refused += [client.get(f"{ADMIN}/status").status_code for _ in range(10)]
    refused += [client.get(f"{ADMIN}/status", headers={"x-latet-admin": "Bearer x"}).status_code for _ in range(10)]
    refused += [in_clear.get(f"{ADMIN}/status", headers=admin).status_code for _ in range(5)]
    assert refused == [403] * 10 + [401] * 20 + [403] * 5 and lookups == []
    assert client.get(f"{ADMIN}/status", headers=admin).status_code == 200  # never refused for them
    entries = [e for e in server.audit.query(action="status.read") if e.actor == "unknown"]
    assert len(entries) == 3 + 1  # the first in the window, and one entry for the window
    assert [e.details.get("limited") for e in reversed(entries)] == [None, None, None, True]


async def test_a_distributed_flood_of_refused_calls_is_recorded_up_to_the_servers_ceiling(server, admin, monkeypatch):
    """Each address has its own limit, and refusals from many of them would each take the chain's lock for its entry:
    past the server's ceiling of such entries in a window, a refused call is refused as before, its entry left out,
    and one entry says how many were, at once for the first and then once a minute (the beat says the rest), so that
    an administrator's call is never held behind the flood's entries (S-05)."""
    monkeypatch.setattr("latet.server.admin.REFUSED_ENTRIES", 5)
    addresses = [f"2001:db8:{n:x}::1" for n in range(30)]  # thirty /64s: no address's own limit stops them
    statuses = []
    for n, where in enumerate(addresses):
        async with _from(server, where) as stranger:
            headers = _guesses(1, n)[0] if n % 2 else {}  # a token nobody issued, or none at all
            statuses.append((await stranger.get(f"{ADMIN}/status", headers=headers)).status_code)
    assert statuses == [401] * 30  # refused as before, whatever is recorded
    async with _from(server, "203.0.113.9") as mine:
        assert (await mine.get(f"{ADMIN}/status", headers=admin)).status_code == 200
    entries = await asyncio.to_thread(server.audit.query, action="status.read")
    assert len([e for e in entries if e.actor == "unknown"]) == 5  # the ceiling's
    assert [e.actor for e in entries if e.actor != "unknown"] == [_actor(server, admin)]  # an administrator's, always
    [first] = await asyncio.to_thread(server.audit.query, action="calls.refused")
    assert (first.actor, first.details) == ("unknown", {"count": 1, "reason": "ceiling"})  # the first past it, at once
    await asyncio.to_thread(server.refusals.flush, server.audit)  # what the beat does once a minute
    counted = await asyncio.to_thread(server.audit.query, action="calls.refused")
    assert sum(e.details["count"] for e in counted) == 25 and {e.details["reason"] for e in counted} == {"ceiling"}
    told = repr([(e.target, e.address, e.details) for e in counted])
    assert all(where not in told for where in addresses)  # a count, no address


async def test_the_servers_ceiling_of_refused_calls_recorded_is_sixty_a_minute(server):
    """S8 (area 3, M3): five addresses past their own limits, or as many /64s, wrote six hundred entries a minute to
    the chain, kept a year: sixty a minute now, ten administrators' worth, the rest counted into one entry."""
    addresses = [f"2001:db8:{n:x}::1" for n in range(70)]  # seventy /64s: no address's own limit stops them
    for where in addresses:
        async with _from(server, where) as stranger:
            assert (await stranger.get(f"{ADMIN}/status")).status_code == 401  # with no token: refused as before
    entries = await asyncio.to_thread(server.audit.query, action="status.read")
    assert len([e for e in entries if e.actor == "unknown"]) == 60
    await asyncio.to_thread(server.refusals.flush, server.audit)
    counted = await asyncio.to_thread(server.audit.query, action="calls.refused")
    assert sum(e.details["count"] for e in counted) == 10 and {e.details["reason"] for e in counted} == {"ceiling"}


def _scoped(server):
    """The app behind a wrapper that keeps every request's scope (what the request log reads of it), and the scopes."""
    app, scopes = create_server_app(server), []

    async def outer(scope, receive, send):
        scopes.append(scope)
        await app(scope, receive, send)

    return outer, scopes


async def test_junk_calls_while_the_credentials_threads_are_busy_ask_nothing_of_them(server, monkeypatch):
    """A1 (the F-R3 check's Important 1): while the credentials' threads are past their mark (``CREDENTIALS_BUSY``), an
    administration call with a token nobody issued is refused at once (503, ``Retry-After``) before its address takes a
    slot, and nothing is recorded of it in those threads either: the server's ceiling of entries is not asked, which
    cost a round trip each, so that one address could keep the mark, and push toward the bound, while its limit never
    applied. The refusals are counted into the entry of those not written one by one, and each is marked the server's
    own refusal, which the request log paces (``REFUSED``), not a line each."""
    monkeypatch.setattr(machines._WAITING, "count", machines.CREDENTIALS_BUSY)  # the threads past their mark
    offloaded, slots, real, slot = [], [], admin_module.off_loop, server.bus.slot

    async def spied(work, *args):
        offloaded.append(work)
        return await real(work, *args)

    monkeypatch.setattr(admin_module, "off_loop", spied)
    monkeypatch.setattr(server.bus, "slot", lambda *args: slots.append(args) or slot(*args))  # what ``take`` asks too
    lookups = _counted(server, monkeypatch)
    app, scopes = _scoped(server)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 40000))
    async with httpx.AsyncClient(transport=transport, base_url=URL) as junk:
        answers = await asyncio.gather(*(junk.get(f"{ADMIN}/status", headers=g) for g in _guesses(20)))
    assert [a.status_code for a in answers] == [503] * 20
    assert {a.headers["retry-after"] for a in answers} == {machines.QUEUED_RETRY_AFTER}
    assert offloaded == [] and slots == [] and lookups == []  # no round trip in the credentials' threads
    assert len(scopes) == 20 and all(scope.get(REFUSED) for scope in scopes)
    entries = await asyncio.to_thread(server.audit.query, action="status.read")
    assert [e for e in entries if e.actor == "unknown"] == []  # none written one by one


async def test_a_call_shed_while_the_threads_were_busy_asks_nothing_of_them_when_the_queue_fell_meanwhile(
    server, monkeypatch
):
    """B1 (the check's probe of A1): a junk administration call comes while the credentials' threads are past their
    mark, waits for the read of what was found valid elsewhere, and is shed; the queue fell under the mark during that
    read. It is still recorded through none of those threads (its address was charged nothing: no refusal costs them a
    round trip uncharged), and counted into the entry of the calls refused while they were busy, whatever the queue is
    by the time it is recorded."""
    offloaded, slots, real, slot = [], [], admin_module.off_loop, server.bus.slot
    held_elsewhere = machines.Known.held_elsewhere

    def falling(self, hashes):  # the queue falls under the mark while the read is made
        machines._WAITING.count = 0
        return held_elsewhere(self, hashes)

    async def spied(work, *args):
        offloaded.append(work)
        return await real(work, *args)

    monkeypatch.setattr(machines._WAITING, "count", 0)  # put back as it was after the test
    monkeypatch.setattr(machines.Known, "held_elsewhere", falling)
    monkeypatch.setattr(admin_module, "off_loop", spied)
    monkeypatch.setattr(server.bus, "slot", lambda *args: slots.append(args) or slot(*args))  # what ``take`` asks too
    lookups = _counted(server, monkeypatch)
    answers = []
    app, scopes = _scoped(server)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 40000))
    async with httpx.AsyncClient(transport=transport, base_url=URL, trust_env=False) as junk:
        for guess in _guesses(4):
            machines._WAITING.count = machines.CREDENTIALS_BUSY  # past the mark when the call comes
            answers.append(await junk.get(f"{ADMIN}/status", headers=guess))
    assert [a.status_code for a in answers] == [503] * 4 and machines._WAITING.count == 0
    assert {a.headers["retry-after"] for a in answers} == {machines.QUEUED_RETRY_AFTER}
    assert len(scopes) == 4 and all(scope.get(REFUSED) for scope in scopes)
    assert offloaded == [] and slots == [] and lookups == []  # no round trip in the credentials' threads
    await asyncio.to_thread(server.refusals.flush, server.audit)  # what the beat does once a minute
    counted = await asyncio.to_thread(server.audit.query, action="calls.refused")
    assert sum(e.details["count"] for e in counted) == 4 and {e.details["reason"] for e in counted} == {"busy"}


def test_a_bus_takes_a_slot_at_once_and_gives_it_back(server):
    bus = server.bus
    seven, eight, nine = (bus.rate_key(f"admin-miss:10.0.3.{n}") for n in (7, 8, 9))
    assert [bus.take("admin-miss:10.0.3.7", 3, 60) for _ in range(5)] == [True, True, True, False, False]
    assert 0 < bus.client.pttl(bus.key(seven)) <= 60_000  # its window, from the first
    bus.give_back("admin-miss:10.0.3.7")
    assert bus.get(seven) == b"4"
    for _ in range(6):
        bus.give_back("admin-miss:10.0.3.7")
    assert bus.get(seven) == b"0"  # never below nothing
    bus.give_back("admin-miss:10.0.3.8")
    assert bus.get(eight) is None  # nothing taken: nothing made
    bus.client.set(bus.key(nine), b"1")  # a count left with no lifetime gets one
    assert bus.take("admin-miss:10.0.3.9", 3, 60) and bus.client.pttl(bus.key(nine)) > 0


def test_a_call_whose_limit_cannot_be_asked_is_refused_and_said_once_a_minute(server, admin, monkeypatch, caplog):
    def silent(*_args):
        raise redis.ConnectionError("Error connecting to valkey://:s3cret-pw@10.0.0.9")

    offloaded, real = [], admin_module.off_loop

    async def spied(work, *args):
        offloaded.append(getattr(work, "__name__", ""))
        return await real(work, *args)

    app, scopes = _scoped(server)
    bare = TestClient(app, base_url=URL)  # no lifespan, so no ear
    monkeypatch.setattr(server.bus, "slot", silent)  # what ``take`` asks too
    monkeypatch.setattr(admin_module, "off_loop", spied)
    with caplog.at_level(logging.DEBUG):
        assert [bare.get(f"{ADMIN}/status", headers=admin).status_code for _ in range(3)] == [503, 503, 503]
    assert len(scopes) == 3 and all(scope.get(REFUSED) for scope in scopes)  # the server's own: paced in the log
    assert "take" not in offloaded  # its address charged nothing: the ceiling is not asked of the threads either (B1)
    [entry] = server.audit.query(action="status.read")
    assert (entry.details["status"], entry.details["reason"], entry.details["count"]) == (503, "unavailable", 1)
    assert "s3cret-pw" not in caplog.text and "ConnectionError" in caplog.text
    # The outage's last calls, said by no entry nor line yet: the beat says them once a minute, Valkey back or not.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        server.refusals.flush(server.audit)
    [flushed] = server.audit.query(action="calls.refused")
    assert (flushed.actor, flushed.details) == ("unknown", {"count": 2, "reason": "unavailable"})
    assert "2 more" in caplog.text and "s3cret-pw" not in caplog.text
    server.refusals.flush(server.audit)
    assert len(server.audit.query(action="calls.refused")) == 1  # nothing more to say: nothing written


# --- no call holds the event loop ---------------------------------------------------------------------------------


@pytest.mark.parametrize("held", ["token", "store", "record", "resolver"])
def test_a_call_waiting_on_a_store_holds_no_other_call(server, client, admin, monkeypatch, held):
    entered, release = threading.Event(), threading.Event()

    def holding(real, when=lambda *_args, **_kwargs: True):
        def wait(*args, **kwargs):
            if when(*args, **kwargs):
                entered.set()
                release.wait(10)
            return real(*args, **kwargs)

        return wait

    slow, path, body = dict(admin), f"{ADMIN}/identities", None
    if held == "token":
        slow = {"x-latet-admin": server.admin.issue("a slow one")}
        checked = server.admin.checked
        monkeypatch.setattr(server.admin, "checked", holding(checked, lambda s: s == slow["x-latet-admin"]))
    elif held == "store":
        monkeypatch.setattr(server.identities, "token_counts", holding(server.identities.token_counts))
    elif held == "record":
        record = server.audit.record
        monkeypatch.setattr(
            server.audit, "record", holding(record, lambda _a, action, *_r, **_k: action == "identities.list")
        )
    else:
        _, path = _key(client, admin)
        body = {
            **client.get(path, headers=admin).json()["settings"],
            "upstreams": [{"name": "mine", "url": "https://llm.slow.example"}],
        }
        monkeypatch.setattr(egress, "resolve", holding(lambda _host, _port: []))
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(client.request, "PUT" if body else "GET", path, json=body, headers=slow)
        assert entered.wait(10)
        started = time.monotonic()
        assert client.get(f"{ADMIN}/status", headers=admin).status_code == 200
        quick = time.monotonic() - started
        if held != "token":  # a store call, an entry, a resolution: never on the lookups' threads
            asyncio.run(_lookups_free())
        release.set()
        assert waiting.result(20).status_code == 200
    assert quick < 3  # the call that waits holds a thread, never the event loop
