"""The settings in use change under one lock: read, changed and applied without another change between.
The destinations a wrapper registered are kept: they stay in use until the daemon restarts. A root key
given is used as it is; without one, the key of this computer is loaded."""

import hashlib
import hmac
import threading
import time

import pytest
from pydantic import ValidationError

from latet import runtime as runtime_module
from latet.api import rest
from latet.config import Config, IdentityConfig, Profile, TermConfig, load_config
from latet.edition import Community
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Entity, Vault, derive_keys
from latet.floor import TermsUnavailable
from latet.runtime import make_runtime
from latet.storage.backup import RESTORE_JOURNAL
from latet.storage.db import Store

from ..conftest import ROOT_KEY
from ..synced import SyncedEdition


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


class _Counting(Community):
    """An edition that counts how often it is closed, and keeps who watches its floor."""

    def __init__(self) -> None:
        self.closed = 0
        self.watchers: list = []

    def watch(self, changed):
        self.watchers.append(changed)
        return lambda: self.watchers.remove(changed)

    def close(self) -> None:
        self.closed += 1


def test_a_runtime_watches_the_editions_floor_and_applies_its_settings_again_when_it_changes(tmp_path, monkeypatch):
    edition = _Counting()
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    applied = []
    monkeypatch.setattr(
        rt, "apply_config", lambda config, save=True, as_is=False: applied.append((config, save, as_is))
    )
    [changed] = edition.watchers
    changed()  # the floor changed: a server's policy heard (E2)
    assert applied == [(rt.config, False, True)]  # as they apply: nothing waiting for approval laid over again (F4)
    rt.close()
    assert edition.watchers == [] and edition.closed == 0  # it stopped watching; the edition is not its own


def test_a_runtime_closes_the_edition_it_made_and_leaves_one_it_was_given(tmp_path, monkeypatch):
    made: list[tuple[object, _Counting]] = []

    def load(context):
        made.append((context, _Counting()))
        return made[-1][1]

    monkeypatch.setattr(runtime_module, "load_edition", load)
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "a", config_file=tmp_path / "a" / "config.yaml")
    context, own = made[0]
    assert rt.edition is own and rt.owns_edition
    assert (context.data, context.config_dir, context.server) == (tmp_path / "a", tmp_path / "a", None)
    rt.close()
    rt.close()  # closing twice is no harm: the edition it made is closed once
    given = _Counting()
    rt = make_runtime(config, data=tmp_path / "b", config_file=tmp_path / "b.yaml", edition=given)
    assert rt.edition is given and not rt.owns_edition and len(made) == 1
    rt.close()
    rt.close()
    assert own.closed == 1 and given.closed == 0


@pytest.mark.parametrize("given", ["identity", "backends"])
def test_a_servers_space_is_never_made_without_the_servers_edition(tmp_path, monkeypatch, given):
    """A space on a Latet server (an identity's, or over a server's backends) runs the server's edition: made without
    one, it is refused before any edition is loaded, never quietly the Community Edition of a computer (L2)."""
    from latet.state import IdentityRef

    loaded = []
    monkeypatch.setattr(runtime_module, "load_edition", lambda context: loaded.append(context) or Community())
    space = {
        "identity": {"identity": IdentityRef(id="u_7f3a2b9c1d", name="Emily Carter", kind="person")},
        "backends": {"backends": object(), "root_key": ROOT_KEY},
    }[given]
    config = Config(identity=IdentityConfig(enabled=False))
    with pytest.raises(ValueError, match="a server's space runs the server's edition: pass edition="):
        make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", **space)
    assert loaded == []


def _restoring(monkeypatch, data):
    """A restore under way in ``data``: the start stops before a key or a store is made."""
    data.mkdir(parents=True, exist_ok=True)
    (data / RESTORE_JOURNAL).write_text("{}", "utf-8")


def _no_dashboard(monkeypatch, data):
    """The start fails at its last step before the watch: its dashboard cannot be made."""

    def refused(*args, **kwargs):
        raise RuntimeError("the dashboard could not be made")

    monkeypatch.setattr(rest, "Dashboard", refused)


@pytest.mark.parametrize("failing", [_restoring, _no_dashboard], ids=["restore-under-way", "last-step"])
def test_a_start_that_fails_closes_the_edition_it_made_once_and_watches_nothing(tmp_path, monkeypatch, failing):
    made: list[_Counting] = []

    def load(context):
        made.append(_Counting())
        return made[-1]

    monkeypatch.setattr(runtime_module, "load_edition", load)
    config = Config(identity=IdentityConfig(enabled=False))
    failing(monkeypatch, tmp_path / "a")
    with pytest.raises(RuntimeError):
        make_runtime(config, data=tmp_path / "a", config_file=tmp_path / "a" / "config.yaml")
    [own] = made
    assert own.closed == 1 and own.watchers == []
    given = _Counting()  # the same failure with an edition it was given: not its own to close
    failing(monkeypatch, tmp_path / "b")
    with pytest.raises(RuntimeError):
        make_runtime(config, data=tmp_path / "b", config_file=tmp_path / "b.yaml", edition=given)
    assert given.closed == 0 and given.watchers == [] and len(made) == 1


def test_every_masker_of_a_runtime_takes_the_editions_live_terms_after_a_change_too(tmp_path):
    edition = SyncedEdition()
    edition.live.use([("Halden Insurance", "CLIENT")], "1.1")
    config = Config(identity=IdentityConfig(enabled=False), ner="off")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    try:
        assert rt.extra == (edition.live,) and edition.live in rt.built.masker.detectors
        rt.apply_config(rt.config.model_copy(update={"profile": Profile.MAX}))
        assert edition.live in rt.built.masker.detectors
        assert edition.live in rt.gateway.dashboard._playground_masker().detectors
        # What a conversation was given is kept under the terms' tag too (R7, S5): a sync masks it again.
        mediator, session = rt.gateway.mediator, rt.built.engine.session("s")
        given = mediator._given_key(session, SlotKind.USER_TEXT, "Kestrel Analytics called", None, None, "")
        edition.live.use([("Halden Insurance", "CLIENT"), ("Kestrel Analytics", "CLIENT")], "2.1")
        assert mediator._given_key(session, SlotKind.USER_TEXT, "Kestrel Analytics called", None, None, "") != given
    finally:
        rt.close()


def test_a_runtime_is_never_made_while_the_editions_terms_cannot_be_said(tmp_path):
    edition = SyncedEdition()
    edition.unavailable = TermsUnavailable("the synced terms cannot be read")
    config = Config(identity=IdentityConfig(enabled=False), ner="off")
    with pytest.raises(TermsUnavailable):
        make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)


def test_the_community_edition_adds_no_detector(rt):
    assert rt.extra == ()


def test_the_conversations_keys_are_0_11_s_own_while_no_term_is_synced(tmp_path):
    """A-R1: with no synced term, `_given_key` is the HMAC of 0.11's own message (`rules` = `{rules_tag}|{scope tag}`,
    no third part), so the texts each conversation was given, kept in Valkey on a server, keep their keys at the
    upgrade."""
    config = Config(identity=IdentityConfig(enabled=False), ner="off")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=SyncedEdition())
    try:
        engine, session = rt.built.engine, rt.built.engine.session("s")
        text, kind = "Halden Insurance called", SlotKind.USER_TEXT
        message = "\x00".join((session.key, f"{engine.rules_tag}|", kind.value, "", "", "", text))
        expected = hmac.new(engine.vault.cache_key, message.encode(), hashlib.sha256).digest()
        assert rt.gateway.mediator._given_key(session, kind, text, None, None, "") == expected
    finally:
        rt.close()
