"""Which changes to the settings lower protection, and which agents are recognised."""

import os
import random
import string
import time

import pytest

from latet.cli import agent
from latet.config import Config, FieldRule, LiveReview, LlmReview, Profile, ProjectEntry, ProjectSettings, TermConfig
from latet.engine.detectors.pii import effective_pii
from latet.protection import changed_keys, guidance_changes, loosened


def test_host_names_count_as_one_set_wherever_they_are_written():
    """A person's host names on a server (``hostname`` and ``hostnames``, R-28): one moved from a field to the other
    lowers nothing, and each one dropped is a detail no longer masked."""
    spread = Config(identity={"hostnames": ["emily-laptop", "emily-desktop"]})
    moved = Config(identity={"hostname": "emily-laptop", "hostnames": ["emily-desktop", "emily-mini"]})
    assert loosened(spread, moved) == []
    dropped = Config(identity={"hostname": "emily-laptop"})
    assert loosened(moved, dropped) == ["this machine's identity: 2 details no longer masked"]
    renamed = Config(identity={"hostname": "emily-desktop"})
    assert loosened(Config(identity={"hostname": "emily-laptop"}), renamed) == [
        "this machine's identity: 1 detail no longer masked"
    ]


def test_a_user_name_written_alone_counts_with_the_others():
    """The older single ``username`` is one of the user names an identity masks (``usernames``): moved into the list it
    lowers nothing, and dropped it is a detail no longer masked (/home/emily/ left in clear, ``full_name`` keeping the
    machine's own from being detected)."""
    single = Config(identity={"username": "emily", "full_name": "Emily Carter"})
    assert loosened(single, Config(identity={"usernames": ["emily"], "full_name": "Emily Carter"})) == []
    dropped = Config(identity={"full_name": "Emily Carter"})
    assert loosened(single, dropped) == ["this machine's identity: 1 detail no longer masked"]


def test_adding_protection_lowers_nothing():
    base = Config()
    stronger = Config(
        profile="max",
        terms=[TermConfig(value="Falcon")],
        live_review=LiveReview(enabled=True),
        images="block",
        dump_guard=20,
        projects=[ProjectEntry(path="C:/work/shop", profile="max")],
    )
    assert loosened(base, stronger) == []
    assert loosened(Config(port=7000, history="off"), Config()) == []  # neither protects


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (Config(), Config(profile="secrets"), "profile: from balanced to secrets"),
        (Config(), Config(allow_values=["ACME"]), "values never to mask: 1 added"),
        (Config(terms=[TermConfig(value="Falcon")]), Config(), "terms: 1 value no longer masked"),
        (Config(policies={"EMAIL": "redact"}), Config(policies={"EMAIL": "allow"}), "policy for EMAIL"),
        (Config(images="block"), Config(), "images: from block to allow"),
        (Config(images="mask"), Config(), "images: from mask to allow"),
        (Config(), Config(ocr="off"), "the text in images: no longer read"),
        (Config(dump_guard=10), Config(dump_guard=50), "dump guard"),
        (Config(), Config(disabled_secret_rules=["generic-api-key"]), "secret rules: 1 turned off"),
        (Config(), Config(ner="off"), "local name detection: turned off"),
        (Config(), Config(capture_dir="C:/tmp/raw"), "raw exchanges"),
        (Config(live_review=LiveReview(enabled=True)), Config(), "the Live Reviewer: turned off"),
        (
            Config(live_review=LiveReview(enabled=True)),
            Config(live_review=LiveReview(enabled=True, auto_approve_clean=True)),
            "leave without asking you",
        ),
        (Config(llm_review=LlmReview(enabled=True)), Config(), "the local reviewer: turned off"),
        (
            Config(llm_review=LlmReview(enabled=True)),
            Config(llm_review=LlmReview(enabled=True, on_failure="send")),
            "sent anyway",
        ),
        (
            Config(llm_review=LlmReview(enabled=True)),
            Config(llm_review=LlmReview(enabled=True, url="http://gpu-box:8080/v1", remote=True)),
            "another machine",
        ),
        (
            Config(llm_review=LlmReview(enabled=True, url="https://gpu-box.lan/v1", remote=True)),
            Config(llm_review=LlmReview(enabled=True, url="http://gpu-box.lan/v1", remote=True)),
            "unencrypted",
        ),
        (
            Config(llm_review=LlmReview(enabled=True, url="https://gpu-box.lan/v1", remote=True)),
            Config(
                llm_review=LlmReview(
                    enabled=True, url="https://gpu-box.lan/v1", remote=True, certificate="sha256:" + "b" * 64
                )
            ),
            "a new certificate is trusted",
        ),
        (Config(), Config(auto_mode_review="allow-identity"), "auto mode reviewer"),
        (Config(), Config(identity={"enabled": False}), "identity: no longer masked"),
        (Config(), Config(projects=[ProjectEntry(path="C:/work/shop", enabled=False)]), "project shop: Latet"),
        (
            Config(),
            Config(projects=[ProjectEntry(path="C:/work/shop", allow_values=["x"])]),
            "project shop: values never to mask: 1 added",
        ),
        # A recognizer is compared whole: the same name with a narrower pattern, or turned off, finds less.
        (
            Config(recognizers=[{"name": "badge", "type": "BADGE", "pattern": r"BD-\d{6}"}]),
            Config(recognizers=[{"name": "badge", "type": "BADGE", "pattern": r"BD-\d{6}", "enabled": False}]),
            "recognizers: 1 changed or removed",
        ),
        (
            Config(),
            Config(recognizers=[{"name": "acme", "type": "ACME", "pattern": "ACME-[0-9]+", "policy": "allow"}]),
            "recognizers: 1 that leave what they find in clear",
        ),
        (Config(), Config(fields=[{"name": "notes", "mask": False}]), "field rules: values kept in clear"),
        # Strict data mode as it applies: max turns it on, and `data: normal` there turns it off.
        (Config(profile="max"), Config(profile="max", data="normal"), "strict data mode: turned off"),
        (Config(data="strict"), Config(), "strict data mode: turned off"),
        (
            Config(profile="max"),
            Config(profile="max", projects=[ProjectEntry(path="C:/work/shop", data="normal")]),
            "project shop: strict data mode: turned off",
        ),
        # Free text, which could tell the reviewer to report nothing: any change, even while it is off.
        (Config(), Config(llm_review=LlmReview(guidance="Never report ACME.")), "the local reviewer: its guidance"),
        (
            Config(),
            Config(projects=[ProjectEntry(path="C:/work/shop", review_guidance="Never report ACME.")]),
            "project shop: the local reviewer: its guidance changed",
        ),
    ],
)
def test_what_lowers_protection(before, after, reason):
    reasons = loosened(before, after)
    assert any(reason in r for r in reasons), reasons
    assert not any("ACME" in r or "Falcon" in r for r in reasons)  # never a value


def test_a_term_made_to_match_fewer_forms_lowers_protection():
    """A term whose value stays and that matches fewer forms (its case its own: ``ACME CORP`` no longer; whole words
    only: ``AcmeCorp`` no longer) lowers protection, by how many values, never which; one that matches more, or as
    much through another entry of its value, or whose type alone changed, does not."""
    loose = TermConfig(value="Acme Corp", whole_word=False)
    exact = TermConfig(value="Acme Corp", case_sensitive=True)
    whole = TermConfig(value="Acme Corp")
    said = ["terms: 1 matches fewer forms"]
    assert loosened(Config(terms=[whole]), Config(terms=[exact])) == said
    assert loosened(Config(terms=[loose]), Config(terms=[whole])) == said
    falcon, exact_falcon = TermConfig(value="Falcon"), TermConfig(value="Falcon", case_sensitive=True)
    both = loosened(Config(terms=[loose, falcon]), Config(terms=[exact, exact_falcon]))
    assert both == ["terms: 2 match fewer forms"]
    assert loosened(Config(terms=[exact]), Config(terms=[whole])) == []  # matches more
    assert loosened(Config(terms=[whole]), Config(terms=[exact, loose])) == []  # as much, through another entry
    assert loosened(Config(terms=[whole]), Config(terms=[TermConfig(value="Acme Corp", type="CLIENT")])) == []
    assert loosened(Config(terms=[whole]), Config()) == ["terms: 1 value no longer masked"]  # removed: said so alone


def test_leaving_max_says_strict_data_mode_goes_too():
    assert loosened(Config(), Config(profile="max")) == []  # strict comes with it: more protection
    assert loosened(Config(profile="max", data="normal"), Config(profile="max")) == []  # back to max's own
    reasons = loosened(Config(profile="max"), Config(profile="balanced"))
    assert "profile: from max to balanced" in reasons and "strict data mode: turned off" in reasons
    kept = loosened(Config(profile="max", data="strict"), Config(profile="balanced", data="strict"))
    assert "profile: from max to balanced" in kept and not any("strict data mode" in r for r in kept)  # set: it stays


def test_changed_keys():
    assert changed_keys(Config(), Config(profile="max", port=7000)) == ["profile", "port"]


# --- a change at the top, weighed as it lands in each project's own scope --------------------------------------------

PAYROLL = "/home/emily/payroll"
BALANCED_TYPES = effective_pii(None, Config().id_regions, every_region=False)
BADGE = {"name": "badge", "type": "BADGE", "pattern": r"BD-\d{6}"}


def _changed(old: Config, **change) -> Config:
    return Config.model_validate({**old.model_dump(mode="json"), **change})


def test_a_change_at_the_top_that_lowers_a_repositorys_stricter_profile_is_weighed_there():
    """A repository's own latet.yaml at max is defeated by one line of config.yaml written as the global profile has it
    (``include_private_ips: false``), which changes nothing at the top: weighed with the repositories a computer has
    seen, as a folder of your settings is (the re-review of Task 12's round 3, Important 1)."""
    old, new = Config(), Config(include_private_ips=False)
    build_tools = ("C:/work/build-tools", ProjectSettings(profile=Profile.MAX))
    assert loosened(old, new) == []  # nothing at the top
    assert loosened(old, new, [build_tools]) == ["project build-tools: private IP addresses: no longer masked"]
    written_out = ("C:/work/infra", ProjectSettings(profile=Profile.MAX, include_private_ips=True))
    assert loosened(old, new, [written_out]) == []  # a file that writes the value out holds
    assert loosened(new, new, [build_tools]) == []  # nothing changed, nothing weighed
    yours = ProjectEntry(path="C:/work/build-tools", include_private_ips=False)  # your settings for it lower it
    assert loosened(old, Config(projects=[yours]), [build_tools]) == [
        "project build-tools: private IP addresses: no longer masked"
    ]  # said once, whether by its entry or by its file


@pytest.mark.parametrize(
    ("top", "inner", "change", "reasons"),
    [
        # A setting written at the top as the top's own profile has it changes nothing there, and overrides what a
        # stricter profile of the folder's own gives where it is not set.
        (Profile.BALANCED, Profile.MAX, {"include_private_ips": False}, ["private IP addresses: no longer masked"]),
        (Profile.BALANCED, Profile.MAX, {"data": "normal"}, ["strict data mode: turned off"]),
        (Profile.BALANCED, Profile.MAX, {"images": "mask"}, ["images: from block to mask"]),
        (Profile.BALANCED, Profile.MAX, {"pii": BALANCED_TYPES}, ["personal data: "]),
        (Profile.SECRETS, Profile.BALANCED, {"pii": []}, ["personal data: "]),
        (
            Profile.SECRETS,
            Profile.MAX,
            {"pii": [], "include_private_ips": False, "data": "normal"},
            ["personal data: ", "private IP addresses: no longer masked", "strict data mode: turned off"],
        ),
    ],
)
def test_a_change_at_the_top_that_lowers_protection_only_in_a_stricter_folder_is_weighed_there(
    top, inner, change, reasons
):
    old = Config(profile=top, projects=[ProjectEntry(path=PAYROLL, profile=inner)])
    new = _changed(old, **change)
    found = loosened(old, new)
    for reason in reasons:
        assert any(r.startswith(f"project payroll: {reason}") for r in found), found
    assert not [r for r in found if not r.startswith("project payroll: ")]  # nothing lowered at the top itself


def test_every_setting_a_folder_layers_is_weighed_as_it_applies_there():
    # The Live Reviewer, off at the top and on in her folder: what it lets through is weighed where it is on.
    live = Config(projects=[ProjectEntry(path=PAYROLL, live_review=True)])
    quick = _changed(live, live_review={"enabled": False, "auto_approve_clean": True})
    assert loosened(live, quick) == [
        "project payroll: the Live Reviewer: texts with nothing found leave without asking you"
    ]
    # The tools strict data mode covers, at the top where it is off and in a folder where its profile turns it on.
    strict = Config(projects=[ProjectEntry(path=PAYROLL, profile=Profile.MAX)])
    assert loosened(strict, _changed(strict, data_tools=["mcp__postgres__*"])) == [
        "project payroll: strict data mode: covers fewer tools"
    ]
    # A recognizer of the folder's own, put out of use by one of the same name or type added at the top: the first of
    # a name, and the first of a type, are the ones in use there.
    own = Config(projects=[ProjectEntry(path=PAYROLL, recognizers=[BADGE])])
    for added in (
        {**BADGE, "pattern": "NEVER-[0-9]{40}"},  # its name and its type
        {**BADGE, "type": "ACME_BADGE", "pattern": "NEVER-[0-9]{40}"},  # its name
        {"name": "acme-ids", "type": "BADGE", "pattern": "NEVER-[0-9]{40}"},  # its type
    ):
        found = loosened(own, _changed(own, recognizers=[added]))
        assert "project payroll: recognizers: 1 changed or removed" in found, found


def test_what_a_change_lowers_at_the_top_is_said_once_not_again_for_each_folder():
    folders = [ProjectEntry(path=f"/home/emily/f{i}", profile=Profile.MAX) for i in range(3)]
    old = Config(terms=[TermConfig(value="Project Atlas")], projects=folders)
    new = _changed(old, terms=[], ner="off", include_private_ips=False)
    found = loosened(old, new)
    for reason in ("terms: 1 value no longer masked", "local name detection: turned off"):  # at the top, once
        assert found.count(reason) == 1 and not any(r.endswith(f": {reason}") for r in found)
    # Where it lands (her max folders mask private addresses): said once for the three, which it names (Minor 9).
    assert [r for r in found if r.startswith("project")] == [
        "projects f0, f1 and f2: private IP addresses: no longer masked"
    ]


def test_a_folder_whose_entry_and_top_both_change_is_weighed_from_each_sides_own_top():
    old = Config(projects=[ProjectEntry(path=PAYROLL, profile=Profile.MAX)])
    new = Config(include_private_ips=False, projects=[ProjectEntry(path=PAYROLL, profile=Profile.MAX, ocr="auto")])
    assert "project payroll: private IP addresses: no longer masked" in loosened(old, new)


def test_weighing_a_folder_changes_neither_side_of_the_change():
    # A folder's own policy is laid over a copy of the global settings: never over the global settings themselves,
    # whose policies would then let its type through everywhere.
    entry = ProjectEntry(path=PAYROLL, profile=Profile.MAX, policies={"EMAIL": "allow"})
    old = Config(projects=[entry])
    new = _changed(old, include_private_ips=False)
    more = ProjectEntry(path=PAYROLL, profile=Profile.MAX, policies={"EMAIL": "allow", "PHONE": "allow"})
    changed = _changed(new, projects=[more.model_dump(mode="json")])
    for before, after in ((old, new), (old, changed)):
        assert loosened(before, after)
        assert before.policies == {} and after.policies == {}


@pytest.mark.timing
def test_a_check_at_the_projects_cap_whose_top_lowers_a_stricter_folder_takes_milliseconds():
    folders = [ProjectEntry(path=f"/home/emily/client-{i:05d}", profile=Profile.MAX,
                            terms=[TermConfig(value=f"Client Name {i:05d}")]) for i in range(200)]  # fmt: skip
    old = Config(terms=[TermConfig(value=v) for v in _words(500, 3)], projects=folders)
    new = _changed(old, include_private_ips=False, data="normal")
    found, took = _fastest(lambda: loosened(old, new))
    # Weighed in each of the 200 folders, and said once for them (Minor 9).
    assert [r for r in found if r.endswith("private IP addresses: no longer masked")] == [
        "projects client-00000, client-00001 and 198 more: private IP addresses: no longer masked"
    ]
    assert took < 0.2, f"{took:.2f} s"


def _words(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return list(dict.fromkeys("".join(rng.choice(string.ascii_lowercase) for _ in range(30)) for _ in range(count)))


def _fastest(work, times: int = 3):
    """What ``work()`` answers, and the shortest of ``times`` runs of it: what it costs, Python's collection of its
    memory (a pass over every object of the test run, hundreds of milliseconds) or another process aside."""
    took = []
    for _ in range(times):
        started = time.perf_counter()
        answer = work()
        took.append(time.perf_counter() - started)
    return answer, min(took)


@pytest.mark.timing
def test_a_change_of_every_entry_at_the_projects_cap_is_weighed_and_composed_in_a_fraction_of_a_second():
    """A-12: a waiting change that touches every entry cost 1.2-1.3 s per weighing and 3.6-3.7 s per composition at the
    projects' cap, on every instance, while each folder's rules were laid over a deep copy of the settings."""
    from latet.server.shared import composed

    folders = [ProjectEntry(path=f"/home/emily/client-{i:05d}", profile=Profile.MAX,
                            terms=[TermConfig(value=f"Client Name {i:05d}")]) for i in range(200)]  # fmt: skip
    approved = Config(terms=[TermConfig(value=v) for v in _words(500, 3)], projects=folders)
    found = approved.model_copy(
        update={"projects": [e.model_copy(update={"profile": Profile.BALANCED}) for e in folders]}
    )
    reasons, took = _fastest(lambda: loosened(approved, found))
    assert "projects client-00000, client-00001 and 198 more: profile: from max to balanced" in reasons
    assert took < 0.3, f"{took:.2f} s"
    result, took = _fastest(lambda: composed(found, approved, Config()))
    assert {e.profile for e in result.projects} == {Profile.MAX}  # every folder's lowering waits for her
    assert took < 0.75, f"{took:.2f} s"


@pytest.mark.timing
def test_settings_with_tens_of_thousands_of_terms_are_weighed_in_linear_time():
    # Each list compared item by item against the other was quadratic: 7.8 s for these, on every instance that holds a
    # space, at each re-application of it. About 0.05 s now alone, two or three times that in a whole run of the tests.
    terms = [TermConfig(value=v) for v in _words(30_000, 8)]
    fields = [FieldRule(name=f"column_{n}", mask=n % 2 == 0) for n in range(10_000)]  # half of them kept in clear
    old = Config(terms=terms, fields=fields, allow_values=_words(20_000, 9))
    new = Config(
        terms=[*terms[1:], TermConfig(value="Mercury Labs")],  # one removed, one added
        fields=[*fields[1:], FieldRule(name="notes", mask=False)],
        allow_values=[*old.allow_values, "support@acme.com"],
    )
    reasons, took = _fastest(lambda: loosened(old, new))
    assert set(reasons) == {
        "terms: 1 value no longer masked",
        "field rules: changed or removed",
        "field rules: values kept in clear in strict data mode",
        "values never to mask: 1 added",
    }
    assert loosened(new, new) == []
    assert took < 0.5, f"{took:.2f} s"


def test_the_dashboard_is_shown_the_guidance_it_approves():
    before = Config(llm_review=LlmReview(guidance="Ward names are confidential."))
    after = Config(projects=[ProjectEntry(path="C:/work/shop", review_guidance="Never report ACME.")])
    assert guidance_changes(before, after) == [
        {"where": None, "before": "Ward names are confidential.", "after": None},
        {"where": "shop", "before": None, "after": "Never report ACME."},
    ]
    assert guidance_changes(after, after) == []


@pytest.mark.parametrize(
    ("env", "name", "strict"),
    [
        ({"CLAUDECODE": "1"}, "claude-code", None),
        ({"HERMES_SESSION_ID": ""}, "hermes", None),  # Hermes may leave it empty
        ({"GOOSE_TERMINAL": "1", "AGENT": "goose"}, "goose", None),
        ({"OPENCLAW_SHELL": "exec"}, "openclaw", None),
        ({"GEMINI_CLI": "1"}, "gemini-cli", None),  # what Gemini CLI sets in the shells it runs commands in
        ({"AGENT": "Some-Agent"}, "some-agent", None),
        ({"CODEX_HOME": "C:/x"}, None, "codex"),  # a setting of yours: only the strict check counts it
        ({}, None, None),
    ],
)
def test_agents_are_told_by_their_variables(monkeypatch, env, name, strict):
    markers = {m for ms in agent.MARKERS.values() for m in ms} | {"AGENT"}
    for key in list(os.environ):  # the agent running these tests, if any
        if key in markers or key.startswith(tuple(agent.PREFIXES)):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert agent.agent_name() == name
    assert agent.agent_name(strict=True) == (name or strict)


# --- Task 12's re-review 3, Minors 1, 2 and 9 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "dropped",
    [
        {"name": "employee-id-old", "type": "EMPLOYEE_ID", "pattern": "(EMP-"},  # does not compile
        {"name": "employee-id-old", "type": "EMPLOYEE_ID", "pattern": r"\w+"},  # too broad: ordinary code matches it
        {"name": "employee-id-old", "type": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{4}\b", "enabled": False},  # off
    ],
    ids=["broken", "too-broad", "off"],
)
def test_a_recognizer_the_build_drops_puts_none_of_its_type_out_of_use(dropped):
    """Minor 1 (security): the build keeps the real recognizer of a type that a broken one before it names; weakening
    the real one is weighed, never taken for a change of one out of use."""
    real = {"name": "employee-id", "type": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{6}\b"}
    weakened = {**real, "pattern": r"\bEMP-\d{6}X\b"}  # finds less: none of the numbers it found
    old, new = Config(recognizers=[dropped, real]), Config(recognizers=[dropped, weakened])
    assert loosened(old, new) == ["recognizers: 1 changed or removed"]


def test_a_folders_own_lowering_is_said_though_the_top_lowers_the_same():
    """Minor 2: the folder's own entry lowers what the top lowers too, in the same words: both are said, since both
    are hers to approve."""
    old = Config(include_private_ips=True, projects=[ProjectEntry(path="/work/billing", include_private_ips=True)])
    new = Config(include_private_ips=False, projects=[ProjectEntry(path="/work/billing", include_private_ips=False)])
    assert loosened(old, new) == [
        "private IP addresses: no longer masked",
        "project billing: private IP addresses: no longer masked",
    ]
    only_top = Config(
        include_private_ips=False, projects=[ProjectEntry(path="/work/billing", include_private_ips=True)]
    )
    assert loosened(old, only_top) == ["private IP addresses: no longer masked"]  # the folder keeps its own


def test_folders_that_share_a_reason_say_it_once_by_count():
    """Minor 9: two settings lowered in two hundred folders were four hundred lines in her dashboard and in the agent's
    answer: a reason of several folders is said once, its folders named up to three, then counted."""
    folders = [f"/work/repo-{n:03d}" for n in range(200)]
    old = Config(projects=[ProjectEntry(path=f, include_private_ips=True, dump_guard=4000) for f in folders])
    new = Config(projects=[ProjectEntry(path=f, include_private_ips=False) for f in folders])
    assert loosened(old, new) == [
        "projects repo-000, repo-001 and 198 more: private IP addresses: no longer masked",
        "projects repo-000, repo-001 and 198 more: dump guard: raised or turned off",
    ]
    two = Config(projects=[ProjectEntry(path=f, include_private_ips=True) for f in folders[:2]])
    assert loosened(two, Config(projects=[ProjectEntry(path=f) for f in folders[:2]])) == [
        "projects repo-000 and repo-001: private IP addresses: no longer masked"
    ]
