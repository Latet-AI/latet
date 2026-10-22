"""The settings in use change under one lock: read, changed and applied without another change between.
The destinations a wrapper registered are kept: they stay in use until the daemon restarts. A root key
given is used as it is; without one, the key of this computer is loaded."""

import threading
import time

import pytest
from pydantic import ValidationError

from latet import runtime as runtime_module
from latet.config import Config, IdentityConfig, TermConfig, load_config
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault, derive_keys
from latet.runtime import make_runtime
from latet.storage.db import Store

from ..conftest import ROOT_KEY


@pytest.fixture
def rt(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    runtime = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    yield runtime
    runtime.close()


def test_a_change_is_applied_and_saved(rt):
    new = rt.update_config(lambda current: current.model_copy(update={"allow_values": ["Maria"]}))
    assert new is rt.config and rt.config.allow_values == ["Maria"]
    assert load_config(rt.config_file).allow_values == ["Maria"]
    rt.update_config(lambda current: current.model_copy(update={"allow_values": ["Maria", "Luca"]}), save=False)
    assert rt.config.allow_values == ["Maria", "Luca"]
    assert load_config(rt.config_file).allow_values == ["Maria"]


def test_no_change_leaves_the_settings_as_they_are(rt):
    before, version = rt.config, rt.built.engine.rules_version
    assert rt.update_config(lambda current: None) is None
    assert rt.update_config(lambda current: current) is None  # the same settings: nothing to apply
    assert rt.config is before and rt.built.engine.rules_version == version
    assert not rt.config_file.exists()


def test_a_change_waits_for_the_one_running_and_starts_from_what_it_made(rt):
    inside, release, seen = threading.Event(), threading.Event(), []

    def first(current):
        inside.set()
        assert release.wait(30)
        return current.model_copy(update={"allow_values": ["Maria"]})

    def second(current):
        seen.append(list(current.allow_values))
        return current.model_copy(update={"terms": [TermConfig(value="Acme Corp")]})

    one = threading.Thread(target=rt.update_config, args=(first,), daemon=True)
    one.start()
    assert inside.wait(30)
    two = threading.Thread(target=rt.update_config, args=(second,), daemon=True)
    two.start()
    two.join(timeout=0.3)
    assert two.is_alive() and seen == []  # it waits: the first holds the settings
    release.set()
    one.join(timeout=30)
    two.join(timeout=30)
    assert not one.is_alive() and not two.is_alive()
    assert seen == [["Maria"]]
    assert rt.config.allow_values == ["Maria"] and [t.value for t in rt.config.terms] == ["Acme Corp"]


def test_the_destinations_a_wrapper_registered_are_kept_in_use(rt):
    assert rt.wrapped == set()
    [name] = rt.add_destinations([("https://llm.acme.com", "claude")])
    assert rt.wrapped == {name}
    assert rt.add_destinations([("https://llm.acme.com", "codex")]) == [name]  # known: the same name, kept as well
    with pytest.raises(ValidationError):  # one address in the batch cannot be used: nothing of it is kept
        rt.add_destinations([("https://gw.acme.com", "codex"), ("not an address", "codex")])
    assert rt.wrapped == {name} and [d.name for d in rt.config.upstreams] == [name]


def test_a_root_key_given_is_used_and_none_is_looked_up(tmp_path, monkeypatch):
    def looked_up(data):
        raise AssertionError(f"a root key was looked up in {data}")

    monkeypatch.setattr(runtime_module, "load_root_key", looked_up)
    key = b"\x01" * 32
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=key)
    try:
        assert rt.built.vault.cache_key == derive_keys(key)["cache"]
        assert not (tmp_path / "data" / "root.key").exists()  # nothing saved
    finally:
        rt.close()


def test_without_a_root_key_the_one_of_this_computer_is_loaded(tmp_path, monkeypatch):
    asked = []

    def load_root_key(data):
        asked.append(data)
        return b"\x02" * 32

    monkeypatch.setattr(runtime_module, "load_root_key", load_root_key)
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    try:
        assert asked == [tmp_path / "data"]
        assert rt.built.vault.cache_key == derive_keys(b"\x02" * 32)["cache"]
    finally:
        rt.close()


def test_closing_the_runtime_ends_the_worker_threads_of_its_mediator(tmp_path):
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    before = set(threading.enumerate())
    rt.mediator.executor.submit(int).result()  # a worker thread starts with the first job it is given
    rt.mediator.bridge_executor.submit(int).result()
    workers = [t for t in set(threading.enumerate()) - before if t.name.startswith("latet-")]
    assert sorted(t.name.split("_")[0] for t in workers) == ["latet-bridge", "latet-engine"]
    rt.close()
    for worker in workers:
        worker.join(timeout=2)
    assert [t.name for t in workers if t.is_alive()] == []  # none stays for the process to wait for
    rt.close()  # and closing twice is no harm


def test_an_identity_of_host_names_alone_is_one_written_and_never_detected():
    """A person's identity on a server may hold the host names of their computers and nothing else yet (R-28): it is
    theirs as written, and nothing of the machine is detected in its place."""
    written = Config(identity=IdentityConfig(hostnames=["emily-desktop"]))
    assert runtime_module.resolve_identity(written, detect=False) == written.identity
    assert runtime_module.resolve_identity(Config(), detect=False) is None


def test_a_fake_that_shows_its_value_kept_before_is_drawn_again_when_latet_starts(tmp_path):
    """A computer's vault kept before the vault refused such fakes holds `emily -> emily59`: the daemon's start gives the
    entry the fake its value gets now, keeps it in place of the old one, and masks the value with it."""
    data = tmp_path / "data"
    entity_id = Vault(ROOT_KEY).entity_id("USERNAME", "emily")
    old = Entity(entity_id, "USERNAME", EntityClass.IDENTITY, "emily", "emily59", 0, time.time(), "identity:username")
    store = Store(data / "latet.db", ROOT_KEY)
    store.save_entity(old)
    store.close()
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=data, config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        new = rt.built.vault.by_original("emily")
        assert new is not None and new.id == old.id and "emily" not in new.surrogate.casefold()
        masked = rt.built.engine.mask_text("ssh emily@build-07", SlotKind.TOOL_RESULT, rt.built.engine.session("s"))
        assert masked.text == f"ssh {new.surrogate}@build-07" and "emily59" not in masked.text
    finally:
        rt.close()
    assert [e.surrogate for e in Store(data / "latet.db", ROOT_KEY).load_entities()] == [new.surrogate]
