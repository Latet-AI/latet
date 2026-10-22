"""A runtime and the backends it runs over: this computer's when it is given none, the ones it is given otherwise
(with the root key their vault is sealed with), and what a start that cannot go on leaves behind."""

import sqlite3
import subprocess
import sys
from dataclasses import replace

import pytest

from latet import runtime as runtime_module
from latet.config import Config, ConfigError, IdentityConfig
from latet.engine.build import build
from latet.engine.pipeline import SPACE_BLOCK_BYTES, SPACE_CACHE_BYTES
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.aws import AwsCredentialStore
from latet.gateway.sigv4 import AwsCredentials
from latet.runtime import digest, make_runtime
from latet.state import (
    IdentityRef,
    LocalAwsSessions,
    LocalConversations,
    LocalSettings,
    LocalSignIns,
    StateBackends,
    local_backends,
)
from latet.storage.backup import RESTORE_JOURNAL
from latet.storage.db import Store
from latet.storage.exchanges import ExchangeStore

from ...conftest import ROOT_KEY


def test_the_state_package_needs_nothing_of_a_server():
    emily = IdentityRef("u_emilycarte", "Emily Carter", "person")
    assert (emily.id, emily.kind) == ("u_emilycarte", "person")
    code = "import sys, latet.state; assert 'psycopg' not in sys.modules and 'redis' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
    assert [f for f in StateBackends.__dataclass_fields__] == [
        "vault", "history", "conversations", "sent", "given", "approvals", "feed", "approvals_feed", "sign_ins",
        "settings", "jobs", "aws",
    ]  # fmt: skip


def test_a_computers_runtime_gateway_and_dashboard_need_nothing_of_a_server():
    """They know how a space on a server works (its egress, its shared settings) and take neither driver."""
    code = (
        "import sys, latet.runtime, latet.gateway.app, latet.api.rest\n"
        "assert 'psycopg' not in sys.modules and 'redis' not in sys.modules, sorted(sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_a_session_added_through_one_store_signs_through_another_over_the_same_sessions():
    shared = LocalAwsSessions()
    bridge, forwarder = AwsCredentialStore(sessions=shared), AwsCredentialStore(sessions=shared)
    session = bridge.add_session(AwsCredentials("AKIDEXAMPLE", "s", "t"))
    assert forwarder.session(session).access_key == "AKIDEXAMPLE" and forwarder.sessions is shared
    assert forwarder.drop_session(session) and not bridge.has_session(session)


def test_a_runtime_is_built_over_the_backends_it_is_given(tmp_path):
    emily = IdentityRef("u_emilycarte", "Emily Carter", "person")
    backends = local_backends(tmp_path / "data", tmp_path / "config.yaml", ROOT_KEY)
    sign_ins = LocalSignIns()
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml",
        root_key=ROOT_KEY, backends=replace(backends, sign_ins=sign_ins), identity=emily,
    )  # fmt: skip
    try:
        assert rt.identity == emily and rt.store is backends.vault and rt.exchanges is backends.history
        assert rt.built.engine.conversations is backends.conversations and rt.recorder.sent is backends.sent
        assert rt.mediator.approvals.backend is backends.approvals and rt.recorder.broadcaster is backends.feed
        assert rt.mediator.approvals.broadcaster is backends.approvals_feed and rt.recorder.jobs is backends.jobs
        assert rt.gateway.dashboard.sign_ins is sign_ins and rt.gateway.aws.sessions is backends.aws
        assert rt.settings is backends.settings
        assert rt.mediator.given is backends.given and rt.mediator.verifier.given is backends.given
    finally:
        rt.close()


def test_the_rules_in_use_are_named_by_their_digest_whatever_this_process_counts(given):
    """The name a text as it left is kept under (``Mediator._given_key``): the same on every instance of a server that
    applies the same settings, where ``rules_version`` counts what this process applied."""
    rt = make_runtime(**given, root_key=ROOT_KEY)
    try:
        assert rt.built.engine.rules_tag == digest(rt.config)
        new = rt.config.model_copy(update={"allow_values": ["Maria"]})
        version = rt.apply_config(new, save=False)
        assert rt.built.engine.rules_tag == digest(new)
        assert rt.apply_config(new, save=False) == version + 1 and rt.built.engine.rules_tag == digest(new)
    finally:
        rt.close()


def test_a_spaces_cache_of_masked_blocks_is_bounded_in_bytes_and_a_computers_by_count_as_before(given, tmp_path):
    """An identity's space on a Latet server is one of many in its process's memory: what its engine keeps of the
    blocks it masked is weighed and bounded."""
    emily = IdentityRef("u_emilycarte", "Emily Carter", "person")
    space = make_runtime(**given, root_key=ROOT_KEY, identity=emily)
    try:
        assert (space.built.engine._frozen.budget, space.built.engine._frozen.largest) == (
            SPACE_CACHE_BYTES,
            SPACE_BLOCK_BYTES,
        )
    finally:
        space.close()
    computer = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "computer",
                            config_file=tmp_path / "computer.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        assert (computer.built.engine._frozen.budget, computer.built.engine._frozen.largest) == (None, None)
    finally:
        computer.close()


def test_a_bounded_cache_keeps_no_block_over_its_limit_and_lets_the_oldest_go_past_its_budget():
    engine = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY)).engine
    engine.bound(8_000, 3_000)
    session = engine.session("s")

    def mask(text):
        return engine.mask_text(text, SlotKind.USER_TEXT, session)

    large = "the notes: " + "x" * 4_000
    assert not mask(large).cached and not mask(large).cached  # too large to keep: masked again when it comes back
    notes = [f"note {n}: " + "y" * 1_000 for n in range(12)]
    assert not any(mask(note).cached for note in notes)
    assert 0 < engine.cached_bytes <= 8_000  # the oldest went
    assert mask(notes[-1]).cached and not mask(notes[0]).cached


def test_without_backends_a_runtime_is_this_computers_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_PORT", raising=False)  # set (the container's), it would be the port of what is loaded
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        assert rt.identity is None and rt.backends is not None
        assert (tmp_path / "data" / "latet.db").is_file() and (tmp_path / "data" / "exchanges.db").is_file()
        assert rt.settings.load() == Config()  # config.yaml: none yet
    finally:
        rt.close()


def test_a_computers_backends_are_its_own_each_time_and_keep_the_two_feeds_apart(tmp_path):
    one = local_backends(tmp_path / "one", tmp_path / "one.yaml", ROOT_KEY)
    two = local_backends(tmp_path / "two", tmp_path / "two.yaml", ROOT_KEY, record=False)
    try:
        assert isinstance(one.vault, Store) and isinstance(one.history, ExchangeStore) and two.history is None
        assert (tmp_path / "one" / "latet.db").is_file() and (tmp_path / "one" / "exchanges.db").is_file()
        assert (tmp_path / "two" / "latet.db").is_file() and not (tmp_path / "two" / "exchanges.db").exists()
        assert isinstance(one.settings, LocalSettings) and one.settings.config_file == tmp_path / "one.yaml"
        assert one.feed is not one.approvals_feed  # the exchanges' feed never carries what waits for you
        mine = {name: getattr(one, name) for name in StateBackends.__dataclass_fields__}
        assert not [name for name, backend in mine.items() if backend is getattr(two, name)]  # nothing is shared
    finally:
        one.vault.close()
        one.history.close()
        two.vault.close()


def test_a_config_yaml_that_cannot_be_read_stops_the_start_before_a_key_is_made_or_a_database_opened(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("LATET_ROOT_KEY")  # the key of this computer would be made in its data folder
    (tmp_path / "config.yaml").write_text("profile: not-a-profile\n", "utf-8")
    with pytest.raises(ConfigError, match="is not valid settings: profile: "):  # by its place
        make_runtime(data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    assert not (tmp_path / "data").exists()  # no root.key, no latet.db, no exchanges.db, nothing left open


@pytest.fixture
def given(tmp_path):
    """A computer's backends, made by the test and given to ``make_runtime`` (as a server gives its own): the
    arguments of a start over them, but the root key."""
    backends = local_backends(tmp_path / "data", tmp_path / "config.yaml", ROOT_KEY)
    yield {
        "config": Config(identity=IdentityConfig(enabled=False)),
        "data": tmp_path / "data",
        "config_file": tmp_path / "config.yaml",
        "backends": backends,
    }
    backends.vault.close()
    backends.history.close()


def test_a_runtime_over_given_backends_needs_the_root_key_their_vault_is_sealed_with(given, monkeypatch):
    """Without it this computer's key would be read, or made in the data folder: on a server each instance would
    mask with a key of its own, and one value would get two ids and two fakes."""
    monkeypatch.delenv("LATET_ROOT_KEY")  # this computer's key would be made in its data folder
    with pytest.raises(ValueError, match="root key"):
        make_runtime(**given).close()
    assert not (given["data"] / "root.key").exists() and not (given["data"] / "bridge.token").exists()


def test_a_start_over_given_backends_stops_too_while_a_restore_is_under_way(given):
    (given["data"] / RESTORE_JOURNAL).write_text("{}", "utf-8")
    with pytest.raises(RuntimeError, match="a restore is under way"):
        make_runtime(**given, root_key=ROOT_KEY).close()
    assert not (given["data"] / "bridge.token").exists()  # stopped before anything was made


def test_a_runtime_told_not_to_record_records_nothing_in_a_history_it_is_given(given):
    rt = make_runtime(**given, root_key=ROOT_KEY, record=False)
    try:
        assert rt.exchanges is None and rt.recorder is None and rt.gateway.recorder is None
        assert rt.mediator.on_review is None  # nor what the local reviewer finds
    finally:
        rt.close()


def _rules_cannot_be_built(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("the rules could not be built")

    monkeypatch.setattr(runtime_module, "build", fail)


def _closed(store, ask):
    """Whether a store's database is closed: asking it anything then fails."""
    try:
        ask(store)
    except sqlite3.ProgrammingError as refused:
        return "closed database" in str(refused)
    return False


def test_a_start_that_fails_closes_the_databases_it_opened(tmp_path, monkeypatch):
    made, opening = [], runtime_module.local_backends
    monkeypatch.setattr(
        runtime_module, "local_backends", lambda *args, **kw: made.append(opening(*args, **kw)) or made[0]
    )
    _rules_cannot_be_built(monkeypatch)
    config = Config(identity=IdentityConfig(enabled=False))
    with pytest.raises(RuntimeError, match="the rules could not be built"):
        make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    [backends] = made
    try:
        assert _closed(backends.vault, Store.load_entities) and _closed(backends.history, ExchangeStore.list)
    finally:
        backends.vault.close()
        backends.history.close()


def test_a_start_that_fails_leaves_the_backends_it_was_given_open(given, monkeypatch):
    _rules_cannot_be_built(monkeypatch)
    with pytest.raises(RuntimeError, match="the rules could not be built"):
        make_runtime(**given, root_key=ROOT_KEY)
    backends = given["backends"]  # whoever made them closes them
    assert backends.vault.load_entities() == [] and backends.history.list() == ([], None)


def test_a_history_that_cannot_be_opened_does_not_leave_the_vault_open(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "exchanges.db").write_bytes(b"not a database " * 64)
    opened, init = [], Store.__init__

    def opening(self, *args, **kwargs):
        init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Store, "__init__", opening)
    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        local_backends(data, tmp_path / "config.yaml", ROOT_KEY)
    [vault] = opened
    try:
        assert _closed(vault, Store.load_entities)
    finally:
        vault.close()


def test_new_settings_and_the_playground_make_no_conversations_of_their_own(live, monkeypatch):
    """They need the rules' masker, not an engine: an engine made for it would bring a store of conversations of this
    process, where the runtime's own (a server's) is the one to use."""
    made, init = [], LocalConversations.__init__

    def making(self, *args, **kwargs):
        made.append(self)
        init(self, *args, **kwargs)

    monkeypatch.setattr(LocalConversations, "__init__", making)
    live.update_config(lambda current: current.model_copy(update={"allow_values": ["Maria"]}))
    masker = live.gateway.dashboard._playground_masker()
    assert made == [] and masker.allow.allows("Maria") and live.built.masker.allow.allows("Maria")
    assert masker.vault is not live.built.vault  # the playground's own vault: nothing typed there is kept
