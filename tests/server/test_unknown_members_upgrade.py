"""A server moves to `unknown_members: check` on upgrade with no approval (the owner's choice of 2026-11-16): the first
start of this version records the new default in the administration log, once for the server, whatever restarts or
instances follow, so that an administrator who relied on the refusal can find when it changed; `latet server status`
says which value is in force; on a server, an approval that moves it to `check` is written to the administration log
(design §3)."""

import pytest
from starlette.testclient import TestClient

from latet import __version__
from latet.server.app import create_server_app

from ..floors import FloorEdition
from .support import URL, close_test_server, open_test_server

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
NOTED = "setting.defaulted"
LOOSENED = "setting.loosened"
LOWERED = "new request members: passed after a check instead of refused"


def _started(server):
    """One start of ``server``'s app: its lifespan run through, as `latet server run` runs it."""
    with TestClient(create_server_app(server), base_url=URL):
        pass


def test_the_first_start_records_the_new_default_once_and_no_restart_again(schema, prefix):
    first = open_test_server(schema, prefix)
    try:
        assert first.audit.query(action=NOTED) == []  # migrated, never started
        _started(first)
        [entry] = first.audit.query(action=NOTED)
        assert (entry.actor, entry.target) == ("server", "unknown_members")
        assert entry.details == {"value": "check", "version": __version__}
        _started(first)  # the same instance, started again
    finally:
        close_test_server(first)
    second = open_test_server(schema, prefix)  # another instance of the server, or the process restarted
    try:
        _started(second)
        assert len(second.audit.query(action=NOTED)) == 1
        assert len(second.audit.query(action="server.started")) == 3
        assert second.audit.verify().ok
    finally:
        close_test_server(second)


class _Refusing:
    """A company's floor that forces `unknown_members: refuse`."""

    locks_terms = carves_terms = decides_exceptions = False

    def below(self, config):
        return [] if config.unknown_members == "refuse" else [LOWERED]

    def raise_to(self, config):
        return config.model_copy(update={"unknown_members": "refuse"})


@pytest.mark.parametrize(("floor", "value"), [(None, "check"), (_Refusing(), "refuse")])
def test_the_status_says_the_value_in_force_for_the_server(server, floor, value):
    if floor is not None:
        server.edition.close()
        server.edition = FloorEdition(floor)
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    with TestClient(create_server_app(server), base_url=URL) as client:
        status = client.get(f"{ADMIN}/status", headers=admin).json()
    assert status["unknown_members"] == value


async def test_her_approval_of_check_on_a_server_is_written_to_the_administration_log(server):
    """Design §3: on a server, an approval that moves `unknown_members` from `refuse` to `check` writes an entry of the
    administration log, the identity its actor (no decider of the company's) and its target; a move to `refuse`
    applies at once and writes none."""
    server.listening.set()
    person, _ = server.identities.add_person("Hedy Lamarr", "hedy@example.com")
    try:
        runtime = await server.spaces.runtime(person)
        strict = runtime.config.model_copy(update={"unknown_members": "refuse"})
        await runtime.mediator.run(runtime.update_config, lambda current: strict)
        assert runtime.config.unknown_members == "refuse" and server.audit.query(action=LOOSENED) == []
        wanted = runtime.config.model_copy(update={"unknown_members": "check"})
        change = await runtime.mediator.run(runtime.propose, wanted, "command")
        assert change is not None and await runtime.mediator.run(runtime.decide_change, change.id, True) == "approved"
        [entry] = server.audit.query(action=LOOSENED)
        assert (entry.actor, entry.target) == (person.id, person.id)
        assert entry.details == {"setting": "unknown_members", "value": "check", "scope": "identity"}
        assert server.audit.verify().ok
    finally:
        await server.spaces.close()


def test_the_administrations_save_of_an_api_keys_check_is_logged_as_settings_changed_alone(server, monkeypatch):
    """The ruling's carve-out: the administration's save of an API key's settings writes `settings.changed` alone (its
    token the actor, the setting among its keys), never a `setting.loosened` credited to the key."""
    from latet.server import egress

    monkeypatch.setattr(egress, "_NAT64", egress._Nat64(()))  # no lookup of ipv4only.arpa from a test
    admin = {"x-latet-admin": server.admin.issue("first administrator")}
    with TestClient(create_server_app(server), base_url=URL) as client:
        key = client.post(f"{ADMIN}/identities", json={"kind": "key", "name": "Billing CI"}, headers=admin).json()
        key_id = key["identity"]["id"]
        path = f"{ADMIN}/identities/{key_id}/settings"
        for value in ("refuse", "check"):
            settings = client.get(path, headers=admin).json()["settings"]
            changed = client.put(path, json={**settings, "unknown_members": value}, headers=admin)
            assert changed.status_code == 200, changed.text
            assert changed.json()["settings"]["unknown_members"] == value
    assert server.audit.query(action=LOOSENED) == []
    entries = [entry for entry in server.audit.query(action="settings.changed") if entry.target == key_id]
    assert len(entries) == 2 and all("unknown_members" in entry.details["keys"] for entry in entries)
    assert all(entry.actor.startswith("admin:") for entry in entries)
