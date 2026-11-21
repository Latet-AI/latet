"""The settings are built on the company's floor (Latet Enterprise, through Edition.floor): yours stay as you set
them, the ones in use are raised, and a change that goes further below the floor is refused, never held for
approval."""

import logging
from types import SimpleNamespace

import pytest

from latet.config import Config, IdentityConfig, Profile, ProjectEntry, TermConfig, load_config, save_config
from latet.engine.masker import MaskReport
from latet.engine.types import SlotKind
from latet.engine.vault import MaskingError
from latet.floor import BelowFloor, fresh_reasons, refusal
from latet.projects import RepositoryScan, masker_weight
from latet.runtime import Runtime, digest, make_runtime

from ..floors import CompanyFloor, FloorEdition, TermsFloor

FALCON = TermConfig(value="Project Falcon", type="PROJECT")


def _runtime(tmp_path, profile="max", locked=True):
    edition = FloorEdition(CompanyFloor() if locked else None)
    config = Config(profile=profile, identity=IdentityConfig(enabled=False))
    save_config(config, tmp_path / "config.yaml")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    return rt, edition


@pytest.fixture
def made(tmp_path):
    runtimes = []

    def make(profile="max", locked=True):
        rt, edition = _runtime(tmp_path, profile, locked)
        runtimes.append(rt)
        return rt, edition

    yield make
    for rt in runtimes:
        rt.close()


@pytest.fixture
def floored(tmp_path):
    """A runtime with these settings over an edition whose floor is ``floor`` (None: none yet)."""
    runtimes = []

    def make(floor=None, folder="here", **settings):
        edition = FloorEdition(floor)
        config = Config(identity=IdentityConfig(enabled=False), ner="off", **settings)
        where = tmp_path / folder
        where.mkdir()
        save_config(config, where / "config.yaml")
        rt = make_runtime(config, data=where / "data", config_file=where / "config.yaml", edition=edition)
        runtimes.append(rt)
        return rt, edition

    yield make
    for rt in runtimes:
        rt.close()


def test_without_a_floor_latet_behaves_as_today(tmp_path):
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data")
    try:
        assert rt.floor() is None and rt.in_use() is rt.config
        assert rt.built.engine.rules_tag == digest(rt.config)
    finally:
        rt.close()


def test_the_settings_in_use_are_raised_and_yours_stay_as_you_set_them(made):
    rt, _ = made(profile="balanced")
    assert rt.config.profile == "balanced" and rt.in_use().profile == "max"
    assert rt.mediator.images.send == "block"  # max sends no images: the engine is built on the settings in use
    assert load_config(rt.config_file).profile == "balanced"


def test_a_change_below_the_floor_is_refused_and_nothing_changes(made):
    rt, edition = made()
    version = rt.built.engine.rules_version
    with pytest.raises(BelowFloor) as refused:
        rt.update_config(lambda current: current.model_copy(update={"profile": Profile.BALANCED}))
    assert refused.value.reasons == ["profile: from max to balanced"]
    assert str(refused.value) == "Set by your company, not changed: profile: from max to balanced"
    assert rt.config.profile == "max" and rt.built.engine.rules_version == version
    assert load_config(rt.config_file).profile == "max"
    told = [(e["reason"], e["outcome"], e["identity"]) for e in edition.events if e["type"] == "policy"]
    assert told == [("below-floor", "refused", None)]


def test_a_refusal_in_a_space_on_a_server_names_whose_space_it_was(made):
    rt, edition = made()
    rt.identity = SimpleNamespace(id="u_7f3a2b9c1d", name="Billing CI", kind="key")  # E0a's: an API key's space
    with pytest.raises(BelowFloor):
        rt.update_config(lambda current: current.model_copy(update={"profile": Profile.SECRETS}))
    [event] = [e for e in edition.events if e["type"] == "policy"]
    assert (event["identity"], event["kind"]) == ("u_7f3a2b9c1d", "key")


def test_a_change_below_the_floor_is_refused_not_held_for_approval(made):
    rt, _ = made()
    with pytest.raises(BelowFloor):
        rt.propose(rt.config.model_copy(update={"profile": Profile.SECRETS}), "command", "hermes")
    assert rt.mediator.approvals.changes == {}


@pytest.mark.parametrize("source", ["file", "start"])
def test_a_change_that_arrives_is_weighed_for_approval_never_refused(made, source):
    """Ruling 11 and L5: settings that arrive (a file read again, a start, on a server another instance's change) are
    raised where they apply, never refused where they are weighed: what lowers protection waits, as without a floor."""
    rt, edition = made()
    change = rt.propose(rt.config.model_copy(update={"profile": Profile.SECRETS}), source)
    assert change is not None and list(rt.mediator.approvals.changes) == [change.id]
    assert [e for e in edition.events if e["type"] == "policy"] == []


def test_a_change_unrelated_to_what_is_below_the_floor_still_saves(made):
    rt, _ = made(profile="balanced")  # below the floor since before it came
    rt.update_config(lambda current: current.model_copy(update={"terms": [TermConfig(value="Northwind Traders")]}))
    assert [t.value for t in rt.config.terms] == ["Northwind Traders"] and rt.in_use().profile == "max"
    with pytest.raises(BelowFloor):  # further below: refused
        rt.update_config(lambda current: current.model_copy(update={"profile": Profile.SECRETS}))


def test_a_change_toward_the_floor_or_stricter_is_never_refused_and_one_that_lowers_a_setting_below_it_is(made):
    """A refusal names what the change itself lowers: its counts and its "from ... to" move with any change, so a
    change of settings below the floor since before is weighed by the settings it lowers, not by its words."""
    rt, _ = made(profile="secrets")  # below the floor since before it came
    rt.apply_config(rt.config.model_copy(update={"allow_values": ["acme.com", "globex.com"]}), save=False)
    rt.update_config(lambda current: current.model_copy(update={"profile": Profile.BALANCED}))  # toward max
    rt.update_config(lambda current: current.model_copy(update={"allow_values": ["acme.com"]}))  # one exception less
    assert (rt.config.profile, rt.config.allow_values) == ("balanced", ["acme.com"])
    with pytest.raises(BelowFloor) as refused:
        rt.update_config(lambda current: current.model_copy(update={"profile": Profile.SECRETS}))
    assert refused.value.reasons == ["profile: from max to secrets"]  # not the exceptions it did not touch


def test_a_refusal_in_a_project_names_the_folder_the_change_lowers(made, tmp_path):
    rt, _ = made()
    billing = ProjectEntry(path=str(tmp_path / "billing"), enabled=False)  # off there since before the floor
    shop = ProjectEntry(path=str(tmp_path / "shop"))
    rt.apply_config(rt.config.model_copy(update={"projects": [billing, shop]}), save=False)
    off = [billing, shop.model_copy(update={"enabled": False})]
    with pytest.raises(BelowFloor) as refused:
        rt.update_config(lambda current: current.model_copy(update={"projects": off}))
    assert refused.value.reasons == ["project shop: Latet turned off"]
    on = [billing.model_copy(update={"enabled": True}), shop]
    rt.update_config(lambda current: current.model_copy(update={"projects": on}))  # back on: nothing lowered
    assert all(entry.enabled for entry in rt.config.projects)


NAMES = ("jazz", "rock and roll", "a, b", "c", "d")  # folders whose names hold the words a list of folders is said in


def _folders(*off):
    """Settings at the floor's profile with a project for each of ``NAMES``, Latet turned off in ``off``."""
    return Config(profile="max", projects=[ProjectEntry(path=f"C:/work/{n}", enabled=n not in off) for n in NAMES])


def test_a_refusal_names_each_folder_the_change_lowers_whatever_its_name():
    """The change's own reasons say several folders at once (``projects jazz and rock and roll: ...``, ``projects
    jazz, rock and roll and 3 more: ...``): the folders are weighed one by one, never read back out of those words."""
    floor = CompanyFloor()

    def refused(before, after):
        return sorted(fresh_reasons(floor, before, after))

    assert refused(_folders(), _folders("jazz", "rock and roll")) == [
        "project jazz: Latet turned off",
        "project rock and roll: Latet turned off",
    ]
    assert refused(_folders(), _folders("a, b", "c")) == [
        "project a, b: Latet turned off",
        "project c: Latet turned off",
    ]
    assert len(refused(_folders(), _folders(*NAMES))) == len(NAMES)  # counted in the change's words: each refused
    # Off in one since before: only the folder the change turns off is refused; turned back on, none.
    assert refused(_folders("a, b"), _folders("a, b", "rock and roll")) == ["project rock and roll: Latet turned off"]
    assert refused(_folders("a, b"), _folders()) == []
    # The profile lowered at the top: said once, for the settings themselves.
    lowered = _folders().model_copy(update={"profile": Profile.SECRETS})
    assert refused(_folders(), lowered) == ["profile: from max to secrets"]


class Counted(CompanyFloor):
    def __init__(self):
        self.asked = 0

    def below(self, config):
        self.asked += 1
        return super().below(config)


def test_only_the_folders_whose_own_entry_changes_are_weighed_one_by_one(monkeypatch):
    """A floor's look costs a raise (Latet Enterprise: up to thousands of terms), and a weighing of a folder costs one
    too: a change of the settings themselves under two hundred folders weighs them once, and asks the floor twice."""
    from latet import protection

    weighed = []
    loosened = protection.loosened
    monkeypatch.setattr(protection, "loosened", lambda old, new, *a: weighed.append(1) or loosened(old, new, *a))
    folders = [ProjectEntry(path=f"C:/work/f{n}") for n in range(200)]
    before = Config(profile="max", projects=folders)
    floor = Counted()
    assert fresh_reasons(floor, before, before.model_copy(update={"profile": Profile.SECRETS})) != []
    assert floor.asked == 2 and len(weighed) == 1


def test_a_change_toward_the_floor_that_lowers_another_setting_is_weighed_by_that_one(made):
    rt, _ = made(profile="secrets")
    rt.apply_config(rt.config.model_copy(update={"phone_regions": ["IT", "DE"]}), save=False)
    rt.update_config(lambda current: current.model_copy(update={"profile": Profile.BALANCED, "phone_regions": ["IT"]}))
    assert rt.config.profile == "balanced" and rt.config.phone_regions == ["IT"]


def test_settings_that_arrive_are_raised_never_refused(made):
    rt, edition = made()
    # What an instance applies because it was told (the shared settings, a change made on another instance): ruling 11.
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.SECRETS}), save=False)
    assert rt.config.profile == "secrets" and rt.in_use().profile == "max"
    assert [e for e in edition.events if e["type"] == "policy"] == []  # nothing refused


def test_a_change_waiting_when_the_floor_comes_is_rejected_when_approved(made):
    rt, edition = made(locked=False)
    change = rt.propose(rt.config.model_copy(update={"profile": Profile.BALANCED}), "command", "hermes")
    assert change is not None
    edition.locked = CompanyFloor()
    with pytest.raises(BelowFloor):
        rt.decide_change(change.id, approve=True)
    assert rt.mediator.approvals.settled[change.id] == "rejected" and rt.config.profile == "max"


def test_a_destination_added_while_a_change_below_the_floor_waits_rejects_that_change(made):
    rt, edition = made(locked=False)
    change = rt.propose(rt.config.model_copy(update={"profile": Profile.BALANCED}), "command", "hermes")
    edition.locked = CompanyFloor()
    assert len(rt.add_destinations([("https://llm.example.com", "claude")])) == 1
    assert rt.mediator.approvals.settled[change.id] == "rejected"
    assert len(load_config(rt.config_file).upstreams) == 1 and load_config(rt.config_file).profile == "max"


def test_a_daemon_started_over_a_config_yaml_below_the_floor_keeps_what_was_approved(tmp_path):
    """The file arrives: what was approved runs, raised, and the change it holds waits for you as at any start (L5),
    where it can only be rejected."""
    rt, edition = _runtime(tmp_path)  # approved: max
    rt.close()
    save_config(Config(profile="secrets", identity=IdentityConfig(enabled=False)), rt.config_file)  # edited by hand
    again = make_runtime(data=rt.data, config_file=rt.config_file, edition=edition)
    try:
        assert again.config.profile == "max" and again.in_use().profile == "max"
        assert load_config(rt.config_file).profile == "secrets"  # the file stays as you wrote it
        [change] = again.mediator.approvals.waiting_changes()
        with pytest.raises(BelowFloor):
            again.decide_change(change.id, approve=True)
        assert again.mediator.approvals.settled[change.id] == "rejected" and again.config.profile == "max"
        assert load_config(rt.config_file).profile == "max"  # undone in config.yaml, as a rejection is
    finally:
        again.close()


def test_when_the_floor_changes_the_settings_are_built_on_it_again(made):
    rt, edition = made(profile="balanced", locked=False)
    assert rt.in_use().profile == "balanced" and len(edition.watchers) == 1
    edition.locked = CompanyFloor()
    edition.watchers[0]()  # the runtime's floor_changed, as the edition's floor_changed calls each watcher
    assert rt.in_use().profile == "max" and rt.config.profile == "balanced"


def test_settings_a_servers_store_refuses_put_back_the_floor_they_were_raised_to(made, monkeypatch):
    """The merge of 0.12.0 into enterprise: a save a server's store refuses puts back what ran before it as it ran
    (``Runtime._put_back``), the settings raised to the company's floor, that floor and the identity provider's name
    among it. Without them, the settings were put back while the floor and the settings raised to it stayed the refused
    ones: ``in_use`` (the Rules page, the dashboard's view) showed settings that never ran, and the dashboard's list of
    request exceptions weighed them under a floor that was not in force (``floor_used``)."""
    rt, edition = made(profile="balanced", locked=False)
    ran, floor, before = rt.in_use(), rt.floor_used, rt.config
    rt.identity = SimpleNamespace(id="u_7f3a2b9c1d", name="Emily Carter", kind="person")  # her space on a server
    edition.locked = CompanyFloor()  # a floor the apply reads, not yet heard by the runtime

    def refused(config):
        raise OSError("another instance wrote them since")

    monkeypatch.setattr(rt.settings, "save", refused)
    with pytest.raises(OSError, match="another instance wrote them since"):
        rt.apply_config(before.model_copy(update={"terms": [FALCON]}), save=True)
    assert rt.config is before and rt.in_use() is ran and rt.floor_used is floor
    assert rt.in_use().profile == "balanced" and rt.built.engine.applying == 0


def test_closing_the_runtime_stops_watching_the_floor(tmp_path):
    rt, edition = _runtime(tmp_path)
    rt.close()
    assert edition.watchers == []


def test_a_refusal_names_the_settings_and_only_what_goes_further_below_counts():
    assert refusal(["profile: from max to balanced", "terms: 1 value no longer masked"]) == (
        "Set by your company, not changed: profile: from max to balanced; terms: 1 value no longer masked"
    )
    assert fresh_reasons(None, Config(), Config(profile="secrets")) == []
    below = Config(profile="balanced", allow_values=["acme.com"])
    assert fresh_reasons(CompanyFloor(), below, below.model_copy(update={"history": "off"})) == []
    assert fresh_reasons(CompanyFloor(), Config(profile="max"), below) == [
        "profile: from max to balanced",
        "values never to mask: 1 added",
    ]


# --- what the engine is built from, and named by (F3, M4, M5) --------------------------------------------------------


class ChangesWhileBuilt(FloorEdition):
    """An edition whose floor changes while a runtime is being built: after the build read it, before its watch."""

    def floor(self, identity):
        floor = self.locked
        if self.locked is None:
            self.locked = CompanyFloor()
            for changed in list(self.watchers):  # nobody watches yet: the runtime being built is not told
                changed()
        return floor


def test_a_floor_that_changes_before_the_runtime_watches_it_is_applied_once_it_does(tmp_path):
    edition = ChangesWhileBuilt()
    config = Config(profile="balanced", identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    try:
        assert rt.config.profile == "balanced" and rt.in_use().profile == "max"
        assert rt.floor_used is edition.locked
    finally:
        rt.close()


class StrictFloor(CompanyFloor):
    """The company also sets the dump guard, the Live Reviewer, auto mode's reviewer and guidance for the local one."""

    def raise_to(self, config):
        raised = super().raise_to(config)
        guided = raised.llm_review.model_copy(update={"guidance": "Ward names are confidential."})
        update = {
            "dump_guard": 7,
            "ocr": "off",
            "live_review": raised.live_review.model_copy(update={"enabled": True}),
            "auto_mode_review": raised.auto_mode_review.model_copy(update={"user_name": "surrogate"}),
            "llm_review": guided,
        }
        return raised.model_copy(update=update)


def _what_the_mediator_runs(rt):
    m = rt.mediator
    return m.images, m.ocr, m.dump_guard, m.review, m.live_review, m.llm_review.guidance


def test_what_the_mediator_runs_is_the_settings_in_use(tmp_path):
    edition = FloorEdition(StrictFloor())
    config = Config(identity=IdentityConfig(enabled=False), ocr="auto")
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
    try:
        from latet.runtime import review_policy
        from latet.vision.policy import image_policy

        used = rt.in_use()
        wanted = (image_policy(used), False, 7, review_policy(used), used.live_review, "Ward names are confidential.")
        assert _what_the_mediator_runs(rt) == wanted  # at the start
        assert rt.built.engine.rules_tag == digest(used) != digest(rt.config)
        rt.apply_config(rt.config.model_copy(update={"history": "off"}), save=False)
        used = rt.in_use()
        assert _what_the_mediator_runs(rt) == (*wanted[:3], review_policy(used), used.live_review, wanted[5])
        assert rt.built.engine.rules_tag == digest(used) != digest(rt.config)
    finally:
        rt.close()


def test_a_new_floor_names_the_rules_anew_while_your_settings_stay(floored):
    rt, edition = floored()
    tag, config = rt.built.engine.rules_tag, rt.config
    edition.locked = TermsFloor(FALCON)
    edition.watchers[0]()
    assert rt.config is config and rt.built.engine.rules_tag != tag


@pytest.mark.parametrize("mine", [[], [FALCON]], ids=["a-term-added", "your-own-term-let-through"])
def test_a_text_sent_before_a_floor_locks_a_term_is_masked_with_it_after(floored, mine):
    """M5: a text a conversation sent before is sent again as it left while the rules are the same: a floor that locks
    a term changes them, even where your settings already held the term first (let through by a pattern of yours), so
    that the settings in use dump as yours do and only the lock tells them apart."""
    rt, edition = floored(terms=mine, allow_patterns=["Project Falcon"])
    text = "the Project Falcon launch"

    def sent() -> str:
        masked = rt.mediator.mask_value(text, SlotKind.USER_TEXT, rt.built.engine.session("cc:s1"), MaskReport())
        assert rt.mediator.verifier.verify([(masked.text, SlotKind.USER_TEXT, False)], "cc:s1").blocking == []
        return masked.text

    assert sent() == text  # in clear: your exception
    edition.locked = TermsFloor(FALCON)
    edition.watchers[0]()
    if mine:
        assert rt.in_use().model_dump() == rt.config.model_dump()  # only the lock differs
    assert "Falcon" not in sent()


def test_every_part_of_a_runtime_is_built_with_the_terms_the_company_locks(floored, tmp_path):
    shop = str(tmp_path / "shop")
    mine = [ProjectEntry(path=shop, terms=[TermConfig(value="Orion", type="PROJECT")])]
    rt, _ = floored(TermsFloor(FALCON), projects=mine, allow_values=["project falcon"])
    assert any(d.id == "locked-terms" for d in rt.built.masker.detectors)
    project = rt.projects.resolve(shop)
    assert project.scope is not None and any(d.id == "locked-terms" for d in project.scope.masker.detectors)
    assert "Falcon" not in project.scope.masker.mask("see project falcon").text
    assert rt.config.locked_terms == [] and load_config(rt.config_file).terms == []  # yours, never the company's


class LosesItsTerms(TermsFloor):
    """A floor whose raise drops the lock on the way (a copy dumped and read again): what no build may run on."""

    def raise_to(self, config):
        return Config.model_validate(super().raise_to(config).model_dump())


def test_settings_in_use_without_the_terms_the_company_locks_build_nothing(floored):
    with pytest.raises(MaskingError):
        floored(LosesItsTerms(FALCON), folder="start")
    rt, edition = floored(folder="later")
    version, masker = rt.built.engine.rules_version, rt.built.masker
    edition.locked = LosesItsTerms(FALCON)
    with pytest.raises(MaskingError):
        rt.apply_config(rt.config, save=False)
    assert rt.built.engine.rules_version == version and rt.built.masker is masker


def test_a_masker_is_weighed_with_the_terms_the_company_locks():
    locked = Config()
    locked.lock_terms([FALCON, TermConfig(value="Northwind Traders")])
    assert masker_weight(locked) - masker_weight(Config()) == masker_weight(Config(terms=[FALCON] * 2)) - masker_weight(
        Config()
    )


TICKETS = {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}", "examples": {"match": ["TCK-3819 is due"]}}
BADGE = {"name": "badge", "type": "BADGE", "pattern": "BD-[0-9]{6}"}


class RecognizerFloor(CompanyFloor):
    """The company adds a recognizer of its own, whose example (a value of its) the check of its examples quotes, and
    an address strict data mode covers (a client's host)."""

    def raise_to(self, config):
        raised = super().raise_to(config)
        hosts = [*raised.data_hosts, "crm.northwind.example"]
        return raised.model_copy(update={"recognizers": [TICKETS, *raised.recognizers], "data_hosts": hosts})


def test_a_person_sees_the_state_of_her_own_recognizers_never_the_companys(tmp_path):
    """What a person's pages show of the settings in use names nothing the company added that its view for a person
    counts or leaves out: a recognizer's state, whose words quote its examples; an address of its own."""
    shop = str(tmp_path / "shop")
    mine = Config(identity=IdentityConfig(enabled=False), ner="off", recognizers=[BADGE])
    mine.projects = [ProjectEntry(path=shop, terms=[TermConfig(value="Orion")])]
    rt = make_runtime(mine, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=FloorEdition())
    try:
        assert rt.edition.watchers  # type: ignore[attr-defined]
        rt.edition.locked = RecognizerFloor()  # type: ignore[attr-defined]
        rt.floor_changed()
        assert [s.name for s in rt.built.recognizers] == ["ticket", "badge"]  # both in use
        dashboard = rt.gateway.dashboard
        assert dashboard is not None
        view = dashboard._config_view(rt.config)
        assert [s["name"] for s in view["recognizer_status"]] == ["badge"] and "TCK-" not in str(view)
        assert rt.projects is not None
        project = dashboard._project_view(rt.projects.resolve(shop), detail=True)
        assert [s["name"] for s in project["recognizer_status"]] == ["badge"] and "TCK-" not in str(project)
        assert project["effective"]["profile"] == "max" and "northwind" not in str(project)
    finally:
        rt.close()


# --- the paths that weigh what arrives: a refusal never breaks them (L5, F8) -----------------------------------------


def _refusing(monkeypatch):
    """``propose`` refusing whatever it weighs, as a floor would below it."""

    def refused(self, *args, **kwargs):
        raise BelowFloor(["profile: from max to secrets"])

    monkeypatch.setattr(Runtime, "propose", refused)


def test_a_start_whose_change_is_refused_runs_what_was_approved(tmp_path, monkeypatch, caplog):
    rt, edition = _runtime(tmp_path)
    rt.close()
    save_config(Config(profile="secrets", identity=IdentityConfig(enabled=False)), rt.config_file)
    _refusing(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="latet.runtime"):
        again = make_runtime(data=rt.data, config_file=rt.config_file, edition=edition)
    try:
        assert again.config.profile == "max" and again.mediator.approvals.changes == {}
        assert "BelowFloor" in caplog.text and "secrets" not in caplog.text
    finally:
        again.close()


def test_a_repository_held_back_whose_change_is_refused_runs_what_was_approved(made, monkeypatch):
    rt, _ = made()
    approved = rt.config
    rt.apply_config(rt.config.model_copy(update={"history": "off"}), save=False)
    assert rt.projects is not None
    rt.projects.before, rt.start_scan = approved, RepositoryScan()
    _refusing(monkeypatch)
    rt._held_back()
    assert rt.config == approved and rt.mediator.approvals.changes == {}


def test_a_held_back_repository_runs_on_what_was_approved_raised(made, tmp_path):
    """A repository seen since the start, worked out under the settings you approved before it (``Projects.before``):
    raised to the floor there too, never below it."""
    rt, _ = made(profile="balanced")
    assert rt.projects is not None
    approved = rt.config.model_copy(update={"allow_values": ["acme.com"]})  # below the floor: none of yours counts
    root = tmp_path / "strict"
    (root / ".git").mkdir(parents=True)
    (root / "latet.yaml").write_bytes(b"profile: max\n")
    # Settings that lower the repository's own (private addresses off there) against those you approved before
    rt.apply_config(rt.config.model_copy(update={"include_private_ips": False}), save=False)
    rt.projects.before, rt.start_scan = approved, RepositoryScan()  # as at a start over them, every repository read
    project = rt.projects.resolve(str(root))
    assert project.config.allow_values == [] and project.config.include_private_ips is None  # approved, raised
    [change] = rt.mediator.approvals.waiting_changes()  # and what the start found waits for you
    assert change.source == "start"
