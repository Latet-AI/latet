"""The settings a person's space runs on a server while a change of hers waits (``shared.composed``): those of now, but
where they lower protection against her approved snapshot, where the snapshot's apply; protective lists united, lists
that leave values in clear met, the server's own keys from the shared settings of now. Never looser than the snapshot,
always holding what the shared settings add."""

import logging
import time
from pathlib import Path
from typing import Any

from latet.config import Config, Destination, LlmReview, Policy, Profile, ProjectEntry, TermConfig
from latet.protection import loosened
from latet.server.shared import composed, repinned

OLD = LlmReview(enabled=True, url="https://gpu-old.acme.lan/v1", remote=True, model="qwen")
NEW = LlmReview(enabled=True, url="https://gpu-new.acme.lan/v1", remote=True, model="qwen")


def _terms(config):
    return sorted(t.value for t in config.terms)


def test_settings_that_lower_nothing_are_the_settings_of_now():
    approved = Config(terms=[TermConfig(value="Project Atlas")])
    found = Config(terms=[TermConfig(value="Project Atlas"), TermConfig(value="Mercury Labs")], retention_days=3)
    assert composed(found, approved, Config()) is found


def test_a_protective_list_holds_both():
    approved = Config(terms=[TermConfig(value="Project Atlas")])
    found = Config(terms=[TermConfig(value="Project Falcon")])  # the administrator replaced Atlas with Falcon
    out = composed(found, approved, Config(terms=[TermConfig(value="Project Falcon")]))
    assert _terms(out) == ["Project Atlas", "Project Falcon"]  # the removal waits, the addition applies


def test_a_list_that_leaves_values_in_clear_holds_what_both_leave():
    approved = Config(allow_values=["support@acme.com"])
    found = Config(allow_values=["support@acme.com", "emily.carter@acme.com"], include_private_ips=True)
    out = composed(found, approved, Config(allow_values=["emily.carter@acme.com"], include_private_ips=True))
    assert out.allow_values == ["support@acme.com"]  # one more value never masked waits
    assert out.include_private_ips is True  # what else was tightened with it applies
    found = Config(allow_patterns=["build-[0-9]+"], disabled_secret_rules=["generic-api-key"])
    out = composed(found, Config(), Config())
    assert out.allow_patterns == [] and out.disabled_secret_rules == []


def test_a_setting_tightened_applies_and_one_loosened_waits():
    approved = Config(profile=Profile.BALANCED, dump_guard=50)
    found = Config(profile=Profile.SECRETS, dump_guard=50, include_private_ips=True, ner="off")
    out = composed(found, approved, Config(include_private_ips=True))
    assert out.profile == Profile.BALANCED and out.ner == approved.ner  # lowered: the snapshot's
    assert out.include_private_ips is True  # what the administrator tightened, at once


def test_a_policy_takes_the_stricter_of_the_two_by_type():
    approved = Config(policies={"EMAIL": Policy.MASK, "PHONE": Policy.REDACT})
    found = Config(policies={"EMAIL": Policy.ALLOW, "PHONE": Policy.MASK, "IBAN": Policy.REDACT})
    out = composed(found, approved, Config())
    assert out.policies == {"EMAIL": Policy.MASK, "PHONE": Policy.REDACT, "IBAN": Policy.REDACT}


def test_the_reviewer_runs_where_the_server_says_and_its_own_settings_are_weighed():
    shared = Config(llm_review=NEW)
    moved = Config(llm_review=NEW)  # the administrator moved the shared reviewer
    assert composed(moved, Config(llm_review=OLD), shared) is moved  # where it runs is the server's, never hers
    assert repinned(Config(llm_review=OLD), shared).llm_review.url == NEW.url
    approved = Config(llm_review=OLD.model_copy(update={"guidance": "Report every customer name."}))
    out = composed(moved, approved, shared)  # and her guidance dropped meanwhile
    assert out.llm_review.url == NEW.url
    assert out.llm_review.guidance == "Report every customer name."  # hers: a guidance changed waits for her


def test_a_project_and_a_destination_keep_what_the_snapshot_gives_them():
    folder = "/home/emily/oss-site"
    approved = Config(
        projects=[ProjectEntry(path=folder, terms=[TermConfig(value="Orion")])],
        upstreams=[Destination(name="gw", url="https://gw.acme.com")],
    )
    found = Config(
        projects=[ProjectEntry(path=folder, enabled=False), ProjectEntry(path="/home/emily/new")],
        upstreams=[
            Destination(name="gw", url="https://gw-moved.acme.com"),
            Destination(name="eu", url="https://eu.acme.com"),
        ],
    )
    out = composed(found, approved, Config())
    paths = {e.path: e for e in out.projects}
    assert paths[folder].enabled and [t.value for t in paths[folder].terms] == ["Orion"]  # Latet off there waits
    assert "/home/emily/new" in paths  # a project of hers that lowers nothing applies
    assert {d.name: d.url for d in out.upstreams} == {"gw": "https://gw.acme.com", "eu": "https://eu.acme.com"}


def test_a_composition_that_would_lower_protection_falls_back_to_the_snapshot(monkeypatch, caplog):
    from latet.server import shared as shared_module

    monkeypatch.setattr(shared_module, "_composed_key", lambda key, found, base, now, then: now)  # one composed wrongly
    approved = Config(allow_values=["support@acme.com"])
    found = Config(allow_values=["support@acme.com", "emily.carter@acme.com"], include_private_ips=True)
    with caplog.at_level(logging.WARNING):
        out = composed(found, approved, Config())
    assert out.allow_values == ["support@acme.com"] and out.include_private_ips is None  # the snapshot, whole
    assert "the approved ones apply" in caplog.text and "emily" not in caplog.text


def test_a_composition_that_falls_back_is_said_once_a_minute(monkeypatch, caplog):
    """C-21: every re-application of every space holding such settings falls back: the log says it once a minute."""
    from latet.logline import Paced
    from latet.server import shared as shared_module

    monkeypatch.setattr(shared_module, "_UNCOMPOSED", Paced())
    monkeypatch.setattr(shared_module, "_composed_key", lambda key, found, base, now, then: now)  # one composed wrongly
    approved = Config(allow_values=["support@acme.com"])
    found = Config(allow_values=["support@acme.com", "emily.carter@acme.com"], include_private_ips=True)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert composed(found, approved, Config()).allow_values == ["support@acme.com"]
    assert sum("the approved ones apply" in r.getMessage() for r in caplog.records) == 1


def _inside(config: Config, entry: ProjectEntry) -> Config:
    from latet.projects import effective_config

    return effective_config(config, None, entry)[0]


def _types(config: Config) -> set[str]:
    from latet.engine.detectors.pii import effective_pii

    if config.profile == Profile.SECRETS:
        return set()
    return set(effective_pii(config.pii, config.id_regions, every_region=config.profile == Profile.MAX))


def test_a_composition_is_never_looser_than_the_snapshot_inside_a_stricter_folder():
    from latet.engine.detectors.pii import effective_pii

    payroll = ProjectEntry(path="/home/emily/payroll", profile=Profile.MAX)
    approved = Config(projects=[payroll])  # balanced at the top, max in her payroll folder
    balanced = effective_pii(None, Config().id_regions, every_region=False)
    for found in (
        Config(pii=["PHONE"], projects=[payroll]),  # her row drops a type at the top: a change waits
        Config(pii=balanced, projects=[payroll]),  # the balanced types written out: nothing changes at the top
        Config(include_private_ips=False, data="normal", projects=[payroll]),  # the same for these two
    ):
        out = composed(found, approved, Config())
        assert loosened(approved, out) == [], found
        inside, kept = _inside(out, payroll), _inside(approved, payroll)
        assert _types(inside) >= _types(kept)  # every country's ID numbers, as max has them there
        assert inside.include_private_ips in (None, True) and inside.data_mode() == "strict"


def test_what_cannot_be_composed_in_a_stricter_folder_waits_alone_and_what_tightens_applies():
    payroll = ProjectEntry(path="/home/emily/payroll", profile=Profile.MAX)
    approved = Config(projects=[payroll])
    # Her types written out: the snapshot's "not set" stays, and the private addresses tightened at the top apply.
    out = composed(Config(pii=["PHONE"], include_private_ips=True, projects=[payroll]), approved, Config())
    assert out.pii is None and out.include_private_ips is True
    # Strict data mode turned off at the top and in her folder's own entry: both wait, the tightening applies.
    normal = payroll.model_copy(update={"data": "normal"})
    out = composed(Config(data="normal", include_private_ips=True, projects=[normal]), approved, Config())
    assert out.data is None and out.projects == [payroll] and out.include_private_ips is True
    assert loosened(approved, out) == []


def test_a_composition_is_never_looser_than_the_snapshot():
    approved = Config(
        terms=[TermConfig(value="Project Atlas")], allow_values=["a@acme.com"], dump_guard=20, profile=Profile.MAX,
        llm_review=OLD,
    )  # fmt: skip
    shared = Config(llm_review=NEW, terms=[TermConfig(value="Project Falcon")])
    for found in (
        Config(profile=Profile.SECRETS, include_private_ips=False, llm_review=NEW),
        Config(terms=[TermConfig(value="Project Falcon")], allow_values=["a@acme.com", "b@acme.com"], llm_review=NEW),
        Config(dump_guard=0, images="allow", ocr="off", data="normal", ner="off", llm_review=NEW),
    ):
        out = composed(found, approved, shared)
        assert loosened(repinned(approved, shared), out) == []


# --- at the 200 projects a server keeps for one identity -----------------------------------------------------------

CUSTOMERS = [TermConfig(value=f"Customer Company {i:04d}", type="ORG") for i in range(500)]


def _at_the_cap(**changed: Any) -> Config:
    """Settings naming 200 folders, each with a term of its own (one lets a value through, one has Latet off), under
    500 shared terms; ``changed``: folders replaced by their index (None: gone), or other settings."""
    entries: list[ProjectEntry | None] = [
        ProjectEntry(path=f"/home/emily/client-{i:05d}", terms=[TermConfig(value=f"Client Name {i:05d}")])
        for i in range(200)
    ]
    entries[3] = ProjectEntry(path="/home/emily/client-00003", allow_values=["ops@acme.com"])
    entries[7] = ProjectEntry(path="/home/emily/client-00007", enabled=False)
    for key in [k for k in changed if k.startswith("folder_")]:
        entries[int(key.removeprefix("folder_"))] = changed.pop(key)
    return Config(terms=CUSTOMERS, projects=[e for e in entries if e is not None], **changed)


def _projects_in_full(old: Config, new: Config) -> list[str]:
    """What the projects lower as the check weighed them before it skipped any: every folder, changed or not."""
    from latet.projects import effective_config, norm_path
    from latet.protection import lowered_in_project

    out: list[str] = []
    before = {norm_path(e.path): e for e in old.projects}
    after = {norm_path(e.path): e for e in new.projects}
    for path in sorted({*before, *after}):
        was, now = before.get(path), after.get(path)
        name = Path(path.rstrip("/\\")).name or path
        if (was is None or was.enabled) and now is not None and not now.enabled:
            out.append(f"project {name}: Latet turned off")
            continue
        old_here, _ = effective_config(new, None, was)
        new_here, _ = effective_config(new, None, now)
        out += [f"project {name}: {reason}" for reason in lowered_in_project(old_here, new_here)]
    return out


def test_a_check_at_the_projects_cap_weighs_the_folders_that_changed_and_answers_as_in_full():
    from latet import protection

    base = _at_the_cap()
    added = ProjectEntry(path="/home/emily/new-site", allow_values=["press@acme.com"])
    for name, new in {
        "nothing": _at_the_cap(),
        "a folder turned off": _at_the_cap(folder_10=ProjectEntry(path="/home/emily/client-00010", enabled=False)),
        "a folder's term dropped": _at_the_cap(folder_11=ProjectEntry(path="/home/emily/client-00011")),
        "a value let through": _at_the_cap(
            folder_12=ProjectEntry(path="/home/emily/client-00012", allow_values=["billing@acme.com"])
        ),
        "a folder gone": _at_the_cap(folder_13=None),
        "a folder added": _at_the_cap().model_copy(update={"projects": [*base.projects, added]}),
        "Latet on again": _at_the_cap(folder_7=ProjectEntry(path="/home/emily/client-00007")),
        "a shared term dropped": _at_the_cap().model_copy(update={"terms": CUSTOMERS[1:]}),
    }.items():
        started = time.perf_counter()
        reasons = loosened(base, new)
        took = time.perf_counter() - started
        assert protection._projects(base, new) == _projects_in_full(base, new), name
        assert all(r in reasons for r in _projects_in_full(base, new)), name
        assert took < 0.2, f"{name}: {took:.2f} s"  # 3.4 s when every folder was weighed


def test_a_composition_under_thirty_thousand_terms_with_a_change_waiting_takes_a_fraction_of_a_second():
    # Each list was compared item by item against the other: 17.6 s at 30,000 terms, at each re-application of a space.
    # In proportion to the terms now: about 0.1 s alone, two or three times that in a whole run of the tests.
    many = [TermConfig(value=f"Customer Company {i:05d}", type="ORG") for i in range(30_000)]
    shared = Config(terms=many[1:])  # the administrator dropped a shared term: a change waits for her
    approved = Config(terms=many)
    found = Config(terms=[*many[1:], TermConfig(value="Mercury Labs")])  # and she added one of her own
    took = []
    for _ in range(3):  # the shortest: what it costs, Python's collection of the test run's memory aside
        started = time.perf_counter()
        out = composed(found, approved, shared)
        took.append(time.perf_counter() - started)
    assert len(out.terms) == 30_001 and {"Customer Company 00000", "Mercury Labs"} <= set(_terms(out))
    assert min(took) < 1.0, f"{min(took):.2f} s"


def test_a_composition_at_the_projects_cap_takes_milliseconds():
    shared = Config(terms=CUSTOMERS[1:])  # the administrator dropped a shared term: a change waits for her
    approved = _at_the_cap()
    found = _at_the_cap().model_copy(update={"terms": [*CUSTOMERS[1:], TermConfig(value="Mercury Labs")]})
    started = time.perf_counter()
    out = composed(found, approved, shared)
    took = time.perf_counter() - started
    assert {"Customer Company 0000", "Mercury Labs"} <= set(_terms(out))  # the removal waits, her term applies
    assert len(out.projects) == 200
    assert took < 0.5, f"{took:.2f} s"  # 10 s when every folder was weighed at each step
