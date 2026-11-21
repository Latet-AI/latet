"""`unknown_members`: how a request member no request profile names is read. `check` (the default, and what every
installation moves to on upgrade, with no approval) reads it whole and checks it; `refuse` refuses the request, as
Latet did before 0.15.0. Moving to `check` lowers protection and waits for approval; a project, a repository's
latet.yaml too, may only set `refuse`; a company's floor may force `refuse`. `latet doctor` and `latet server status`
say which value is in force."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from latet.config import (
    Config,
    IdentityConfig,
    ProjectEntry,
    ProjectSettings,
    config_path,
    data_dir,
    load_config,
    save_config,
)
from latet.doctor import checks
from latet.floor import BelowFloor, raised_to
from latet.project_file import _project_settings
from latet.projects import effective_config, repo_settings
from latet.protection import _repositories, effective_unknown_members, loosened, lowered_in_project
from latet.runtime import make_runtime
from latet.server.spaces import _setting_logged
from latet.state.local import LocalSettings
from latet.transfer import export_rules, merge_rules, read_rules

from ..floors import FloorEdition
from .conftest import run_cli

LOWERED = "new request members: passed after a check instead of refused"
CHECK, REFUSE = Config(), Config(unknown_members="refuse")
SHOP = "C:/work/shop"


class RefuseFloor:
    """A company's floor that forces `unknown_members: refuse` (as Latet Enterprise's does, ``ee/policy.py``), so that
    the free code's side is tested without ee/."""

    locks_terms = carves_terms = decides_exceptions = False

    def below(self, config: Config) -> list[str]:
        return loosened(self.raise_to(config), config)

    def raise_to(self, config: Config) -> Config:
        return config.model_copy(update={"unknown_members": "refuse"})


# --- the setting and the upgrade -----------------------------------------------------------------------------------


def test_settings_without_the_key_read_as_check_and_the_upgrade_waits_for_no_approval(computer):
    """An installation from before 0.15.0 has no value: its config.yaml and the settings it approved last read as
    `check`, and moving to this version lowers nothing that waits (the owner's choice of 2026-11-16)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("profile: balanced\nhistory: full\n", "utf-8")
    assert load_config(path).unknown_members == "check"
    assert Config.model_validate({}).unknown_members == "check"
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / "config.reviewed.json").write_text('{"profile": "balanced"}', "utf-8")
    approved = LocalSettings(None, data_dir()).reviewed()
    assert approved is not None and approved.unknown_members == "check"
    assert loosened(approved, load_config(path)) == []


def test_only_check_and_refuse_are_values():
    with pytest.raises(ValidationError):
        Config.model_validate({"unknown_members": "allow"})
    assert "unknown_members" in Config.model_fields and Config.model_fields["unknown_members"].description


# --- what lowers protection ----------------------------------------------------------------------------------------


def test_moving_to_check_is_a_loosening_said_in_its_words_and_moving_to_refuse_is_not():
    assert loosened(REFUSE, CHECK) == [LOWERED]
    assert loosened(CHECK, REFUSE) == []
    assert loosened(CHECK, CHECK) == loosened(REFUSE, REFUSE) == []


def test_a_projects_rules_weigh_it_too():
    assert lowered_in_project(REFUSE, CHECK) == [LOWERED]
    assert lowered_in_project(CHECK, REFUSE) == []


def test_a_project_of_yours_that_stops_refusing_is_a_loosening_of_that_project():
    """``_projects``: an entry of yours that sets `refuse`, taken away, lowers that project alone; one added lowers
    nothing; the top moving to `check` is said once, never again for each folder, and not for a folder that keeps
    refusing on its own."""
    strict_shop = Config(projects=[ProjectEntry(path=SHOP, unknown_members="refuse")])
    plain_shop = Config(projects=[ProjectEntry(path=SHOP)])
    assert loosened(strict_shop, plain_shop) == [f"project shop: {LOWERED}"]
    assert loosened(plain_shop, strict_shop) == []
    many = [ProjectEntry(path=f"C:/work/shop{n}", profile="max") for n in range(5)]
    assert loosened(REFUSE.model_copy(update={"projects": many}), CHECK.model_copy(update={"projects": many})) == [
        LOWERED
    ]
    kept = REFUSE.model_copy(update={"projects": strict_shop.projects})
    assert loosened(kept, CHECK.model_copy(update={"projects": strict_shop.projects})) == [LOWERED]


def test_a_repositorys_latet_yaml_is_weighed_as_it_applies_there():
    """``_repositories``: the top moving to `check` lowers a repository whose latet.yaml does not refuse on its own,
    never one whose file sets `refuse`; with the top's reasons given, nothing is said twice."""
    refusing = ("C:/repos/billing", ProjectSettings(unknown_members="refuse"))
    silent = ("C:/repos/shop", ProjectSettings(profile="max"))
    assert _repositories(REFUSE, CHECK, [refusing, silent], top=()) == [f"project shop: {LOWERED}"]
    assert _repositories(REFUSE, CHECK, [refusing, silent], top={LOWERED}) == []
    assert loosened(REFUSE, CHECK, [refusing, silent]) == [LOWERED]
    assert loosened(CHECK, REFUSE, [refusing, silent]) == []


# --- a project's own settings: only tightening ------------------------------------------------------------------------


def test_a_repository_file_may_set_refuse_and_it_applies_there():
    settings, notices = repo_settings({"unknown_members": "refuse"})
    assert settings.unknown_members == "refuse" and notices == []
    applied, more = effective_config(CHECK, settings, None)
    assert applied.unknown_members == "refuse" and more == []
    assert effective_unknown_members(CHECK, settings) == "refuse"


def test_a_repository_file_that_says_check_is_a_notice_and_ignored():
    settings, notices = _project_settings(b"unknown_members: check\nprofile: max\n")
    assert settings is not None and settings.unknown_members is None and settings.profile == "max"
    [notice] = notices
    assert (notice.source, notice.key, notice.level) == ("repo", "unknown_members", "ignored")
    assert "refuse" in notice.message and "config.yaml" in notice.message
    applied, _ = effective_config(REFUSE, settings, None)
    assert applied.unknown_members == "refuse"  # the file loosens nothing of yours


def test_your_own_settings_for_a_project_may_only_set_refuse():
    entry = ProjectEntry(path=SHOP, unknown_members="refuse")
    assert effective_config(CHECK, None, entry)[0].unknown_members == "refuse"
    assert effective_config(REFUSE, None, ProjectEntry(path=SHOP))[0].unknown_members == "refuse"
    with pytest.raises(ValidationError):
        ProjectEntry(path=SHOP, unknown_members="check")  # type: ignore[arg-type]


def test_a_rules_file_carries_refuse_and_its_import_adds_it_at_once():
    text, _ = export_rules(REFUSE)
    assert "unknown_members: refuse" in text
    assert "unknown_members" not in export_rules(CHECK)[0]
    imported, notices = read_rules(text)
    assert imported.unknown_members == "refuse" and notices == []
    merged = merge_rules(CHECK, imported)
    assert merged.protective.unknown_members == "refuse" and merged.report.held == []
    assert merge_rules(REFUSE, imported).protective.unknown_members == "refuse"
    loose, said = read_rules("unknown_members: check\n")
    assert loose.unknown_members is None and [line.split(":")[0] for line in said] == ["unknown_members"]


# --- the value in force -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("top", ["check", "refuse"])
@pytest.mark.parametrize("project", [None, "unset", "refuse"])
@pytest.mark.parametrize("floored", [False, True])
def test_refuse_is_in_force_where_the_settings_the_project_or_the_floor_says_so(top, project, floored):
    config = Config(unknown_members=top)
    settings = None if project is None else ProjectSettings() if project == "unset" else ProjectSettings(
        unknown_members="refuse"
    )  # fmt: skip
    in_use = raised_to(RefuseFloor() if floored else None, config)
    expected = "refuse" if "refuse" in (top, project) or floored else "check"
    assert effective_unknown_members(in_use, settings) == expected
    if settings is not None:  # the same through the project's rules as a conversation reads them
        assert effective_unknown_members(effective_config(in_use, settings, None)[0], None) == expected


def test_a_floor_that_forces_refuse_finds_check_below_it():
    floor = RefuseFloor()
    assert floor.below(CHECK) == [LOWERED] and floor.below(REFUSE) == []


def test_the_runtime_reads_it_from_the_settings_in_use_and_refuses_a_change_below_the_floor(tmp_path):
    """``Runtime.unknown_members``: what the gateway asks, from the settings raised to the company's floor (yours as you
    set them) and the project's own; your `refuse` taken away below a floor that forces it is refused, never held."""
    mine = Config(identity=IdentityConfig(enabled=False), ner="off")
    plain = make_runtime(mine, data=tmp_path / "plain", edition=FloorEdition(None))
    try:
        assert plain.unknown_members() == "check"
        assert plain.unknown_members(ProjectSettings(unknown_members="refuse")) == "refuse"
    finally:
        plain.close()
    floored = make_runtime(mine, data=tmp_path / "floored", edition=FloorEdition(RefuseFloor()))
    try:
        assert floored.config.unknown_members == "check" and floored.unknown_members() == "refuse"
        with pytest.raises(BelowFloor, match="new request members"):
            floored.check_floor(mine, before=mine.model_copy(update={"unknown_members": "refuse"}))
    finally:
        floored.close()


# --- what the doctor and the server's status say --------------------------------------------------------------------


def _line():
    found = checks.check_unknown_members()
    assert [check.title for check in found] == ["New request members"], found
    assert found[0].status == checks.OK
    return found[0].detail


DEFAULT_SINCE = (
    "the default since 0.15.0 (before, such a request was refused): `unknown_members: refuse` in config.yaml"
)


def test_the_doctor_says_the_value_in_force(computer):
    assert "check_unknown_members" in checks.COMPUTER_GROUPS
    assert "`unknown_members: check`" in _line()  # nothing written: the default
    save_config(REFUSE, config_path())
    assert "`unknown_members: refuse`" in _line()
    # D6's string case still passes under `refuse` (body_coverage runs it before the refusal): the line says so.
    assert "Claude Code, Bedrock and Vertex a field of text alone" in _line()


def test_the_doctor_says_check_came_with_the_upgrade_until_config_yaml_names_it(computer):
    """The spec's "says it once after the upgrade": while config.yaml has no `unknown_members` key, the line says `check`
    is the default since 0.15.0 and how to refuse again; once the key is written, whatever its value, it says the value
    alone."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("profile: balanced\n", "utf-8")  # as an installation from before 0.15.0 left it
    said = _line()
    assert said.startswith("`unknown_members: check`") and DEFAULT_SINCE in said
    path.write_text("profile: balanced\nunknown_members: check\n", "utf-8")
    assert _line().startswith("`unknown_members: check`") and "0.15.0" not in _line()
    path.write_text("profile: balanced\nunknown_members: refuse\n", "utf-8")
    assert _line().startswith("`unknown_members: refuse`") and "0.15.0" not in _line()
    path.write_text("profile: balanced\n", "utf-8")  # the key taken out while `refuse` is what you approved
    LocalSettings(None, data_dir()).save_reviewed(REFUSE)
    said = _line()
    assert said.startswith("`unknown_members: refuse`") and "waits for your approval" in said and "0.15.0" not in said


def test_the_doctor_says_what_the_daemon_runs_and_what_waits(computer):
    """The settings you approved last are what the daemon runs: config.yaml's `check` waits for you meanwhile, and its
    `refuse` applies once the daemon reads it."""
    save_config(CHECK, config_path())
    LocalSettings(None, data_dir()).save_reviewed(REFUSE)
    said = _line()
    assert said.startswith("`unknown_members: refuse`") and "waits for your approval" in said
    save_config(REFUSE, config_path())
    LocalSettings(None, data_dir()).save_reviewed(CHECK)
    said = _line()
    assert said.startswith("`unknown_members: check`") and "latet reload" in said


def _status(**more):
    """A server's status answer, as the administration gives it, with ``more``."""
    return {
        "url": "https://latet.acme.lan", "version": "0.15.0", "identities": {"people": 1, "keys": 0, "enabled": 1},
        "vault": {"limit": 200000, "near": []}, "audit": {"chain": "ok"}, "edition": [], **more,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("value", "said"),
    [
        ("check", "new request members: read and checked (`unknown_members: check`"),
        ("refuse", "new request members: refused for every identity (`unknown_members: refuse`"),
        (None, "new request members: cannot be said now"),
    ],
)
def test_the_servers_status_says_the_value_in_force(computer, monkeypatch, value, said):
    monkeypatch.setenv("LATET_SERVER_URL", "https://latet.acme.lan")
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    monkeypatch.setattr("latet.cli.server.admin_call", lambda method, path, body=None, **kwargs: _status(
        unknown_members=value
    ))  # fmt: skip
    done = run_cli("server", "status")
    assert done.exit_code == 0, done.output
    assert said in done.output and done.output.count("new request members") == 1


def test_the_status_of_an_older_server_says_nothing_of_it(computer, monkeypatch):
    monkeypatch.setenv("LATET_SERVER_URL", "https://latet.acme.lan")
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    monkeypatch.setattr("latet.cli.server.admin_call", lambda method, path, body=None, **kwargs: _status())
    done = run_cli("server", "status")
    assert done.exit_code == 0 and "new request members" not in done.output


# --- an approval that moves it to `check`, in a server's administration log -------------------------------------------

CHECKED = {"setting": "unknown_members", "value": "check"}
HERS = SimpleNamespace(id="u_emilycarte", name="Emily Carter", kind="person")
KEY = SimpleNamespace(id="u_billingci2", name="Billing CI", kind="key")


@pytest.fixture
def spaced(tmp_path):
    """A space of ``who``'s on these settings, as a server builds one (its identity, an edition given), its approvals
    written to ``logged`` as the server's hook writes them to the administration log (``None``: no hook, a computer)."""
    made = []

    def make(who=HERS, logged=(), **settings):
        config = Config(identity=IdentityConfig(enabled=False), ner="off", **settings)
        folder = tmp_path / f"{who.id}-{len(made)}"
        rt = make_runtime(config, data=folder, config_file=folder / "config.yaml", identity=who, edition=FloorEdition())
        rt.setting_log = None if logged is None else logged.append  # type: ignore[union-attr]
        made.append(rt)
        return rt

    yield make
    for rt in made:
        rt.close()


def _approved(rt, **update):
    """``update`` asked for and approved by the person herself, as her dashboard approves a change that waits."""
    change = rt.propose(rt.config.model_copy(update=update), "command")
    assert change is not None and rt.decide_change(change.id, True) == "approved"


def test_her_approval_of_check_is_logged_once_by_its_scope(spaced):
    logged: list = []
    rt = spaced(logged=logged, unknown_members="refuse")
    _approved(rt, unknown_members="check")
    assert logged == [{**CHECKED, "scope": "identity"}]
    shop, billing = ProjectEntry(path=SHOP, unknown_members="refuse"), ProjectEntry(path="C:/work/billing")
    logged.clear()
    rt = spaced(logged=logged, projects=[shop, billing.model_copy(update={"unknown_members": "refuse"})])
    _approved(rt, projects=[ProjectEntry(path=SHOP), billing])  # both stop refusing, the settings at `check`
    assert logged == [{**CHECKED, "scope": "project", "projects": 2}]
    assert "shop" not in repr(logged) and "billing" not in repr(logged)  # never a folder


def test_her_own_save_that_moves_it_to_check_is_logged_as_hers(spaced):
    """A save that applies at once (her dashboard's, when nobody else decides) is her approval as well, logged with the
    identity as the actor. The administration's save of an API key's settings (``approving``) writes none here: its
    route writes `settings.changed` alone, with the administration's token as the actor, once the approval is kept."""
    logged: list = []
    rt = spaced(logged=logged, unknown_members="refuse")
    rt.update_config(lambda c: c.model_copy(update={"unknown_members": "check"}), from_dashboard=True)
    assert logged == [{**CHECKED, "scope": "identity"}]
    logged.clear()
    key = spaced(KEY, logged=logged, unknown_members="refuse")
    key.update_config(lambda c: c.model_copy(update={"unknown_members": "check"}), approving=True)
    assert key.config.unknown_members == "check" and logged == []


def test_no_entry_for_refuse_for_an_approval_that_leaves_it_alone_or_without_a_server(spaced):
    logged: list = []
    rt = spaced(logged=logged)
    rt.update_config(lambda c: c.model_copy(update={"unknown_members": "refuse"}), from_dashboard=True)
    key = spaced(KEY, logged=logged)
    key.update_config(lambda c: c.model_copy(update={"unknown_members": "refuse"}), approving=True)
    for left in (spaced(logged=logged, unknown_members="refuse"), spaced(logged=logged)):  # another loosening alone
        _approved(left, llm_review=left.config.llm_review.model_copy(update={"guidance": "Report nothing."}))
    kept = spaced(logged=logged, unknown_members="refuse", projects=[ProjectEntry(path=SHOP, unknown_members="refuse")])
    kept.update_config(lambda c: c.model_copy(update={"projects": [ProjectEntry(path=SHOP)]}))  # still refused there
    assert logged == []
    computer = spaced(logged=None, unknown_members="refuse")
    _approved(computer, unknown_members="check")  # no hook: approved all the same
    assert computer.config.unknown_members == "check"


def test_an_entry_that_cannot_be_written_leaves_the_approval_standing(spaced, caplog):
    def broken(details):
        raise ConnectionError("postgresql://latet:secret@db.acme.lan said no")

    rt = spaced(logged=[], unknown_members="refuse")
    rt.setting_log = broken
    _approved(rt, unknown_members="check")
    assert rt.config.unknown_members == "check" and rt.reviewed().unknown_members == "check"
    assert "ConnectionError" in caplog.text and "secret" not in caplog.text and "db.acme" not in caplog.text


def test_the_administration_log_has_the_decider_as_the_actor():
    recorded = []
    server = SimpleNamespace(audit=SimpleNamespace(record=lambda *entry: recorded.append(entry)))
    _setting_logged(server, "u_emilycarte", {**CHECKED, "scope": "identity", "by": "u_markelliss"})
    _setting_logged(server, "u_emilycarte", {**CHECKED, "scope": "project", "projects": 2})
    assert recorded == [
        ("u_markelliss", "setting.loosened", "u_emilycarte", {**CHECKED, "scope": "identity"}),
        ("u_emilycarte", "setting.loosened", "u_emilycarte", {**CHECKED, "scope": "project", "projects": 2}),
    ]
