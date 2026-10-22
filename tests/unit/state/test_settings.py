"""The settings through their store: a change is weighed, applied, saved and approved there, the settings approved
last are read from it (and count as none when they cannot be read), and a reload reads config.yaml through it."""

import json
import logging
import os
import stat
import traceback
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig, Profile, ProjectEntry, TermConfig, load_config, save_config
from latet.engine.types import SlotKind
from latet.gateway.app import create_app
from latet.projects import SeenRepositories
from latet.runtime import REVIEWED_FILE, make_runtime
from latet.state import LocalSettings, local_backends
from latet.state import local as local_module
from latet.state.local import REPOSITORIES_BYTES, REPOSITORIES_FILE, REPOSITORIES_KEPT, LocalRepositories

from ...conftest import ROOT_KEY
from .conftest import _live_runtime


def test_a_change_is_prepared_strictly_before_it_applies(tmp_path, monkeypatch):
    asked = []
    prepare = LocalSettings.prepare

    def recording(self, config, *, strict=False):
        asked.append(strict)
        return prepare(self, config, strict=strict)

    monkeypatch.setattr(LocalSettings, "prepare", recording)
    rt = _live_runtime(tmp_path)
    try:
        rt.update_config(lambda current: current.model_copy(update={"allow_values": ["Maria"]}))
    finally:
        rt.close()
    assert asked[-2:] == [True, False]  # weighed first, then applied


def test_approved_settings_that_cannot_be_read_count_as_none(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / REVIEWED_FILE).write_text("{not json", "utf-8")
    store = LocalSettings(tmp_path / "config.yaml", tmp_path / "data")
    assert store.reviewed() is None
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False), history="metadata"), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        assert rt.config.history == "metadata" and rt.reviewed() == rt.config  # started, and its settings approved
    finally:
        rt.close()


def test_approved_settings_that_are_not_there_count_as_none_and_nothing_is_said(tmp_path, caplog):
    store = LocalSettings(tmp_path / "config.yaml", tmp_path / "data")
    assert store.reviewed() is None  # no data folder yet: a first start
    (tmp_path / "data").mkdir()
    assert store.reviewed() is None and caplog.records == []  # none approved yet is nothing to warn of


def _cut_short(path):
    path.write_text('{"allow_values": ["Mercury Labs"', "utf-8")


def _not_the_settings(path):
    path.write_text('{"allow_values": "Mercury Labs"}', "utf-8")  # the error of this one quotes what it read


def _not_text(path):
    path.write_bytes(b"\xff\xfe Mercury Labs")


def _a_folder_in_its_place(path):
    path.mkdir()


@pytest.mark.parametrize("damaged", [_cut_short, _not_the_settings, _not_text, _a_folder_in_its_place])
def test_approved_settings_that_are_there_and_cannot_be_read_are_said_so_by_their_file_alone(tmp_path, caplog, damaged):
    """They count as none, as when there are none (the daemon starts with config.yaml): but a config.yaml edited while
    the daemon was off then applies without waiting for you, so you are told. The warning names the file, never what
    it holds: it holds your terms, your identity and the values you let through."""
    path = tmp_path / "data" / REVIEWED_FILE
    path.parent.mkdir()
    damaged(path)
    assert LocalSettings(tmp_path / "config.yaml", tmp_path / "data").reviewed() is None
    [record] = [r for r in caplog.records if r.name == "latet.state.local"]
    assert record.levelno == logging.WARNING and str(path) in record.getMessage() and record.exc_info is None
    assert "Mercury Labs" not in caplog.text


def test_the_settings_you_approved_are_written_for_you_alone(tmp_path, monkeypatch):
    """config.reviewed.json holds your terms, your identity and the values you let through, in the data folder beside
    root.key: it is written as that key is, for you alone where the system allows it. Where files have such modes that
    is 0600; on Windows a file's access follows its folder's, and the write asks for the same."""
    asked, write = [], local_module.write_private_where_allowed

    def writing(path, text):
        asked.append(path.name)
        write(path, text)

    monkeypatch.setattr(local_module, "write_private_where_allowed", writing)
    store = LocalSettings(tmp_path / "config.yaml", tmp_path / "data")
    approved = Config(terms=[TermConfig(value="Mercury Labs", type="CLIENT")])
    store.save_reviewed(approved)
    assert asked == [REVIEWED_FILE] and store.reviewed() == approved  # and you read it back
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "data" / REVIEWED_FILE).stat().st_mode) == 0o600


def test_the_settings_you_approved_are_still_written_where_chmod_is_refused(tmp_path, chmod_refused, caplog):
    """On a mount that shows files as another owner's chmod is refused, and the copy is written all the same, with a
    warning in the log: by its file, once, never by what it holds."""
    store = LocalSettings(tmp_path / "config.yaml", tmp_path / "data")
    path = tmp_path / "data" / REVIEWED_FILE
    approved = Config(terms=[TermConfig(value="Mercury Labs", type="CLIENT")])
    store.save_reviewed(approved)
    assert store.reviewed() == approved and list(path.parent.iterdir()) == [path]
    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert str(path) in record.getMessage() and record.exc_info is None
    store.save_reviewed(Config(history="metadata"))  # the next change: not said again
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == [record]
    assert "Mercury Labs" not in caplog.text


def test_where_chmod_is_refused_a_first_start_still_approves_its_settings(tmp_path, chmod_refused):
    """Without an approved copy a config.yaml edited while the daemon is off applies without waiting for you."""
    off = IdentityConfig(enabled=False)
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    make_runtime(Config(identity=off), **start).close()  # a first install
    save_config(Config(identity=off, allow_values=["Maria"]), tmp_path / "config.yaml")  # edited meanwhile: it lowers
    rt = make_runtime(**start)
    try:
        assert rt.config.allow_values == [] and rt.reviewed() == rt.config  # it runs with the settings you approved
        [change] = rt.mediator.approvals.pending_changes()
        assert change["source"] == "start" and change["keys"] == ["allow_values"]  # and the edit waits for you
    finally:
        rt.close()


class RecordingSettings:
    """The settings of a computer (config.yaml, config.reviewed.json), recording what a runtime asks of them."""

    def __init__(self, inner):
        self.inner, self.asked = inner, []

    def load(self):
        self.asked.append("load")
        return self.inner.load()

    def save(self, config):
        self.asked.append("save")
        self.inner.save(config)

    def reviewed(self):
        self.asked.append("reviewed")
        return self.inner.reviewed()

    def save_reviewed(self, config):
        self.asked.append("save_reviewed")
        self.inner.save_reviewed(config)

    def prepare(self, config, *, strict=False):
        self.asked.append("prepare strict" if strict else "prepare")
        return self.inner.prepare(config, strict=strict)

    def since(self):
        """What was asked since the last time."""
        asked, self.asked = self.asked, []
        return asked


def _over_recording_settings(tmp_path, config=None):
    """A runtime over a computer's backends whose settings are a ``RecordingSettings``: it, and the runtime."""
    backends = local_backends(tmp_path / "data", tmp_path / "config.yaml", ROOT_KEY)
    settings = RecordingSettings(backends.settings)
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY,
                      backends=replace(backends, settings=settings))  # fmt: skip
    return rt, settings


@pytest.fixture
def recorded(tmp_path):
    """A runtime started with settings of its own, over a ``RecordingSettings``; its record holds the start."""
    rt, settings = _over_recording_settings(tmp_path, Config(identity=IdentityConfig(enabled=False)))
    yield rt, settings
    rt.close()


def _reload(rt):
    """`latet reload`: the daemon reads config.yaml again, as the local token asks."""
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        return client.post("/_latet/admin/reload", headers={"x-latet-token": rt.gateway.bridge_token}).json()


def test_a_runtime_reads_the_settings_it_is_not_given_through_its_backends(tmp_path):
    save_config(Config(identity=IdentityConfig(enabled=False), history="metadata"), tmp_path / "config.yaml")
    rt, settings = _over_recording_settings(tmp_path)
    try:
        assert rt.config.history == "metadata" and rt.settings is settings
        assert settings.since() == ["load", "reviewed", "save_reviewed"]
    finally:
        rt.close()


def test_a_runtime_asks_its_settings_for_those_approved_last_and_keeps_the_ones_it_runs_as_approved(recorded):
    rt, settings = recorded
    assert settings.since() == ["reviewed", "save_reviewed"]  # at the start
    assert rt.reviewed() == rt.config and settings.since() == ["reviewed"]


def test_a_change_is_weighed_applied_saved_and_approved_through_the_settings(recorded):
    rt, settings = recorded
    settings.since()
    rt.update_config(lambda current: current.model_copy(update={"profile": Profile.MAX}))
    assert settings.since() == ["prepare strict", "prepare", "save", "save_reviewed"]
    assert load_config(rt.config_file).profile == Profile.MAX and rt.reviewed().profile == Profile.MAX


def test_a_reload_reads_config_yaml_through_the_settings(recorded):
    rt, settings = recorded
    save_config(rt.config.model_copy(update={"profile": Profile.MAX}), rt.config_file)  # edited by hand
    settings.since()
    assert _reload(rt)["applied"] and rt.config.profile == Profile.MAX
    # The file read first, and what it is weighed against, before the repositories are (R24 M4); then under the lock.
    assert settings.since() == ["load", "reviewed", "load", "reviewed", "prepare strict", "prepare", "save_reviewed"]


# Files that are not YAML, each with a line of Mercury Labs where PyYAML finds the mistake, and where that is (1-based).
NOT_YAML = {
    "a value where none can be": ("profile: max\nterms:\n  - value: Mercury Labs: Orion\n", 3, 24),
    "a list left open": ("terms: [Mercury Labs\nprofile: max\n", 2, 8),
    "a tab to indent": ("terms:\n\t- value: Mercury Labs\n", 2, 1),
    "a character YAML refuses": ("terms:\n  - value: Mercury\x00Labs\n", 2, 19),
    "an alias": ("other: *Mercury_Labs\n", 1, 8),
    "a tag": ("terms: !!python/object:os.system Mercury Labs\n", 1, 8),
    "a date no calendar has": ("terms:\n  - value: Mercury Labs\nsince: 2024-13-45\n", 3, 8),
    "a time a whole day off": ("terms:\n  - value: Mercury Labs\nsince: 2024-01-01 10:00:00 +24:00\n", 3, 8),
    "a number of 5,000 digits": ("terms:\n  - value: Mercury Labs\nlimit: " + "7" * 5000 + "\n", 3, 8),
}


@pytest.mark.parametrize(("text", "line", "column"), NOT_YAML.values(), ids=list(NOT_YAML))
def test_a_config_yaml_that_is_not_yaml_is_a_value_error_that_says_where_and_never_what(tmp_path, text, line, column):
    """The store raises ValueError for settings that are not valid, and OSError for settings it cannot read: PyYAML's
    own error is neither, and its message quotes the line it found the mistake in, which can be a term, your identity
    or a value you let through."""
    path = tmp_path / "config.yaml"
    path.write_text(text, "utf-8")
    for load in (lambda: load_config(path), LocalSettings(path, tmp_path / "data").load):
        with pytest.raises(ValueError, match="is not valid YAML") as raised:
            load()
        assert str(raised.value) == f"{path} is not valid YAML (line {line}, column {column})"
        shown = "".join(traceback.format_exception(raised.value))  # as a traceback shows it: the chained errors too
        assert "Mercury" not in shown and "Labs" not in shown


BROKEN = r"(emily\.carter@acme\.com|Falcon Holdings"  # an allow pattern missing its ")": what it lets through


def test_a_reload_of_a_config_yaml_holding_an_allow_pattern_that_does_not_compile_applies_the_rest(tmp_path, caplog):
    """The pattern is left out (more is masked) and the answer names its place, as the log does: nothing else stops."""
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        rt.config_file.write_text(
            "identity: {enabled: false}\nterms: [{value: Mercury Labs}]\nallow_patterns: ['" + BROKEN + "']\n", "utf-8"
        )
        answer = _reload(rt)
        assert answer["applied"] and [t.value for t in rt.config.terms] == ["Mercury Labs"]
        assert answer["notices"] == ["allow_patterns entry 1 is not a valid RE2 pattern and is left out"]
        assert rt.config.allow_patterns == [] and "emily" not in caplog.text and "Falcon" not in caplog.text
    finally:
        rt.close()


def test_a_reload_of_settings_that_are_not_valid_names_their_places_and_quotes_nothing(recorded):
    rt, _ = recorded
    rt.config_file.write_text("retention_days: emily.carter@acme.com\npolicies: {Falcon Holdings: hide}\n", "utf-8")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283", raise_server_exceptions=False) as client:
        answer = client.post("/_latet/admin/reload", headers={"x-latet-token": rt.gateway.bridge_token})
    assert answer.status_code == 400
    said = answer.json()["error"]
    assert said.startswith(f"cannot read the configuration: {rt.config_file} is not valid settings: ")
    assert "retention_days: " in said and "policies.*: " in said and "emily" not in said and "Falcon" not in said


def test_a_save_in_the_dashboard_still_refuses_an_allow_pattern_that_does_not_compile(recorded):
    """A load leaves it out; whatever saves settings refuses it, 400 by its place, nothing of it said back."""
    rt, _ = recorded
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        code = rt.gateway.dashboard.issue_code()
        assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
        saved = client.put("/_latet/api/v1/config", json={"allow_patterns": ["ok-[0-9]+", BROKEN]},
                           headers={"x-latet-ui": "1"})  # fmt: skip
    assert saved.status_code == 400 and rt.config.allow_patterns == []
    [detail] = saved.json()["details"]
    assert (detail["loc"], detail["msg"]) == (["allow_patterns", 1], "Value error, not a valid RE2 pattern")
    assert "emily" not in saved.text and "Falcon" not in saved.text


def test_a_rules_save_in_the_dashboard_keeps_a_user_name_written_alone(tmp_path):
    """config.yaml names the user the older way (``username``), beside a full name that keeps the machine's own from
    being detected: a save of the Rules page, which sends the identity's switch alone, keeps that name, folded into the
    list it stands for, so that /home/emily/ still leaves masked."""
    identity = IdentityConfig(username="emily", full_name="Emily Carter")
    rt = make_runtime(Config(identity=identity), data=tmp_path / "data", config_file=tmp_path / "config.yaml",
                      root_key=ROOT_KEY)  # fmt: skip
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = rt.gateway.dashboard.issue_code()
            assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
            saved = client.put("/_latet/api/v1/config", json={"identity": {"enabled": True}, "retention_days": 30},
                               headers={"x-latet-ui": "1"})  # fmt: skip
        assert saved.status_code == 200 and rt.config.retention_days == 30
        assert rt.config.identity.usernames == ["emily"] and rt.config.identity.username is None
        assert load_config(rt.config_file).identity.usernames == ["emily"]
        session = rt.built.engine.session("cc:s1")
        masked = rt.built.engine.mask_text("cat /home/emily/notes.txt", SlotKind.TOOL_RESULT, session).text
        assert "emily" not in masked
    finally:
        rt.close()


def test_a_reload_names_by_their_places_what_a_store_refuses_without_wording_it(recorded, monkeypatch):
    """A settings store whose refusal is pydantic's own (with what it was given): the answer words it by places."""
    rt, settings = recorded

    def unworded():
        return Config.model_validate({"retention_days": "emily.carter@acme.com", "policies": {"Falcon Holdings": 1}})

    monkeypatch.setattr(settings, "load", unworded)
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283", raise_server_exceptions=False) as client:
        answer = client.post("/_latet/admin/reload", headers={"x-latet-token": rt.gateway.bridge_token})
    said = answer.json()["error"]
    assert answer.status_code == 400 and "retention_days: " in said and "policies.*: " in said
    assert "emily" not in said and "Falcon" not in said


def test_approved_settings_holding_an_allow_pattern_that_does_not_compile_still_count_without_it(tmp_path, caplog):
    """Approved by an older Latet, which took such a pattern: read as config.yaml is, so that they still count (else a
    config.yaml edited meanwhile would apply without waiting for you), the pattern left out and said by its place."""
    path = tmp_path / "data" / REVIEWED_FILE
    path.parent.mkdir()
    approved = Config(terms=[TermConfig(value="Mercury Labs")]).model_dump(mode="json")
    path.write_text(json.dumps({**approved, "allow_patterns": ["ok-[0-9]+", BROKEN]}), "utf-8")
    reviewed = LocalSettings(tmp_path / "config.yaml", tmp_path / "data").reviewed()
    assert reviewed is not None and reviewed.allow_patterns == ["ok-[0-9]+"]
    said = [r.getMessage() for r in caplog.records if r.name == "latet.state.local"]
    assert said == [f"{REVIEWED_FILE}: allow_patterns entry 2 is not a valid RE2 pattern and is left out"]
    assert "emily" not in caplog.text and "Falcon" not in caplog.text


def test_a_reload_of_a_config_yaml_that_is_not_yaml_cannot_read_it_and_logs_nothing_of_it(recorded, caplog):
    rt, settings = recorded
    rt.config_file.write_text("terms:\n  - value: Mercury Labs: Orion\n", "utf-8")
    settings.since()
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283", raise_server_exceptions=False) as client:
        answer = client.post("/_latet/admin/reload", headers={"x-latet-token": rt.gateway.bridge_token})
    assert answer.status_code == 400  # as for settings that are not valid: not a server error
    error = f"cannot read the configuration: {rt.config_file} is not valid YAML (line 2, column 24)"
    assert answer.json() == {"error": error}
    assert settings.since() == ["load", "load"] and rt.config.terms == []  # and nothing was applied
    assert "Mercury" not in answer.text + caplog.text


def test_a_change_that_waits_and_is_rejected_puts_config_yaml_back_through_the_settings(recorded):
    rt, settings = recorded
    save_config(rt.config.model_copy(update={"allow_values": ["Maria"]}), rt.config_file)  # lowers protection
    settings.since()
    assert not _reload(rt)["applied"] and rt.config.allow_values == []  # it waits for you
    assert settings.since() == ["load", "reviewed", "load", "reviewed"]  # read first (R24 M4), then under the lock
    [change] = rt.mediator.approvals.changes.values()
    assert load_config(rt.config_file).allow_values == ["Maria"]
    assert rt.decide_change(change.id, False) == "rejected"
    assert settings.since() == ["save"] and load_config(rt.config_file).allow_values == []  # config.yaml: as it was


# A term's option an agent turns on, and a form of the term that no longer matches once it is (its case its own, or
# whole words only), in a text.
FEWER_FORMS = {
    "case_sensitive": (TermConfig(value="Acme Corp", type="CLIENT"), "ACME CORP", "the ACME CORP contract"),
    "whole_word": (
        TermConfig(value="Acme Corp", type="CLIENT", whole_word=False),
        "Acme Corp",
        "the Acme Corporation contract",
    ),
}


@pytest.mark.parametrize("option", list(FEWER_FORMS))
def test_a_term_made_to_match_fewer_forms_waits_at_a_reload(tmp_path, option):
    """An agent rewrites a term of config.yaml with ``case_sensitive`` or ``whole_word`` turned on and runs `latet
    reload`: the term matches fewer forms, which lowers protection. It waits for you, and meanwhile those forms stay
    masked (the reload applied it, and "ACME CORP" left in clear)."""
    term, form, text = FEWER_FORMS[option]
    mine = Config(identity=IdentityConfig(enabled=False), terms=[term])
    rt = make_runtime(mine, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    try:
        save_config(mine.model_copy(update={"terms": [term.model_copy(update={option: True})]}), rt.config_file)
        assert not _reload(rt)["applied"]
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == ["terms: 1 matches fewer forms"]
        session = rt.built.engine.session("cc:s1")
        assert form not in rt.built.engine.mask_text(text, SlotKind.USER_TEXT, session).text
    finally:
        rt.close()


def _masked_in(rt, folder: str, address: str) -> bool:
    """Whether a private address in a tool's output of a conversation in ``folder`` leaves masked."""
    engine = rt.built.engine
    session = engine.session(f"cc:{folder}")
    engine.attach(session, folder)
    return (
        address not in engine.mask_text(f"the payroll database runs at {address}", SlotKind.TOOL_RESULT, session).text
    )


def test_a_line_of_config_yaml_that_lowers_protection_only_in_a_stricter_folder_waits_at_a_reload_and_a_start(
    tmp_path,
):
    """An agent writes one line in config.yaml: the balanced profile's own value, which changes nothing at the top, and
    in a folder of yours at the max profile turns off the masking of private addresses. It waits for you, at a reload
    and at the daemon's next start alike, and meanwhile the folder keeps them masked."""
    payroll = str(tmp_path / "payroll")
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    stricter = Config(
        identity=IdentityConfig(enabled=False), projects=[ProjectEntry(path=payroll, profile=Profile.MAX)]
    )
    rt = make_runtime(stricter, **start)
    try:
        assert _masked_in(rt, payroll, "10.1.2.3")
        save_config(stricter.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
        assert not _reload(rt)["applied"] and _masked_in(rt, payroll, "10.1.2.4")
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == ["project payroll: private IP addresses: no longer masked"]
    finally:
        rt.close()
    rt = make_runtime(**start)  # the daemon started again with that config.yaml
    try:
        assert rt.config.include_private_ips is None and _masked_in(rt, payroll, "10.1.2.5")
        [change] = rt.mediator.approvals.pending_changes()
        assert change["source"] == "start"
    finally:
        rt.close()


def _repository(tmp_path):
    """A repository whose own latet.yaml is at the max profile, and where a computer's runtime starts."""
    repo = tmp_path / "build-tools"
    (repo / ".git").mkdir(parents=True)
    (repo / "latet.yaml").write_text("profile: max\n", encoding="utf-8")
    return repo, {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}


_REPOSITORY_LOWERED = ["project build-tools: private IP addresses: no longer masked"]


def test_a_line_of_config_yaml_that_lowers_a_repositorys_stricter_profile_waits_at_a_reload(tmp_path):
    """An agent writes one line of config.yaml, the balanced profile's own value: nothing changes at the top, and in a
    repository whose own latet.yaml is at max the masking of private addresses ends. It waits for you, and meanwhile the
    repository keeps them masked (R3-4: the reload applied it, and the build server's address left in clear)."""
    repo, start = _repository(tmp_path)
    mine = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(mine, **start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.2")  # a conversation there: the repository's own profile
        save_config(mine.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
        assert not _reload(rt)["applied"]  # it waits
        assert _masked_in(rt, str(repo), "10.9.8.3")  # and the repository keeps the address masked
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == _REPOSITORY_LOWERED
    finally:
        rt.close()


def test_a_line_of_config_yaml_written_while_the_daemon_was_off_waits_where_a_repository_is_stricter(tmp_path):
    """The same line written while the daemon was off: the start knows no repository yet, so the settings approved
    before it are kept, and the repository is weighed against them when it is first seen. It keeps the address masked,
    and the line waits for you, as at a start that knew the repository; again at the next start."""
    repo, start = _repository(tmp_path)
    mine = Config(identity=IdentityConfig(enabled=False))
    make_runtime(mine, **start).close()  # approved: yours
    save_config(mine.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
    make_runtime(**start).close()  # started on it, and stopped again with no conversation in the repository
    for _ in range(2):  # then at each start until you decide
        rt = make_runtime(**start)
        try:
            project = rt.projects.resolve(str(repo))  # its first build: under the settings you approved before
            masked = project.scope.masker.mask("the build server runs at 10.9.8.3", SlotKind.TOOL_RESULT).text
            assert "10.9.8.3" not in masked
            [change] = rt.mediator.approvals.pending_changes()
            assert change["source"] == "start" and change["loosened"] == _REPOSITORY_LOWERED
            assert rt.config.include_private_ips is None and _masked_in(rt, str(repo), "10.9.8.4")  # what you approved
            assert load_config(tmp_path / "config.yaml").include_private_ips is False  # what waits for you
        finally:
            rt.close()
    rt = make_runtime(**start)
    try:
        _masked_in(rt, str(repo), "10.9.8.5")
        [change] = rt.mediator.approvals.pending_changes()
        assert rt.decide_change(change["id"], approve=True) == "approved"
        assert not _masked_in(rt, str(repo), "10.9.8.6")  # approved: the line applies there too
    finally:
        rt.close()


def test_an_import_in_the_dashboard_holds_back_what_lowers_a_repositorys_stricter_profile(tmp_path):
    """A rules file adding a personal-data type, imported in the dashboard: written out at the top, the list of types is
    used as it is in a repository seen whose own latet.yaml is at max, and detects fewer there. Weighed in the
    repositories seen, as a reload is, it is held back, never applied as protection added; a term is added."""
    repo, start = _repository(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.8")  # a conversation there: the repository is seen
        rules = "latet_rules: 1\npii: [BE_NATIONAL_NUMBER]\nterms:\n  - {value: Fonderia Brambilla}\n"
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = rt.gateway.dashboard.issue_code()
            assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
            answer = client.post("/_latet/api/v1/rules/import", json={"file": rules, "apply": True},
                                 headers={"x-latet-ui": "1"})  # fmt: skip
        assert answer.status_code == 200 and answer.json()["applied"]
        report = answer.json()["report"]
        assert [s.split(":")[0] for s in report["added"]] == ["terms"]
        assert [s.split(":")[0] for s in report["held"]] == ["personal-data types"]
        assert rt.config.pii is None and [t.value for t in rt.config.terms] == ["Fonderia Brambilla"]
    finally:
        rt.close()


def _seen_then_restarted(tmp_path):
    """A repository at max seen once (a conversation there), the daemon stopped; its next start's arguments."""
    repo, start = _repository(tmp_path)
    save_config(Config(identity=IdentityConfig(enabled=False)), tmp_path / "config.yaml")
    rt = make_runtime(**start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.1")
    finally:
        rt.close()
    return repo, start


def test_a_line_of_config_yaml_that_lowers_a_repository_seen_before_the_start_waits_at_a_reload(tmp_path):
    """R3-4's residual: the repository was seen before the daemon last started, and not since. An agent writes the line
    that ends the masking of private addresses where its latet.yaml asks for max, and runs `latet reload`: the change
    waits for you, as for a repository seen since the start, and the repository keeps the address masked (before: the
    reload knew the repositories of this run alone, applied it, and the build server's address left in clear)."""
    repo, start = _seen_then_restarted(tmp_path)
    rt = make_runtime(**start)
    try:
        save_config(rt.config.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
        assert not _reload(rt)["applied"]
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == _REPOSITORY_LOWERED
        assert rt.config.include_private_ips is None and _masked_in(rt, str(repo), "10.9.8.2")
    finally:
        rt.close()


def test_a_line_written_while_the_daemon_was_off_waits_at_the_start_where_a_repository_seen_before_is_stricter(
    tmp_path,
):
    """The same line written while the daemon was off: the start weighs it in the repositories seen before, so it
    waits from the start, before any conversation there, and the daemon runs what you approved (before: the start knew
    no repository, and the change opened only once a conversation reached the repository)."""
    repo, start = _seen_then_restarted(tmp_path)
    save_config(Config(identity=IdentityConfig(enabled=False), include_private_ips=False), tmp_path / "config.yaml")
    rt = make_runtime(**start)
    try:
        [change] = rt.mediator.approvals.pending_changes()  # at once
        assert change["source"] == "start" and change["loosened"] == _REPOSITORY_LOWERED
        assert rt.config.include_private_ips is None and _masked_in(rt, str(repo), "10.9.8.3")
    finally:
        rt.close()


def _link_folder(target, link):
    """``link``, a link to the folder ``target``: a junction on Windows, which takes no privilege, a symbolic link
    elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_repository_reached_through_a_link_is_weighed_after_a_restart(tmp_path):
    """Its conversations reach it through a link (a junction, a symbolic link), which the daemon follows while it runs:
    after a restart the reload weighs it through the link too, so the line waits and the address stays masked (before:
    an entry that was a link had nothing to weigh, the reload applied, and 10.9.8.2 left in clear there)."""
    real = tmp_path / "disk" / "repo"
    (real / ".git").mkdir(parents=True)
    (real / "latet.yaml").write_text("profile: max\n", encoding="utf-8")
    linked = tmp_path / "build-tools"
    _link_folder(real, linked)
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    save_config(Config(identity=IdentityConfig(enabled=False)), tmp_path / "config.yaml")
    rt = make_runtime(**start)
    try:
        assert _masked_in(rt, str(linked), "10.9.8.1")
    finally:
        rt.close()
    rt = make_runtime(**start)
    try:
        save_config(rt.config.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
        assert not _reload(rt)["applied"]
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == _REPOSITORY_LOWERED
        assert _masked_in(rt, str(linked), "10.9.8.2")
    finally:
        rt.close()


def test_a_repository_on_a_volume_away_at_the_start_is_weighed_once_it_is_back(tmp_path):
    """The disk the repository is on is unplugged (its parent folder renamed away) while the daemon starts, and plugged
    in again before any conversation reaches it: the entry stayed in the list, so the agent's reload waits (before: the
    start dropped it as gone, the reload applied, and the change was approved for good)."""
    volume = tmp_path / "volume"
    repo = volume / "build-tools"
    (repo / ".git").mkdir(parents=True)
    (repo / "latet.yaml").write_text("profile: max\n", encoding="utf-8")
    start = {"data": tmp_path / "data", "config_file": tmp_path / "config.yaml", "root_key": ROOT_KEY}
    save_config(Config(identity=IdentityConfig(enabled=False)), tmp_path / "config.yaml")
    rt = make_runtime(**start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.1")
    finally:
        rt.close()
    volume.rename(tmp_path / "away")  # unplugged
    rt = make_runtime(**start)
    try:
        (tmp_path / "away").rename(volume)  # plugged in again
        save_config(rt.config.model_copy(update={"include_private_ips": False}), tmp_path / "config.yaml")
        assert not _reload(rt)["applied"]
        [change] = rt.mediator.approvals.changes.values()
        assert change.loosened == _REPOSITORY_LOWERED
    finally:
        rt.close()


def test_an_import_in_the_dashboard_holds_back_what_lowers_a_repository_seen_before_the_start(tmp_path):
    """The dashboard's import weighs the same list: a type written out at the top, which detects fewer in the
    repository seen before the start, is held back; the term is added."""
    _, start = _seen_then_restarted(tmp_path)
    rt = make_runtime(**start)
    try:
        rules = "latet_rules: 1\npii: [BE_NATIONAL_NUMBER]\nterms:\n  - {value: Fonderia Brambilla}\n"
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            code = rt.gateway.dashboard.issue_code()
            assert client.get(f"/_latet/ui/login?code={code}", follow_redirects=False).status_code == 303
            answer = client.post("/_latet/api/v1/rules/import", json={"file": rules, "apply": True},
                                 headers={"x-latet-ui": "1"})  # fmt: skip
        assert answer.status_code == 200 and answer.json()["applied"]
        report = answer.json()["report"]
        assert [s.split(":")[0] for s in report["held"]] == ["personal-data types"]
        assert [s.split(":")[0] for s in report["added"]] == ["terms"]
        assert rt.config.pii is None and [t.value for t in rt.config.terms] == ["Fonderia Brambilla"]
    finally:
        rt.close()


def test_the_repositories_seen_are_written_for_you_alone(tmp_path, monkeypatch):
    """The list names your projects' folders: written as the settings you approved are, whole or not at all, for you
    alone where the system allows it (0600 where files have such modes)."""
    asked, write = [], local_module.write_private_where_allowed

    def writing(path, text):
        asked.append(path.name)
        write(path, text)

    monkeypatch.setattr(local_module, "write_private_where_allowed", writing)
    repo, start = _repository(tmp_path)
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), **start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.4")
    finally:
        rt.close()
    path = tmp_path / "data" / REPOSITORIES_FILE
    assert REPOSITORIES_FILE in asked and LocalRepositories(tmp_path / "data").load() == [str(repo)]
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(f".{REPOSITORIES_FILE}")] == []  # no copy left
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def _folders(count, prefix="repo"):
    """``count`` absolute folders, the most recent first, none of them on this disk."""
    root = "C:/dev" if os.name == "nt" else "/home/emily/dev"
    return [f"{root}/{prefix}-{n:04d}" for n in range(count)]


def test_the_repositories_seen_are_the_2000_most_recent(tmp_path):
    store = LocalRepositories(tmp_path / "data")
    folders = _folders(REPOSITORIES_KEPT + 500)
    store.save(folders)
    assert json.loads(store.path.read_text("utf-8")) == {"repositories": folders[:REPOSITORIES_KEPT]}
    assert store.load() == folders[:REPOSITORIES_KEPT]


def test_the_repositories_seen_are_written_within_their_bytes_the_most_recent_first(tmp_path):
    """Long folders: fewer of them, the most recent, and never a file larger than the list may be, which would be
    read as none."""
    store = LocalRepositories(tmp_path / "data")
    folders = _folders(600, prefix="x" * 3_900)
    store.save(folders)
    kept = store.load()
    assert 0 < len(kept) < len(folders) and kept == folders[: len(kept)]
    assert store.path.stat().st_size <= REPOSITORIES_BYTES


def test_a_list_of_repositories_written_by_hand_is_read_within_its_bounds(tmp_path):
    """Entries a list may not hold are left out (a relative folder, one with a NUL, a blank, one too long, what is not a
    text), and no more than the 2,000 first are read."""
    store = LocalRepositories(tmp_path / "data")
    store.path.parent.mkdir(parents=True)
    good = _folders(REPOSITORIES_KEPT + 10)
    bad = ["relative/folder", good[0] + "\x00", "   ", good[0] + "/" + "a" * 4_096, 42, None, ["C:/dev"]]
    store.path.write_text(json.dumps({"repositories": [*bad, *good]}), "utf-8")
    assert store.load() == good[:REPOSITORIES_KEPT]


def _not_a_list(path):
    path.write_text('{"repositories": "C:/dev/payroll-api"}', "utf-8")


def _not_json(path):
    path.write_text('{"repositories": ["C:/dev/payroll-api"', "utf-8")


def _nested(path):
    path.write_text("[" * 100_000, "utf-8")  # past what Python's reader nests: a RecursionError, never a crash


def _too_large(path):
    path.write_text(json.dumps({"repositories": ["C:/dev/payroll-api"]}) + " " * REPOSITORIES_BYTES, "utf-8")


def _a_folder(path):
    path.mkdir()


@pytest.mark.parametrize("damaged", [_not_a_list, _not_json, _nested])
def test_a_list_of_repositories_that_cannot_be_read_counts_as_none_and_is_said_by_its_file_alone(
    tmp_path, caplog, damaged
):
    """Read, and not a list of folders: it counts as none, as the settings you approved do when they cannot be read,
    and the log says so by the file and why, never by what it holds (folders); the next list written replaces it."""
    store = LocalRepositories(tmp_path / "data")
    store.path.parent.mkdir(parents=True)
    damaged(store.path)
    assert store.load() == []
    [record] = [r for r in caplog.records if r.name == "latet.state.local"]
    assert record.levelno == logging.WARNING and str(store.path) in record.getMessage() and record.exc_info is None
    assert "payroll" not in caplog.text
    store.save(["C:/dev/billing-api" if os.name == "nt" else "/home/emily/dev/billing-api"])
    assert len(store.load()) == 1


@pytest.mark.parametrize("unreadable", [_too_large, _a_folder])
def test_a_list_of_repositories_that_is_not_read_at_all_is_unavailable_never_none(tmp_path, caplog, unreadable):
    """What the reader does not read (a file larger than the list may be, a folder or anything else that is not a
    regular file of its own) is never taken for an empty list (R24 M3): it is unavailable now, nothing is written over
    it, and a change weighed in the repositories waits until it can be read."""
    store = LocalRepositories(tmp_path / "data")
    store.path.parent.mkdir(parents=True)
    unreadable(store.path)
    before = store.path.read_bytes() if store.path.is_file() else None
    assert store.load() is None
    [record] = [r for r in caplog.records if r.name == "latet.state.local"]
    assert "left as it is" in record.getMessage() and "payroll" not in caplog.text
    seen = SeenRepositories(store)
    seen.saw("C:/dev/billing-api" if os.name == "nt" else "/home/emily/dev/billing-api")
    assert (store.path.read_bytes() if store.path.is_file() else None) == before and store.path.exists()
    assert not seen.weighed().complete


def test_settings_found_at_the_start_that_lower_no_repository_seen_since_apply_without_a_question(tmp_path):
    """A line written while the daemon was off that lowers nothing in the repository (a term removed nowhere, the port)
    applies at its start as before: the repository is weighed and nothing waits."""
    repo, start = _repository(tmp_path)
    mine = Config(identity=IdentityConfig(enabled=False))
    make_runtime(mine, **start).close()
    save_config(mine.model_copy(update={"port": 5299}), tmp_path / "config.yaml")
    rt = make_runtime(**start)
    try:
        assert _masked_in(rt, str(repo), "10.9.8.7") and rt.config.port == 5299
        assert rt.mediator.approvals.pending_changes() == []
    finally:
        rt.close()


def test_destinations_added_while_a_change_waits_are_saved_through_the_settings(recorded):
    rt, settings = recorded
    save_config(rt.config.model_copy(update={"allow_values": ["Maria"]}), rt.config_file)
    assert not _reload(rt)["applied"]
    settings.since()
    [name] = rt.add_destinations([("https://llm.acme.com", "claude")])
    assert settings.since() == ["reviewed", "prepare strict", "prepare", "save_reviewed", "reviewed", "save"]
    saved = load_config(rt.config_file)  # config.yaml holds what waits for you, with the destination in it
    assert saved.allow_values == ["Maria"] and [d.name for d in saved.upstreams] == [name]
