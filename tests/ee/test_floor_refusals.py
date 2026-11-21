"""What a runtime refuses below a company's policy (Latet Enterprise's floor): only what the change itself lowers. A
change that comes closer to the floor, or makes the settings stricter, goes through however far below it they were;
one that lowers a setting the policy locks is refused, naming it."""

import time

import pytest
import yaml

from latet import floor as floor_module
from latet.config import Config, FieldRule, IdentityConfig, Profile, ProjectEntry, TermConfig, save_config
from latet.ee.policy import parse_policy
from latet.floor import TOO_MANY_FOLDERS, BelowFloor, fresh_reasons
from latet.runtime import make_runtime

from ..floors import FloorEdition


@pytest.fixture
def under(tmp_path):
    runtimes = []

    def make(policy: str, **settings):
        config = Config(identity=IdentityConfig(enabled=False), ner="off", **settings)
        save_config(config, tmp_path / "config.yaml")
        edition = FloorEdition(parse_policy(f"latet_rules: 1\n{policy}"))
        rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=edition)
        runtimes.append(rt)
        return rt

    yield make
    for rt in runtimes:
        rt.close()


def test_a_profile_brought_closer_to_the_policys_goes_through(under):
    rt = under("profile: max\n", profile="secrets")
    rt.update_config(lambda current: current.model_copy(update={"profile": Profile.BALANCED}))
    assert rt.config.profile == "balanced" and rt.in_use().profile == "max"


def test_a_profile_taken_further_below_the_policys_is_refused(under):
    rt = under("profile: max\n", profile="balanced")
    with pytest.raises(BelowFloor) as refused:
        rt.update_config(lambda current: current.model_copy(update={"profile": Profile.SECRETS}))
    assert "profile: from max to secrets" in refused.value.reasons and rt.config.profile == "balanced"


def test_one_exception_less_goes_through_when_none_of_yours_counts(under):
    rt = under("profile: max\nexceptions: company\n", profile="max", allow_values=["acme.com", "globex.com"])
    rt.update_config(lambda current: current.model_copy(update={"allow_values": ["acme.com"]}))
    assert rt.config.allow_values == ["acme.com"] and rt.in_use().allow_values == []
    with pytest.raises(BelowFloor) as refused:  # one more: the exceptions are lowered, and they are the company's
        rt.update_config(lambda current: current.model_copy(update={"allow_values": ["acme.com", "initech.com"]}))
    assert refused.value.reasons == ["values never to mask: 2 added"]


TICKET = {"name": "ticket", "type": "TICKET", "pattern": "TCK-[0-9]{6}"}
BADGE = {"name": "badge", "type": "BADGE", "pattern": "BD-[0-9]{6}"}
POLICIES = {
    "a-term": "terms:\n  - {value: Northwind Traders, type: CLIENT}\n",
    "a-recognizer": "recognizers:\n  - {name: ticket, type: TICKET, pattern: 'TCK-[0-9]{6}'}\n",
    "a-field-rule": "fields:\n  - {name: iban, type: IBAN}\n",
    "strict-data-mode": "data: strict\n",
}
YOURS = {
    "a-term": {"terms": [TermConfig(value="Bar Ltd", type="CLIENT")]},
    "a-recognizer": {"recognizers": [BADGE]},
    "a-field-rule": {"fields": [FieldRule(name="notes")]},
    "strict-data-mode": {"data_files": ["*.csv"]},
}


@pytest.mark.parametrize("locked", sorted(POLICIES))
def test_an_item_of_your_own_taken_out_goes_through_where_the_policys_reason_stays_as_it_was(under, locked):
    """The settings are below the policy for its own items (not among yours): taking one of yours out lowers that
    setting, yet leaves them exactly as far below it as they were."""
    rt = under(POLICIES[locked], **YOURS[locked])
    assert rt.floor() is not None and rt.floor().below(rt.config) != []  # below it, since before
    emptied = {key: [] for key in YOURS[locked]}
    rt.update_config(lambda current: current.model_copy(update=emptied))
    assert all(getattr(rt.config, key) == [] for key in emptied)


def _folders(count, **entry):
    return [ProjectEntry(path=f"C:/work/p{n}", **entry) for n in range(count)]


@pytest.mark.timing
def test_a_change_that_lowers_more_folders_than_are_weighed_is_refused_at_once():
    """M-B: each folder a change lowers is weighed with the settings raised twice: 200 of them under a policy of 500
    field rules held a person's worker for minutes. Past ``FOLDERS_WEIGHED``, refused in fixed words, unweighed."""
    fields = [{"name": f"*field_{n}*", "type": f"F_{n}"} for n in range(500)]
    floor = parse_policy(yaml.safe_dump({"fields": fields, "policies": {"EMAIL": "redact"}}))
    before, after = Config(projects=_folders(200)), Config(projects=_folders(200, policies={"EMAIL": "allow"}))
    started = time.perf_counter()
    assert fresh_reasons(floor, before, after) == [TOO_MANY_FOLDERS]
    assert (took := time.perf_counter() - started) < 5.0, took


def test_a_change_of_as_many_folders_as_are_weighed_is_weighed_folder_by_folder(monkeypatch):
    monkeypatch.setattr(floor_module, "FOLDERS_WEIGHED", 2)
    floor = parse_policy("policies: {EMAIL: redact}\n")
    before = Config(projects=_folders(3))
    two = Config(projects=[*_folders(2, policies={"EMAIL": "allow"}), *_folders(3)[2:]])
    assert fresh_reasons(floor, before, two) == [
        "project p0: policy for EMAIL: from redact to allow",
        "project p1: policy for EMAIL: from redact to allow",
    ]
    assert fresh_reasons(floor, before, Config(projects=_folders(3, policies={"EMAIL": "allow"}))) == [TOO_MANY_FOLDERS]
    more = Config(
        projects=_folders(3, ner="auto")
    )  # three folders changed, none lowered: nothing to weigh, nothing said
    assert fresh_reasons(floor, before, more) == []


def test_one_more_type_of_personal_data_goes_through_below_the_policys_profile(under):
    rt = under("profile: max\n", profile="balanced", pii=["EMAIL", "PHONE"])
    rt.update_config(lambda current: current.model_copy(update={"pii": ["EMAIL", "PHONE", "ES_DNI"]}))
    assert rt.config.pii == ["EMAIL", "PHONE", "ES_DNI"]
