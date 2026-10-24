"""Which changes to the settings lower protection, and which agents are recognised."""

import os
import random
import string
import time

import pytest

from latet.cli import agent
from latet.config import (
    Config,
    FieldRule,
    IdentityConfig,
    LiveReview,
    LlmReview,
    Policy,
    Profile,
    ProjectEntry,
    ProjectSettings,
    TermConfig,
)
from latet.engine.build import build
from latet.engine.detectors.pii import effective_pii
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.protection import changed_keys, guidance_changes, loosened

from ..conftest import ROOT_KEY


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


# --- 0.9.8: a term or a field rule of a type your policies let through --------------------------------------------

IBAN = "IT60X0542811101000000123456"
URL_ALLOWED = {"policies": {"URL": "allow"}}
IN_CLEAR_TERMS = "terms: 1 that leave what they find in clear (policy allow)"
IN_CLEAR_FIELDS = "field rules: 1 that leave what they find in clear (policy allow)"
ACME_REF = {"name": "acme-ref", "type": "ACME_REF", "pattern": r"\bACME-\d{6}\b", "policy": "allow"}
SHOP = "C:/work/shop"


def _shop(**settings) -> list[ProjectEntry]:
    return [ProjectEntry(path=SHOP, **URL_ALLOWED, **settings)]


@pytest.mark.parametrize(
    ("before", "after", "reasons"),
    [
        # A term of a type let through: its span outranks what the detectors find there (an IBAN), left in clear.
        (Config(**URL_ALLOWED), Config(terms=[TermConfig(value=IBAN, type="URL")], **URL_ALLOWED), [IN_CLEAR_TERMS]),
        # A term given such a type: still there, so no value is said to be no longer masked.
        (
            Config(terms=[TermConfig(value="Fonderia Brambilla", type="CLIENT")], **URL_ALLOWED),
            Config(terms=[TermConfig(value="Fonderia Brambilla", type="URL")], **URL_ALLOWED),
            [IN_CLEAR_TERMS],
        ),
        # Its type as the detector reads it (`url` is URL), and a type a recognizer's own policy lets through.
        (Config(**URL_ALLOWED), Config(terms=[TermConfig(value=IBAN, type="url")], **URL_ALLOWED), [IN_CLEAR_TERMS]),
        (
            Config(recognizers=[ACME_REF]),
            Config(recognizers=[ACME_REF], terms=[TermConfig(value=IBAN, type="ACME_REF")]),
            [IN_CLEAR_TERMS],
        ),
        # A field rule that masks, of a type let through: on a secret's field it outranks the password's detection.
        (
            Config(**URL_ALLOWED),
            Config(fields=[FieldRule(name="password", type="URL")], **URL_ALLOWED),
            [IN_CLEAR_FIELDS],
        ),
        (  # without a type its values are FIELD
            Config(policies={"FIELD": "allow"}),
            Config(policies={"FIELD": "allow"}, fields=[FieldRule(name="password")]),
            [IN_CLEAR_FIELDS],
        ),
        (
            Config(fields=[FieldRule(name="password", type="SECRET_NOTE")], **URL_ALLOWED),
            Config(fields=[FieldRule(name="password", type="URL")], **URL_ALLOWED),
            ["field rules: changed or removed", IN_CLEAR_FIELDS],
        ),
        # Under a project's settings, whose own policy lets the type through there.
        (
            Config(projects=_shop()),
            Config(projects=_shop(terms=[TermConfig(value=IBAN, type="URL")])),
            [f"project shop: {IN_CLEAR_TERMS}"],
        ),
        (
            Config(projects=_shop(terms=[TermConfig(value="Fonderia Brambilla", type="CLIENT")])),
            Config(projects=_shop(terms=[TermConfig(value="Fonderia Brambilla", type="URL")])),
            [f"project shop: {IN_CLEAR_TERMS}"],
        ),
        (
            Config(projects=_shop()),
            Config(projects=_shop(fields=[FieldRule(name="password", type="URL")])),
            [f"project shop: {IN_CLEAR_FIELDS}"],
        ),
        # Added at the top, of a type the folder's own policy lets through: in clear in that folder alone.
        (
            Config(projects=_shop()),
            Config(terms=[TermConfig(value=IBAN, type="URL")], projects=_shop()),
            [f"project shop: {IN_CLEAR_TERMS}"],
        ),
        (
            Config(projects=_shop()),
            Config(fields=[FieldRule(name="password", type="URL")], projects=_shop()),
            [f"project shop: {IN_CLEAR_FIELDS}"],
        ),
    ],
)
def test_a_term_or_a_field_rule_of_a_type_let_through_lowers_protection(before, after, reasons):
    assert loosened(before, after) == reasons


def _in_every_folder(old: Config, new: Config) -> list[str]:
    """What a change of the top lowers in each folder of ``new`` (whose entry did not change), each weighed in full: its
    rules under the old settings against its rules under the new ones, past what the change lowers at the top, said as
    the check says it. What the check must answer, however it weighs them."""
    from latet.projects import effective_config, norm_path
    from latet.protection import _by_folders, lowered_in_project

    top = set(loosened(old.model_copy(update={"projects": []}), new.model_copy(update={"projects": []})))
    said = []
    for entry in sorted(new.projects, key=lambda e: norm_path(e.path)):
        old_here, _ = effective_config(old, None, entry)
        new_here, _ = effective_config(new, None, entry)
        name = entry.path.rstrip("/").rsplit("/", 1)[-1]
        said += [(name, reason) for reason in lowered_in_project(old_here, new_here) if reason not in top]
    return sorted(_by_folders(said))


@pytest.mark.timing
def test_a_change_of_the_top_terms_at_the_projects_cap_whose_folders_let_a_type_through_takes_milliseconds():
    """Important 1 of the 0.9.8 review: a term or a field rule changed at the top sent every folder whose own settings
    let a type through to a weighing in full, over every term, at each check: 18.9 s for one term added under 30,000
    with 200 such folders. Only what changed at the top, of a type such a folder lets through, is weighed there."""
    folders = [
        ProjectEntry(path=f"/home/emily/client-{i:05d}", policies={"EMAIL": "allow"},
                     terms=[TermConfig(value=f"Client Name {i:05d}")])
        for i in range(200)
    ]  # fmt: skip
    old = Config(terms=[TermConfig(value=v) for v in _words(30_000, 3)], projects=folders)
    leave = "terms: 1 that leave what they find in clear (policy allow)"
    for name, change, reasons in (
        ("a term", {"terms": [*old.terms, TermConfig(value="Mercury Labs")]}, []),
        ("an EMAIL term", {"terms": [*old.terms, TermConfig(value="ops@acme-intra.it", type="EMAIL")]}, [leave]),
        ("an EMAIL field rule", {"fields": [FieldRule(name="contact", type="EMAIL")]}, [leave.replace("terms", "field rules")]),
        ("a term removed", {"terms": old.terms[1:]}, []),  # said at the top, where it lowers every folder alike
    ):  # fmt: skip
        new = old.model_copy(update=change)
        found, took = _fastest(lambda new=new: loosened(old, new))
        assert [r for r in found if r.startswith("project")] == [
            f"projects client-00000, client-00001 and 198 more: {reason}" for reason in reasons
        ], name
        assert took < 0.2, f"{name}: {took:.2f} s"


CLIENT_NAME = TermConfig(value="Client Name", type="CLIENT")
EXACT_EMAIL = TermConfig(value="Client Name", type="EMAIL", case_sensitive=True)


@pytest.mark.parametrize(
    "change",
    [
        {"terms": [TermConfig(value="Project Atlas"), TermConfig(value="ops@acme-intra.it", type="EMAIL")]},
        {"terms": [TermConfig(value="Project Atlas", type="email")]},  # retyped, its type written in any case
        {"terms": [TermConfig(value="Project Atlas", whole_word=False)]},  # matches more forms
        {"terms": [TermConfig(value="Project Atlas"), TermConfig(value="Shop Secret", type="URL")]},  # one folder's
        {"terms": [TermConfig(value="Project Atlas"), TermConfig(value=IBAN, type="ACME_REF")]},  # a recognizer's
        {"terms": [TermConfig(value="Project Atlas"), TermConfig(value="Client Name", type="EMAIL")]},  # its own
        {"terms": [TermConfig(value="x@acme.com", type="EMAIL")], "policies": {"URL": Policy.ALLOW}},
        {"fields": [FieldRule(name="contact", type="EMAIL"), FieldRule(name="pin", type="URL", mask=False)]},
        {"fields": [FieldRule(name="password", type="URL")], "policies": {"URL": Policy.REDACT}},
        {"terms": [], "fields": []},
        # The top drops one typing of a value it keeps under another, or retypes it: the folder's own term of that
        # value and type, wider and of a type it lets through, comes into force there (Critical A of the review).
        {"before": [CLIENT_NAME, EXACT_EMAIL], "terms": [CLIENT_NAME]},
        {"before": [EXACT_EMAIL], "terms": [CLIENT_NAME]},
        {"before": [EXACT_EMAIL], "terms": []},
    ],
    ids=[
        "added", "retyped", "wider", "url", "recognizer", "own", "with-policy", "fields", "fields-policy", "removed",
        "typing-dropped", "typing-retyped", "typing-removed",
    ],
)  # fmt: skip
def test_a_change_of_the_top_terms_is_weighed_in_each_folder_as_in_full(change):
    """What a change of the top's terms and field rules lowers in folders whose own settings let a type through
    (their policies, their recognizers), weighed from what changed alone, is what weighing each folder in full says.
    ``before``: the top's terms before the change, when they are not Project Atlas alone."""
    folders = [
        ProjectEntry(path="/work/mail", policies={"EMAIL": "allow"}, terms=[TermConfig(value="Client Name", type="EMAIL")]),
        ProjectEntry(path="/work/shop", policies={"URL": "allow", "EMAIL": "mask"}),
        ProjectEntry(path="/work/acme", recognizers=[ACME_REF], fields=[FieldRule(name="contact", type="EMAIL")]),
        ProjectEntry(path="/work/plain", profile=Profile.MAX),
    ]  # fmt: skip
    change = dict(change)
    old = Config(terms=change.pop("before", [TermConfig(value="Project Atlas")]), projects=folders)
    new = old.model_copy(update=change)
    assert sorted(r for r in loosened(old, new) if r.startswith("project")) == _in_every_folder(old, new)


def test_a_repository_files_term_set_aside_there_does_not_come_into_force_when_the_top_drops_its_value():
    """Re-review minor 2: the top dropping its entry of a value brought the file's entry of it into the weighing, though
    the file's is set aside there (its type let through by your settings for the folder): said as leaving it in clear,
    where a weighing in full says nothing of the repository. The file's term the top adds is still weighed."""
    wide = TermConfig(value="Fonderia Brambilla", type="EMAIL")
    exact = TermConfig(value="Fonderia Brambilla", type="EMAIL", case_sensitive=True)
    lets = [ProjectEntry(path="C:/work/acme", policies={"EMAIL": "allow"})]
    repository = ("C:/work/acme", ProjectSettings(terms=[wide]))
    dropped = loosened(Config(terms=[exact], projects=lets), Config(projects=lets), [repository])
    assert dropped == ["terms: 1 value no longer masked"]
    added = loosened(Config(projects=lets), Config(terms=[wide], projects=lets), [repository])
    assert added == [f"project acme: {IN_CLEAR_TERMS}"]


def test_a_term_added_at_the_top_is_weighed_in_a_repository_whose_folder_lets_its_type_through():
    """A repository's own recognizer that lets what it finds through is set aside there (``projects._layer``): a term
    of its type added at the top is masked in that repository, as at the top. One of a type your settings for its folder
    let through is in clear there, and said once for the folder."""
    repository = ("C:/work/acme", ProjectSettings(recognizers=[ACME_REF]))
    found = Config(terms=[TermConfig(value=IBAN, type="ACME_REF")])
    assert loosened(Config(), found) == []  # masked at the top
    assert loosened(Config(), found, [repository]) == []  # and in the repository
    yours = [ProjectEntry(path="C:/work/acme", **URL_ALLOWED)]
    url = Config(terms=[TermConfig(value=IBAN, type="URL")], projects=yours)
    assert loosened(Config(projects=yours), url, [repository]) == [f"project acme: {IN_CLEAR_TERMS}"]


def test_a_term_or_a_field_rule_of_a_type_masked_or_in_clear_already_lowers_nothing_new():
    term, rule = TermConfig(value=IBAN, type="URL"), FieldRule(name="password", type="URL")
    # Their type masked: what they find is masked as that type.
    assert loosened(Config(), Config(terms=[term], fields=[rule])) == []
    redacted = {"policies": {"URL": "redact"}}
    assert loosened(Config(**redacted), Config(terms=[term], fields=[rule], **redacted)) == []
    # A field rule that keeps its values in clear in strict data mode finds nothing to mask: said as such.
    notes = FieldRule(name="notes", type="URL", mask=False)
    assert loosened(Config(**URL_ALLOWED), Config(fields=[notes], **URL_ALLOWED)) == [
        "field rules: values kept in clear in strict data mode"
    ]
    # In clear already, as they are: nothing new, however their type is written.
    kept = Config(terms=[term], fields=[rule], **URL_ALLOWED)
    assert loosened(kept, Config(terms=[term], fields=[rule], **URL_ALLOWED)) == []
    assert loosened(kept, Config(terms=[TermConfig(value=IBAN, type="url")], fields=[rule], **URL_ALLOWED)) == []
    # In clear inside longer words too, or in any case: more of it in clear.
    for wider in (TermConfig(value=IBAN, type="URL", whole_word=False), TermConfig(value="Acme", type="URL")):
        exact = TermConfig(value=wider.value, type="URL", case_sensitive=True)
        assert loosened(Config(terms=[term, exact], **URL_ALLOWED), Config(terms=[term, wider], **URL_ALLOWED)) == [
            IN_CLEAR_TERMS
        ]


def test_a_policy_set_to_allow_is_said_once_for_the_terms_and_field_rules_of_its_type():
    """A policy set to allow for a type some terms and field rules have: said as the policy, which names the type, and
    not again for each of them; a term or a field rule of that type added with it is one more, said too."""
    term, rule = TermConfig(value=IBAN, type="URL"), FieldRule(name="password", type="URL")
    held = Config(terms=[term], fields=[rule])
    assert loosened(held, Config(terms=[term], fields=[rule], **URL_ALLOWED)) == ["policy for URL: from mask to allow"]
    more = Config(
        terms=[term, TermConfig(value="Fonderia Brambilla", type="URL")],
        fields=[rule, FieldRule(name="api_key", type="URL")],
        **URL_ALLOWED,
    )
    assert loosened(held, more) == [IN_CLEAR_TERMS, "policy for URL: from mask to allow", IN_CLEAR_FIELDS]


def test_a_term_or_a_field_rule_of_a_type_let_through_that_is_removed_is_said_as_removed():
    """Of a type let through, a term or a field rule still masks something (an EMAIL one brings its domain along):
    removing one lowers protection, said as any removal."""
    kept = Config(
        terms=[TermConfig(value=IBAN, type="URL")], fields=[FieldRule(name="password", type="URL")], **URL_ALLOWED
    )
    assert loosened(kept, Config(**URL_ALLOWED)) == [
        "terms: 1 value no longer masked",
        "field rules: changed or removed",
    ]


def _sent(config: Config, text: str, kind: SlotKind = SlotKind.USER_TEXT) -> tuple[str, list[str]]:
    """``text`` masked as ``config`` masks it, and the types the final check finds left in clear in it."""
    built = build(config.model_copy(update={"identity": IdentityConfig(enabled=False)}), Vault(ROOT_KEY))
    masked = built.engine.mask_text(text, kind, built.engine.session("s")).text
    return masked, [leak.etype for leak in built.verifier.verify([(masked, kind, False)], "s").blocking]


def test_a_term_or_a_field_rule_of_a_type_let_through_leaves_what_it_finds_in_clear():
    """Why they lower protection: a term's span outranks the detectors' (an IBAN found as personal data), a field rule's
    the password's detection, and a type let through leaves either in clear. The final check stops a request that
    carries the IBAN; a client's name, which only its term finds, leaves, and so does the password."""
    text = f"please wire it to {IBAN} today"
    assert IBAN not in _sent(Config(**URL_ALLOWED), text)[0]
    assert _sent(Config(terms=[TermConfig(value=IBAN, type="URL")], **URL_ALLOWED), text) == (text, ["IBAN"])
    contract = "the Fonderia Brambilla contract"
    client = Config(terms=[TermConfig(value="Fonderia Brambilla", type="CLIENT")], **URL_ALLOWED)
    assert "Fonderia Brambilla" not in _sent(client, contract)[0]
    retyped = Config(terms=[TermConfig(value="Fonderia Brambilla", type="URL")], **URL_ALLOWED)
    assert _sent(retyped, contract) == (contract, [])
    secret = '{"user": "emily", "password": "Wq7-hunter2-Zx9!"}'
    assert "hunter2" not in _sent(Config(**URL_ALLOWED), secret, SlotKind.TOOL_RESULT)[0]
    rule = Config(fields=[FieldRule(name="password", type="URL")], **URL_ALLOWED)
    assert _sent(rule, secret, SlotKind.TOOL_RESULT) == (secret, [])


def test_an_email_term_of_a_type_let_through_still_masks_its_domain_and_removing_it_is_said():
    """E-mail addresses let through, and not detected: a term typed EMAIL leaves its address in clear and still brings
    its domain along, masked wherever else it appears (``Masker._repeats``). Removing it lowers protection, and is said
    as any term no longer masked; a field rule typed EMAIL, as any field rule removed."""
    address = "ops@acme-intra.it"
    allowed = Config(policies={"EMAIL": "allow"}, pii=["PHONE"])
    text = f"write to {address}; the API is api.acme-intra.it"
    assert _sent(allowed, text)[0] == text
    with_term = allowed.model_copy(update={"terms": [TermConfig(value=address, type="EMAIL")]})
    masked = _sent(with_term, text)[0]
    assert address in masked and "api.acme-intra.it" not in masked
    assert loosened(with_term, allowed) == ["terms: 1 value no longer masked"]
    with_rule = allowed.model_copy(update={"fields": [FieldRule(name="contact", type="EMAIL")]})
    assert loosened(with_rule, allowed) == ["field rules: changed or removed"]


def test_a_policy_taken_away_that_lets_a_recognizers_own_allow_apply_lowers_protection():
    """A recognizer's own policy is its type's default and an entry of ``policies`` wins over it: taking away
    ``ACME_REF: mask`` lets the recognizer's ``allow`` apply, and what it finds leaves in clear, as a policy set to allow
    does. Weighed as the policy the type ends up with; the other way round, nothing is lowered."""
    held = Config(recognizers=[ACME_REF], policies={"ACME_REF": "mask"})
    freed = Config(recognizers=[ACME_REF])
    assert loosened(held, freed) == ["policy for ACME_REF: from mask to allow"]
    assert loosened(freed, held) == []
    text = "ticket ACME-123456 open"
    assert "ACME-123456" not in _sent(held, text)[0] and _sent(freed, text)[0] == text
    redact = {**ACME_REF, "policy": "redact"}
    assert loosened(Config(recognizers=[redact], policies={"ACME_REF": "allow"}), Config(recognizers=[redact])) == []


BADGE_WIDE = {"name": "badge-team", "type": "BADGE", "pattern": r"\bBD-[0-9]{6,8}\b", "policy": "redact"}
BADGE_NARROW = {"name": "badge-mine", "type": "BADGE", "pattern": r"\bBD-[0-9]{6}\b"}


def test_a_recognizer_renamed_or_made_stricter_is_the_same_recognizer_and_one_whose_values_lose_a_policy_is_not():
    """A recognizer is matched by what it finds and does, not by its name and type: renamed, or laid under a name and a
    type of its own in a project (``projects.effective_config``, Ruling 098-R3), or given a stricter policy of its own,
    it finds the same values at a policy as strict, and nothing is lowered. Retyped away from a type `policies` redacts,
    its values become fakes: said. A `policies` entry taken away is said as that policy alone, not as the recognizer."""
    badge = {"name": "badge", "type": "BADGE", "pattern": r"\bBD-\d{6}\b"}
    assert loosened(Config(recognizers=[badge]), Config(recognizers=[{**badge, "name": "badges"}])) == []
    assert loosened(Config(recognizers=[badge]), Config(recognizers=[{**badge, "type": "OTHER"}])) == []
    assert loosened(Config(recognizers=[badge]), Config(recognizers=[{**badge, "policy": "redact"}])) == []
    redacted = Config(recognizers=[badge], policies={"BADGE": "redact"})
    retyped = Config(recognizers=[{**badge, "type": "OTHER"}], policies={"BADGE": "redact"})
    assert loosened(redacted, retyped) == ["recognizers: 1 changed or removed"]
    assert loosened(Config(recognizers=[{**badge, "policy": "redact"}]), Config(recognizers=[badge])) == [
        "recognizers: 1 changed or removed"
    ]
    held = Config(recognizers=[{**badge, "policy": "allow"}], policies={"BADGE": "mask"})
    assert loosened(held, Config(recognizers=[{**badge, "policy": "allow"}])) == [
        "policy for BADGE: from mask to allow"
    ]
    # One turned off, one that finds fewer values, one gone: said as before; one turned on: not.
    for after in ({**badge, "enabled": False}, {**badge, "pattern": r"\bBD-\d{6}X\b"}):
        assert loosened(Config(recognizers=[badge]), Config(recognizers=[after])) == [
            "recognizers: 1 changed or removed"
        ]
    assert loosened(Config(recognizers=[badge]), Config()) == ["recognizers: 1 changed or removed"]
    assert loosened(Config(recognizers=[{**badge, "enabled": False}]), Config(recognizers=[badge])) == []
    # One added that leaves what it finds in clear, by its own policy or by the type's: said; one in clear already: not.
    allowed = Config(policies={"ACME_REF": "allow"})
    assert loosened(allowed, allowed.model_copy(update={"recognizers": [{**ACME_REF, "policy": None}]})) == [
        "recognizers: 1 that leave what they find in clear (policy allow)"
    ]
    assert loosened(Config(recognizers=[ACME_REF]), Config(recognizers=[{**ACME_REF, "name": "acme"}])) == []


def test_a_recognizer_of_yours_of_a_repository_files_type_added_for_its_folder_lowers_nothing_there():
    """Ruling 098-R3: in a repository whose latet.yaml has a recognizer of a type, adding one of yours of that type for
    the folder lays the file's apart, both applying: nothing is lowered there, and an agent's such change applies at
    once. Taking yours away is said, as a recognizer removed; a file's recognizer of your type that finds nothing (the
    reviewer's probe D) takes nothing away either."""
    repository = ("C:/work/acme", ProjectSettings(recognizers=[BADGE_WIDE]))
    mine = ProjectEntry(path="C:/work/acme", recognizers=[BADGE_NARROW])
    assert loosened(Config(), Config(projects=[mine]), [repository]) == []
    assert loosened(Config(projects=[mine]), Config(), [repository]) == [
        "project acme: recognizers: 1 changed or removed"
    ]
    never = ProjectEntry(path="C:/work/acme", recognizers=[{**BADGE_NARROW, "pattern": r"\bQQZX-[0-9]{30}\b"}])
    assert loosened(Config(), Config(projects=[never]), [repository]) == []
    off = ProjectEntry(path="C:/work/acme", recognizers=[{**BADGE_WIDE, "enabled": False}])
    assert loosened(Config(), Config(projects=[off]), [repository]) == []


HOSTS = {"name": "hosts", "type": "INTERNAL_HOST", "pattern": r"\bsrv-[0-9]{4}\b", "policy": "allow"}


def test_a_recognizer_retyped_is_weighed_for_the_terms_and_field_rules_of_the_type_it_leaves_or_joins():
    """Round 5 (C1): a recognizer's own policy is its type's default, and the type's terms and field rules follow it.
    `hosts` (policy allow) retyped to the type of a term sends the term's values in clear (an IBAN typed LEDGER, a
    client's name), to the type of a field rule the field's values; a redacting one retyped away from a term's type
    turns its values from redacted into fakes; two recognizers swapping types do the same. 0.9.7 held each as a
    recognizer changed; the policy in force of every type a term or a field rule uses is now weighed, as a `policies`
    entry is, at the top and in a folder. A retype that loosens a type nothing of the settings names (the vault finds
    remembered values again under their type) waits as a recognizer changed, as in 0.9.7 (Ruling 098-R4)."""
    ledger = TermConfig(value=IBAN, type="LEDGER")
    old, new = (
        Config(terms=[ledger], recognizers=[HOSTS]),
        Config(terms=[ledger], recognizers=[{**HOSTS, "type": "LEDGER"}]),
    )
    text = f"wire to {IBAN} today"
    assert IBAN not in _sent(old, text)[0] and IBAN in _sent(new, text)[0]
    assert loosened(old, new) == ["policy for LEDGER: from mask to allow"]
    assert loosened(new, old) == ["recognizers: 1 changed or removed"]  # INTERNAL_HOST let through, nothing names it
    client = TermConfig(value="Fonderia Brambilla", type="CLIENT")
    joined = Config(terms=[client], recognizers=[{**HOSTS, "type": "CLIENT"}])
    assert loosened(Config(terms=[client], recognizers=[HOSTS]), joined) == ["policy for CLIENT: from mask to allow"]
    rule = FieldRule(name="password", type="CREDENTIAL")
    credential = Config(fields=[rule], recognizers=[{**HOSTS, "type": "CREDENTIAL"}])
    assert loosened(Config(fields=[rule], recognizers=[HOSTS]), credential) == [
        "policy for CREDENTIAL: from mask to allow"
    ]
    code = {"name": "code", "type": "CODENAME", "pattern": r"\bFAL-[0-9]{4}\b", "policy": "redact"}
    falcon = TermConfig(value="Project Falcon", type="CODENAME")
    old, new = (
        Config(terms=[falcon], recognizers=[code]),
        Config(terms=[falcon], recognizers=[{**code, "type": "CODEREF"}]),
    )
    before, after = _sent(old, "about Project Falcon")[0], _sent(new, "about Project Falcon")[0]
    assert "[REDACTED:CODENAME]" in before and "[REDACTED" not in after and "Project Falcon" not in after
    assert loosened(old, new) == ["policy for CODENAME: from redact to mask"]
    a = {"name": "a", "type": "CLIENT", "pattern": r"\bCL-[0-9]{4}\b"}
    b = {"name": "b", "type": "INTERNAL_HOST", "pattern": r"\bsrv-[0-9]{4}\b", "policy": "allow"}
    swapped = [{**a, "type": "INTERNAL_HOST"}, {**b, "type": "CLIENT"}]
    assert "policy for CLIENT: from mask to allow" in loosened(
        Config(terms=[client], recognizers=[a, b]), Config(terms=[client], recognizers=swapped)
    )
    # A folder's own term of the type a global recognizer joins: said for the folder; at the top, where nothing names
    # the type let through, as a recognizer changed.
    folder = ProjectEntry(path="C:/work/acme", terms=[ledger])
    old, new = (
        Config(recognizers=[HOSTS], projects=[folder]),
        Config(recognizers=[{**HOSTS, "type": "LEDGER"}], projects=[folder]),
    )
    assert loosened(old, new) == [
        "recognizers: 1 changed or removed",
        "project acme: policy for LEDGER: from mask to allow",
    ]
    # A retype that lets a type nothing names through waits as a recognizer changed (no type named, as 0.9.7 said it); a
    # rename, a retype between types with no policy of their own, a stricter own policy wait for nobody.
    assert loosened(Config(recognizers=[HOSTS]), Config(recognizers=[{**HOSTS, "type": "LEDGER"}])) == [
        "recognizers: 1 changed or removed"
    ]
    badge = {"name": "badge", "type": "BADGE", "pattern": r"\bBD-\d{6}\b"}
    for after_one in ({**badge, "name": "badges"}, {**badge, "type": "OTHER"}, {**badge, "policy": "redact"}):
        assert loosened(Config(recognizers=[badge]), Config(recognizers=[after_one])) == []


STRICT_ROW = '{"customer": "Fonderia Brambilla", "balance": "48213.55", "host": "srv-0042"}'


def test_a_recognizer_retyped_to_or_from_a_type_latets_own_detectors_give_is_weighed_for_their_values():
    """Ruling 098-R4 (N1): a recognizer may take a type another detector of Latet gives values, which `reserved_types`
    leaves free (strict data mode's DATA, the name detector's ZIPCODE, the local reviewer's SECRET, ORG, HEALTH...), and
    its own policy becomes that type's default: `hosts` (policy allow) retyped DATA sent every value strict data mode
    masks in clear, and the change applied at once, where 0.9.7 held it as a recognizer changed; a redacting DATA
    recognizer retyped away turned them into fakes. Said as the policy the type ends up with, at the top and in a
    folder; those types stay free for a recognizer to take."""
    old, new = (
        Config(profile="max", recognizers=[HOSTS]),
        Config(profile="max", recognizers=[{**HOSTS, "type": "DATA"}]),
    )
    table = SlotKind.TOOL_RESULT  # strict data mode masks a tool's output
    assert "Fonderia Brambilla" not in _sent(old, STRICT_ROW, table)[0]
    assert "Fonderia Brambilla" in _sent(new, STRICT_ROW, table)[0]
    assert loosened(old, new) == ["policy for DATA: from mask to allow"]
    written = Config(data="strict", recognizers=[HOSTS]), Config(data="strict", recognizers=[{**HOSTS, "type": "DATA"}])
    assert loosened(*written) == ["policy for DATA: from mask to allow"]
    code = {"name": "code", "type": "DATA", "pattern": r"\bFAL-[0-9]{4}\b", "policy": "redact"}
    old, new = (
        Config(profile="max", recognizers=[code]),
        Config(profile="max", recognizers=[{**code, "type": "CODEREF"}]),
    )
    assert "[REDACTED:DATA]" in _sent(old, STRICT_ROW, table)[0] and "[REDACTED" not in _sent(new, STRICT_ROW, table)[0]
    assert loosened(old, new) == ["policy for DATA: from redact to mask"]
    for etype in ("SECRET", "ORG", "HEALTH", "ZIPCODE", "FIELD"):
        retyped = Config(recognizers=[{**HOSTS, "type": etype}])
        assert loosened(Config(recognizers=[HOSTS]), retyped) == [f"policy for {etype}: from mask to allow"], etype
    folder = ProjectEntry(path="C:/work/acme", data="strict", recognizers=[HOSTS])
    retyped_there = ProjectEntry(path="C:/work/acme", data="strict", recognizers=[{**HOSTS, "type": "DATA"}])
    assert loosened(Config(projects=[folder]), Config(projects=[retyped_there])) == [
        "project acme: policy for DATA: from mask to allow"
    ]


def test_a_recognizer_retyped_to_a_type_the_vault_remembers_values_as_is_weighed_for_them():
    """Ruling 098-R4 (N1b): a value the vault remembers keeps the type it was found as, and is found again under it at
    that type's policy in force. `ref` found RF-482913 as REF and was taken away since (approved): the value stays
    masked as a known one. `hosts` (policy allow) retyped REF lets it out: weighed as a recognizer changed, as 0.9.7
    did, no type named (nothing of the settings names REF)."""
    vault = Vault(ROOT_KEY)

    def masked(config: Config, text: str) -> str:
        quiet = config.model_copy(update={"identity": config.identity.model_copy(update={"enabled": False})})
        built = build(quiet, vault)
        return built.engine.mask_text(text, SlotKind.USER_TEXT, built.engine.session("s")).text

    ref = {"name": "ref", "type": "REF", "pattern": r"\bRF-[0-9]{6}\b"}
    text = "order RF-482913 on srv-0042"
    assert "RF-482913" not in masked(Config(recognizers=[ref, HOSTS]), text)
    before, after = Config(recognizers=[HOSTS]), Config(recognizers=[{**HOSTS, "type": "REF"}])
    assert "RF-482913" not in masked(before, text) and "RF-482913" in masked(after, text)
    assert loosened(before, after) == ["recognizers: 1 changed or removed"]
