"""A space on a Latet server built on the company's floor (Latet Enterprise): every masker it builds holds the terms
the company locks, though its settings are read again as a store keeps them (which no lock survives); a new floor
applies what waits for approval nothing of; and a refusal of the floor never breaks what applies settings heard."""

import logging
from dataclasses import replace

import pytest

from latet.config import Config, Profile, ProjectEntry, TermConfig
from latet.floor import BelowFloor
from latet.gateway.app import OnServer
from latet.projects import _MASKER_PER_TERM
from latet.runtime import Runtime, digest, make_runtime, rules_digest
from latet.server.spaces import _fixed
from latet.state import local_backends

from ...conftest import ROOT_KEY
from ...floors import FloorEdition, TermsFloor
from .conftest import EMILY, SHARED, URL, MemorySettings
from .test_space_on_server import W, _client, _reapplied, _shared_now

FALCON = TermConfig(value="Northwind Traders", type="CLIENT")
LOOSER = SHARED.model_copy(update={"allow_values": [*SHARED.allow_values, W]})  # an administrator lets a value through


@pytest.fixture
def floored(tmp_path):
    """Emily's space as the ``space`` fixture makes it, over an edition whose floor the test sets (none at first)."""
    settings = MemorySettings(SHARED)
    edition = FloorEdition()
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(
        None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY, edition=edition
    )
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    yield rt, settings, edition
    rt.close()


def _locked(masker):
    return any(d.id == "locked-terms" for d in masker.detectors)


def _floor_comes(rt, edition, floor):
    edition.locked = floor
    edition.watchers[0]()


def test_every_masker_of_a_space_holds_the_terms_the_company_locks(floored):
    rt, settings, edition = floored
    _floor_comes(rt, edition, TermsFloor(FALCON))
    assert _locked(rt.built.masker) and rt.config.locked_terms == []
    _reapplied(rt)  # its settings read again, as the store keeps them
    assert _locked(rt.built.masker)
    rt.update_config(lambda config: config.model_copy(update={"terms": [*config.terms, TermConfig(value="Orion")]}))
    assert _locked(rt.built.masker)  # a save, laid over the shared settings again
    rt.update_config(
        lambda config: config.model_copy(
            update={"projects": [ProjectEntry(path="/home/emily/shop", terms=[TermConfig(value="Vega")])]}
        )
    )
    assert rt.projects is not None
    project = rt.projects.resolve("/home/emily/shop")
    assert project.scope is not None and _locked(project.scope.masker)
    assert "Northwind" not in rt.built.masker.mask("ask Northwind Traders").text
    assert FALCON.value not in str(settings.own) and FALCON.value not in str(settings.approved)  # never hers


def test_a_space_built_under_a_floor_holds_its_terms(tmp_path):
    settings = MemorySettings(SHARED)
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    edition = FloorEdition(TermsFloor(FALCON))
    rt = make_runtime(None, data=tmp_path / "m", root_key=ROOT_KEY, backends=backends, identity=EMILY, edition=edition)
    try:
        assert _locked(rt.built.masker) and rt.in_use().locked_terms == [FALCON]
        assert rt.built.engine.rules_tag == rules_digest(rt.in_use()) != digest(rt.in_use())  # the lock names them
    finally:
        rt.close()


def test_a_new_floor_applies_nothing_that_waits_for_approval(floored):
    """F4: the settings in use, a composition while a change waits for her, are applied again as they are: laid over the
    shared settings again, what waits would apply."""
    rt, settings, edition = floored
    _shared_now(rt, settings, LOOSER)
    [waiting] = rt.mediator.approvals.waiting_changes()
    composed = rt.config
    _floor_comes(rt, edition, TermsFloor(FALCON))
    assert W not in rt.config.allow_values and W not in rt.in_use().allow_values and rt.config == composed
    assert [c.id for c in rt.mediator.approvals.waiting_changes()] == [waiting.id]


def test_a_space_is_weighed_with_the_terms_the_company_locks(floored):
    rt, _, edition = floored
    before = _fixed(rt)
    _floor_comes(rt, edition, TermsFloor(FALCON))
    assert _fixed(rt) - before >= 2 * _MASKER_PER_TERM  # in the settings in use, and in their detector of their own


class SurrogateUserName:
    """The company has auto mode's reviewer sent a fake of the user name."""

    locks_terms = carves_terms = decides_exceptions = False

    def below(self, config):
        return []

    def raise_to(self, config):
        review = config.auto_mode_review.model_copy(update={"user_name": "surrogate"})
        return config.model_copy(update={"auto_mode_review": review})


def test_a_computers_user_name_is_answered_as_the_settings_in_use_want_it(floored):
    rt, _, edition = floored
    assert rt.config.auto_mode_review.user_name != "surrogate"
    _floor_comes(rt, edition, SurrogateUserName())
    with _client(rt) as c:
        fake = c.post("/_latet/admin/user-name", json={"name": "emily"}).json()["surrogate"]
    assert fake is not None and rt.built.vault.by_original("emily").surrogate == fake


def _refusing(monkeypatch):
    def refused(self, *args, **kwargs):
        raise BelowFloor(["profile: from max to secrets"])

    monkeypatch.setattr(Runtime, "propose", refused)


def test_settings_heard_whose_change_is_refused_apply_what_was_approved(floored, monkeypatch, caplog):
    rt, settings, _ = floored
    _refusing(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="latet.server.spaces"):
        _shared_now(rt, settings, LOOSER)  # raised nothing: the space keeps running
    assert W not in rt.config.allow_values and rt.mediator.approvals.changes == {}
    assert "BelowFloor" in caplog.text


def test_a_change_waiting_that_goes_below_the_floor_when_asked_again_is_rejected(floored, monkeypatch, caplog):
    rt, settings, _ = floored
    _shared_now(rt, settings, LOOSER)
    [waiting] = rt.mediator.approvals.waiting_changes()
    _refusing(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="latet.runtime"):
        rt.update_config(lambda config: config.model_copy(update={"profile": Profile.MAX}))  # her save, kept
    assert rt.config.profile == "max" and rt.mediator.approvals.settled[waiting.id] == "rejected"
    assert rt.mediator.approvals.waiting_changes() == [] and W not in rt.config.allow_values
    assert "BelowFloor" in caplog.text and W not in caplog.text


def test_settings_found_at_a_start_whose_change_is_refused_run_what_was_approved(tmp_path, monkeypatch):
    settings = MemorySettings(LOOSER)
    settings.approved = Config.model_validate(SHARED.model_dump())  # she approved the shared settings of before
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    _refusing(monkeypatch)
    rt = make_runtime(
        None, data=tmp_path / "m", root_key=ROOT_KEY, backends=backends, identity=EMILY, edition=FloorEdition()
    )
    try:
        assert W not in rt.config.allow_values and rt.mediator.approvals.changes == {}
    finally:
        rt.close()


def test_a_floor_change_indexes_the_settings_at_the_count_it_read_them_at(monkeypatch):
    """Fix round 2 of the release minors (the review's L3): a space that applies a new floor writes its exceptions'
    index from the settings in use and the settings count read with them, both under the runtime's lock, so that a
    save of this space in between (which moves the count under the same lock) never has an older index written at its
    count."""
    import threading
    from types import SimpleNamespace

    from latet.server import spaces as spaces_module

    lock = threading.RLock()
    held: list[bool] = []

    class Counts:
        def highest(self):
            held.append(lock._is_owned())  # type: ignore[attr-defined]
            return 7

    runtime = SimpleNamespace(_lock=lock, floor_changed=lambda: None, config="the settings", floor_used=None)
    space = SimpleNamespace(runtime=runtime, identity=SimpleNamespace(id="u_emilycarte"), settings_counts=Counts())
    indexed = []
    monkeypatch.setattr(spaces_module, "_index_again", lambda *args, **kwargs: indexed.append((args, kwargs)))
    spaces_module._floor(space, "the server")
    assert held == [True]
    assert indexed == [(("the server", "u_emilycarte", "the settings"), {"read_at": 7, "company": False})]
