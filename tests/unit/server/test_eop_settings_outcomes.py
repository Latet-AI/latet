"""ES.6/8/9 and EOP-36 through today's runtime and dashboard interfaces."""

import copy
import ipaddress
import json
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from latet.api import rest
from latet.config import TERM_LONGEST, Config, Destination, TermConfig
from latet.gateway.app import OnServer, create_app
from latet.gateway.approvals import Change
from latet.runtime import digest, make_runtime
from latet.server import egress, machines
from latet.server.shared import SettingsRefused, TermsTooLong
from latet.state import local_backends
from latet.transfer import read_rules

from ...conftest import ROOT_KEY
from .conftest import EMILY, SHARED, URL, MemorySettings, as_identity

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
CANARY = "es9-private@acme.com"


def _runtime(tmp_path, settings, backends=None):
    backends = backends or replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    return rt


def _client(rt):
    return TestClient(as_identity(create_app(rt.gateway), EMILY.id), base_url=URL)


@pytest.mark.parametrize("base", ["current", "stale", "unreadable"])
def test_initial_build_uses_a_current_base_even_when_the_desired_settings_already_wait(tmp_path, base):
    settings = MemorySettings(SHARED)
    settings.approved = settings.load()
    settings.own = {"allow_values": [*SHARED.allow_values, CANARY]}
    wanted = settings.load()
    applied = settings.composed(wanted, settings.approved)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    previous = Change(
        None if base == "unreadable" else wanted,
        digest(applied) if base == "current" else "obsolete-base",
        "start",
        None,
        ["allows a value"],
        ["allow_values"],
    )
    backends.approvals.changes[previous.id] = previous
    rt = _runtime(tmp_path, settings, backends)
    try:
        [waiting] = list(rt.mediator.approvals.waiting_changes())
        assert waiting.config == wanted and waiting.base == digest(rt.config)
        assert (waiting.id == previous.id) is (base == "current")
        assert CANARY not in rt.config.allow_values
    finally:
        rt.close()


def test_a_save_while_a_shared_destination_move_waits_is_a_no_write_conflict(tmp_path):
    old = Config(upstreams=[Destination(name="gw", url="https://gw.acme.com")])
    moved = Config(upstreams=[Destination(name="gw", url="https://gw-new.acme.com")])
    settings = MemorySettings(moved)
    settings.approved = old
    rt = _runtime(tmp_path, settings)
    try:
        assert rt.config.upstreams == old.upstreams and settings.composition is not None
        before = copy.deepcopy(settings.own), settings.approved, rt.config, list(rt.mediator.approvals.changes)
        with _client(rt) as client:
            etag = client.get(f"{API}/config").json()["etag"]
            answer = client.put(
                f"{API}/config",
                json={"etag": etag, "terms": [{"value": "Mercury Labs"}]},
                headers=UI,
            )
        assert answer.status_code == 409 and answer.json()["reason"] == "settings_change_waits"
        assert "retry-after" not in answer.headers
        assert (settings.own, settings.approved, rt.config, list(rt.mediator.approvals.changes)) == before
        assert rt.config.upstreams == old.upstreams
    finally:
        rt.close()


@pytest.fixture
def held_runtime(tmp_path):
    settings = MemorySettings(SHARED)
    settings.approved = settings.load()
    settings.own = {"allow_values": [*SHARED.allow_values, CANARY]}
    rt = _runtime(tmp_path, settings)
    assert settings.composition is not None and CANARY not in rt.config.allow_values
    yield rt, settings
    rt.close()


def test_an_unmoved_pending_change_does_not_block_an_unrelated_protective_save(held_runtime):
    rt, settings = held_runtime
    with _client(rt) as client:
        etag = client.get(f"{API}/config").json()["etag"]
        terms = [*rt.config.model_dump(mode="json")["terms"], {"value": "Mercury Labs"}]
        answer = client.put(f"{API}/config", json={"etag": etag, "terms": terms}, headers=UI)
    assert answer.status_code == 200
    assert "Mercury Labs" in [t.value for t in rt.config.terms]
    assert CANARY in settings.own["allow_values"] and CANARY not in rt.config.allow_values


@pytest.mark.parametrize("surface", ["config", "import", "destinations"])
@pytest.mark.parametrize("fault", [OSError, RuntimeError])
def test_a_reproposal_fault_after_save_reports_kept_state_without_replaying(
    held_runtime, monkeypatch, caplog, surface, fault
):
    rt, settings = held_runtime
    writes, refreshes = [], []
    keep = settings._keep_own

    def kept(own):
        writes.append(copy.deepcopy(own))
        keep(own)

    def failed_refresh():
        refreshes.append(True)
        raise fault("es9-private-refresh-payload")

    monkeypatch.setattr(settings, "_keep_own", kept)
    monkeypatch.setattr(rt, "_asked_again", failed_refresh)
    monkeypatch.setattr(rt.gateway, "work", machines.in_work)
    monkeypatch.setattr(egress, "resolve", lambda host, port: [ipaddress.ip_address("1.2.3.4")])
    with _client(rt) as client:
        if surface == "config":
            etag = client.get(f"{API}/config").json()["etag"]
            terms = [*rt.config.model_dump(mode="json")["terms"], {"value": "Mercury Labs"}]
            answer = client.put(f"{API}/config", json={"etag": etag, "terms": terms}, headers=UI)
        elif surface == "import":
            answer = client.post(
                f"{API}/rules/import", json={"file": "terms:\n  - {value: Mercury Labs}\n", "apply": True}, headers=UI
            )
        else:
            answer = client.post("/_latet/admin/destinations", json={"destinations": [{"url": "https://new.acme.com"}]})
    assert answer.status_code == 409
    assert answer.json()["reason"] == "settings_saved_change_pending" and answer.json()["saved"] is True
    assert "retry-after" not in answer.headers and len(writes) == 1 and refreshes == [True]
    assert settings.own == writes[0] and CANARY not in rt.config.allow_values
    if surface == "destinations":
        [added] = [d for d in rt.config.upstreams if d.url == "https://new.acme.com"]
        assert added.name in rt.wrapped
        assert any(d["url"] == added.url for d in settings.own["upstreams"])
    else:
        assert "Mercury Labs" in [t.value for t in rt.config.terms]
        assert any(t["value"] == "Mercury Labs" for t in settings.own["terms"])
    assert "es9-private-refresh-payload" not in answer.text + caplog.text


def test_terms_too_long_has_its_own_reason_without_relabelling_other_refusals():
    answer = rest._refused_items(TermsTooLong(["terms.0.value"]))
    assert answer.status_code == 409
    assert json.loads(bytes(answer.body))["reason"] == "settings_terms_too_long"
    other = rest._refused_items(SettingsRefused("a fixed unrelated refusal"))
    assert other.status_code == 409 and "reason" not in json.loads(bytes(other.body))


def test_fresh_overlong_import_terms_are_currently_left_out_before_the_save_boundary():
    """Characterization: coordinator preserved parser discard+notice; this is not a 409 reproduction."""
    private = "es36-private-" + "Z" * TERM_LONGEST
    imported, notices = read_rules(f"terms:\n  - value: {private}\n")
    assert imported.terms == [] and notices
    assert private not in " ".join(notices)


def test_an_import_candidate_with_an_existing_long_own_term_is_refused_without_saving(space, monkeypatch):
    """An explicitly injected legacy candidate proves endpoint propagation, not a natural fresh-import failure."""
    rt, settings = space
    private = "es36-private-" + "Z" * TERM_LONGEST
    # A legacy own term already in memory survives merge_rules; read_rules separately drops newly imported long ones.
    rt.config = rt.config.model_copy(update={"terms": [*rt.config.terms, TermConfig(value=private)]})
    before = copy.deepcopy(settings.own), settings.approved, rt.config
    built, vault = rt.built, list(rt.built.vault.entities())
    writes, prepared = [], []
    prepare = settings.prepare

    def checked(candidate, *, strict=False):
        prepared.append((strict, private in [t.value for t in candidate.terms]))
        return prepare(candidate, strict=strict)

    monkeypatch.setattr(settings, "prepare", checked)
    monkeypatch.setattr(settings, "_keep_own", lambda own: writes.append(own))
    with _client(rt) as client:
        answer = client.post(
            f"{API}/rules/import", json={"file": "terms:\n  - {value: Mercury Labs}\n", "apply": True}, headers=UI
        )
    assert answer.status_code == 409 and answer.json()["reason"] == "settings_terms_too_long"
    assert writes == [] and (settings.own, settings.approved, rt.config) == before
    assert prepared == [(True, True)] and rt.built is built and list(rt.built.vault.entities()) == vault
    assert private not in answer.text
