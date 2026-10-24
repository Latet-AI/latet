"""Settings for one project: the repository's latet.yaml (only stricter) and your own (anything)."""

import errno
import logging
import os
import re
import shutil
import signal
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from latet.config import Config, FieldRule, ProjectEntry, ProjectSettings, TermConfig
from latet.engine.build import build
from latet.engine.pipeline import Engine
from latet.engine.types import Policy, SlotKind
from latet.engine.vault import MaskingError, Vault
from latet.engine.verifier import Verifier
from latet.gateway.mediate import cwd_from
from latet.projects import (
    MAX_FILE_BYTES,
    PROJECT_FILE,
    Projects,
    SeenRepositories,
    effective_config,
    find_project_file,
    norm_path,
    personal_for,
    project_id,
    project_root,
    read_project_file,
    render_project_file,
    with_personal,
    within,
)
from latet.protection import loosened
from latet.protocols import openai_chat, openai_responses
from latet.state.local import LocalRepositories

from ..conftest import ROOT_KEY


def _repo(tmp_path, text=None, name="acme"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "src" / "deep").mkdir(parents=True)
    if text is not None:
        (root / PROJECT_FILE).write_text(text, "utf-8")
    return root


def test_the_nearest_file_up_to_the_repository_root(tmp_path):
    root = _repo(tmp_path, "profile: max\n")
    assert find_project_file(root / "src" / "deep") == root / PROJECT_FILE
    assert project_root(str(root / "src" / "deep")) == (str(root), root / PROJECT_FILE)
    (tmp_path / PROJECT_FILE).write_text("profile: max\n", "utf-8")  # above the repository: not read
    bare = _repo(tmp_path, name="bare")
    assert find_project_file(bare / "src") is None
    assert project_root(str(bare / "src")) == (str(bare), None)  # the repository is the project
    outside = tmp_path / "loose"
    outside.mkdir()
    assert project_root(str(outside)) == (str(outside), None)
    assert project_root("C:/not/on/this/machine") == ("C:/not/on/this/machine", None)


def test_folders_compare_without_slashes_and_on_windows_without_case():
    assert norm_path("C:\\Users\\Me\\dev\\Acme\\") == "c:/users/me/dev/acme"
    assert within("c:/users/me/dev/acme/src", "C:\\Users\\Me\\dev\\Acme")
    assert not within("c:/users/me/dev/acme2", "C:/Users/Me/dev/Acme")
    entries = [ProjectEntry(path="C:/dev"), ProjectEntry(path="C:/dev/acme", allow_values=["x"])]
    assert personal_for(entries, "c:\\dev\\acme\\api").path == "C:/dev/acme"  # the nearest folder wins


def test_a_repository_file_only_adds_protection(tmp_path):
    root = _repo(
        tmp_path,
        "profile: max\nid_regions: [DE]\npii: [IBAN]\nterms: [{value: Project Falcon, type: PROJECT}]\n"
        "policies: {EMAIL: redact}\ninclude_private_ips: true\n"
        "allow_values: [acme-dev]\nallow_patterns: ['.*']\ndisabled_secret_rules: [generic-api-key]\n"
        "enabled: false\nport: 9999\nwhatever: 1\n",
    )
    settings, notices = read_project_file(root / PROJECT_FILE)
    ignored = {n.key for n in notices if n.level == "ignored"}
    assert ignored == {"allow_values", "allow_patterns", "disabled_secret_rules", "enabled", "port", "whatever"}
    config, more = effective_config(Config(), settings, None)
    assert more == []
    assert config.profile.value == "max"
    assert "DE" in config.id_regions and "IBAN" in config.pii and "DE_TAX_ID" in config.pii
    assert [t.value for t in config.terms] == ["Project Falcon"]
    assert config.policies["EMAIL"] == Policy.REDACT
    assert config.include_private_ips is True
    assert config.allow_values == [] and config.disabled_secret_rules == []


def test_a_repository_files_ignored_settings_carry_the_reason_the_dashboard_words(tmp_path):
    """The dashboard words a notice in its reader's language by its reason: each setting a latet.yaml cannot set says
    why with one, as the daemon's English does."""
    root = _repo(tmp_path, "allow_values: [acme-dev]\nenabled: false\nport: 9999\nwhatever: 1\n")
    _, notices = read_project_file(root / PROJECT_FILE)
    reasons = {n.key: n.reason for n in notices if n.level == "ignored"}
    assert reasons == {
        "allow_values": "repo_cannot_set",
        "enabled": "repo_cannot_set",
        "port": "repo_whole_latet",
        "whatever": "repo_unknown",
    }
    assert all(n.as_dict()["reason"] == reasons[n.key] for n in notices if n.level == "ignored")


QUIET = {"name": "quiet", "type": "QUIET", "pattern": r"\bNEVER-\d{40}\b", "policy": "allow"}
AMOUNT = {"name": "amount", "type": "INTERNAL_ID", "pattern": "[0-9]{5}[.][0-9]{2}"}
SECRET_JSON = '{"user": "emily", "password": "Wq7-hunter2-Zx9!"}'
IN_CLEAR = "repo_in_clear"


def _sent(config: Config, text: str, kind: SlotKind = SlotKind.TOOL_RESULT) -> tuple[str, list[str]]:
    """``text`` masked as ``config`` masks it, and the types the final check finds left in clear in it."""
    built = build(config.model_copy(update={"identity": config.identity.model_copy(update={"enabled": False})}),
                  Vault(ROOT_KEY))  # fmt: skip
    masked = built.engine.mask_text(text, kind, built.engine.session("s")).text
    return masked, [leak.etype for leak in built.verifier.verify([(masked, kind, False)], "s").blocking]


def test_a_repository_file_cannot_leave_a_password_in_clear_through_a_type_of_its_own(tmp_path):
    """0.9.8: a latet.yaml held a recognizer that lets what it finds through and a field rule for `password` of its
    type: in that repository every password left in clear, past the final check, and nothing waited for anyone. The
    recognizer is set aside, with a notice saying where to put it instead; set aside, it lets nothing through, so the
    field rule masks as its type says, and the password stays masked."""
    root = _repo(
        tmp_path,
        "recognizers:\n  - {name: quiet, type: QUIET, pattern: '\\bNEVER-\\d{40}\\b', policy: allow}\n"
        "fields:\n  - {name: password, type: QUIET}\n",
    )
    settings, _ = read_project_file(root / PROJECT_FILE)
    config, notices = effective_config(Config(), settings, None)
    assert "hunter2" not in _sent(config, SECRET_JSON)[0]
    assert [(n.key, n.reason) for n in notices] == [("recognizers.quiet", IN_CLEAR)]
    assert config.recognizers == [] and [f.name for f in config.fields] == ["password"]
    assert all(n.level == "ignored" and "your own settings" in n.message for n in notices)


@pytest.mark.parametrize(
    ("mine", "repo", "personal", "key"),
    [
        # A recognizer whose own policy lets what it finds through.
        (Config(), ProjectSettings(recognizers=[QUIET]), None, "recognizers.quiet"),
        # A term of a type let through: by your own policies, by the file's own recognizer, by your settings for it.
        (Config(policies={"URL": "allow"}), ProjectSettings(terms=[TermConfig(value="Acme Corp", type="url")]), None, "terms.1"),
        # A recognizer of a type let through there: by your own policies, by your settings for the folder.
        (Config(policies={"INTERNAL_ID": "allow"}), ProjectSettings(recognizers=[AMOUNT]), None, "recognizers.amount"),
        (Config(), ProjectSettings(recognizers=[AMOUNT]), ProjectEntry(path="/work/acme", policies={"INTERNAL_ID": "allow"}), "recognizers.amount"),
        (Config(), ProjectSettings(recognizers=[AMOUNT]), None, None),  # its type masked: it stays
        # A recognizer set aside lets nothing through: a term of its type masks, and stays (review minor 1).
        (Config(), ProjectSettings(recognizers=[QUIET], terms=[TermConfig(value="Acme Corp", type="QUIET")]), None, "recognizers.quiet"),
        (
            Config(),
            ProjectSettings(terms=[TermConfig(value="Acme Corp", type="URL")]),
            ProjectEntry(path="/work/acme", policies={"URL": "allow"}),
            "terms.1",
        ),
        # A field rule of a type let through, whether it masks or not: by your own policies, by a recognizer's own.
        (Config(policies={"URL": "allow"}), ProjectSettings(fields=[FieldRule(name="password", type="URL")]), None, "fields.password"),
        (
            Config(recognizers=[QUIET]),
            ProjectSettings(fields=[FieldRule(name="token")], policies={}),
            None,
            None,  # FIELD is masked: nothing set aside
        ),
        (Config(policies={"FIELD": "allow"}), ProjectSettings(fields=[FieldRule(name="token")]), None, "fields.token"),
        (Config(recognizers=[QUIET]), ProjectSettings(fields=[FieldRule(name="password", type="quiet")]), None, "fields.password"),
    ],
    ids=["recognizer", "term-policy", "recognizer-policy", "recognizer-personal", "recognizer-masked",
         "term-own-recognizer", "term-personal", "field-policy", "field-masked", "field-default-type", "field-recognizer"],
)  # fmt: skip
def test_a_repository_file_sets_aside_what_would_leave_values_in_clear(mine, repo, personal, key):
    config, notices = effective_config(mine, repo, personal)
    set_aside = [n for n in notices if n.reason == IN_CLEAR]
    assert [n.key for n in set_aside] == ([key] if key else [])
    assert all(n.source == "repo" and n.level == "ignored" for n in set_aside)
    assert not any("Acme" in n.message or "NEVER" in n.message for n in notices)  # never a value
    if key and key.startswith("terms."):
        assert "Acme Corp" not in [t.value for t in config.terms]
    if key and key.startswith("fields."):
        assert config.fields == []


def test_a_repository_recognizer_of_a_type_let_through_there_is_set_aside():
    """Review B1: a latet.yaml recognizer with no policy of its own, of a type your policies let through, found a
    table's balance and left it in clear in strict data mode, where it was masked everywhere else. Set aside, the
    balance stays masked there; a term of the type of a recognizer set aside for its own `allow` masks, and stays."""
    table = '{"customer": "Fonderia Brambilla", "balance": "48213.55"}'
    mine = Config(profile="max", policies={"INTERNAL_ID": "allow"})
    assert "48213.55" not in _sent(mine, table)[0]
    config, notices = effective_config(mine, ProjectSettings(recognizers=[AMOUNT]), None)
    assert "48213.55" not in _sent(config, table)[0]
    assert [(n.key, n.reason) for n in notices] == [("recognizers.amount", IN_CLEAR)]
    top = Config(recognizers=[{"name": "acme-mask", "type": "ACME_REF", "pattern": "ACME-[0-9]{6}", "policy": "mask"}])
    repo = ProjectSettings(
        recognizers=[{"name": "acme-ref", "type": "ACME_REF", "pattern": "REF-[0-9]{6}", "policy": "allow"}],
        terms=[TermConfig(value="Fonderia Brambilla", type="ACME_REF")],
    )
    config, notices = effective_config(top, repo, None)
    assert [n.key for n in notices] == ["recognizers.acme-ref"]
    assert "Fonderia Brambilla" not in _sent(config, "invoice for Fonderia Brambilla", SlotKind.USER_TEXT)[0]


BADGE_REDACTED = {"name": "badge", "type": "BADGE", "pattern": "BD-[0-9]{6}", "policy": "redact"}


@pytest.mark.parametrize(
    "theirs",
    [
        {"name": "badge", "type": "BADGE2", "pattern": "ZZQX-[0-9]{9}"},  # its name, a pattern that never matches
        {"name": "other", "type": "BADGE", "pattern": "BD-[0-9]{6}", "policy": "mask"},  # its type, a fake for it
    ],
    ids=["same-name", "same-type"],
)
def test_your_recognizers_for_a_folder_win_over_the_repository_files(theirs):
    """Review B2: the file was laid before your settings for its folder, and the first recognizer of a name or a type
    is the one in use: a latet.yaml recognizer of the name of yours put yours out of use (the badge in clear), one of
    its type made a reversible fake where you redact. Yours come first, as the global ones do."""
    yours = ProjectEntry(path="/work/acme", recognizers=[BADGE_REDACTED])
    alone, _ = effective_config(Config(projects=[yours]), None, yours)
    assert _sent(alone, "badge BD-381904", SlotKind.USER_TEXT)[0] == "badge [REDACTED:BADGE]"
    config, _ = effective_config(Config(projects=[yours]), ProjectSettings(recognizers=[theirs]), yours)
    assert _sent(config, "badge BD-381904", SlotKind.USER_TEXT)[0] == "badge [REDACTED:BADGE]"


WIDE, NARROW = r"\bBD-[0-9]{6,8}\b", r"\bBD-[0-9]{6}\b"
EMPID = r"\bEMP-[0-9]{5}\b"
NEVER = r"\bQQZX-[0-9]{30}\b"
BADGES = "badge BD-1234567 and BD-123456"
APART, RENAMED = "recognizer_apart", "recognizer_renamed"


def _badge(name: str, pattern: str = NARROW, **more: object) -> dict[str, object]:
    return {"name": name, "type": "BADGE", "pattern": pattern, **more}


@pytest.mark.parametrize(
    ("mine", "theirs", "top", "text", "masked_097", "redacted", "apart"),
    [
        # A tie (one type, one policy), the file's pattern broader or yours: 0.9.7 kept the file's, laid first.
        ([_badge("mine")], [_badge("team", WIDE)], [], BADGES, ["BD-1234567", "BD-123456"], [], [("team", APART)]),
        ([_badge("mine", WIDE)], [_badge("team")], [], BADGES, ["BD-123456"], [], [("team", APART)]),
        ([_badge("mine", policy="redact")], [_badge("team", WIDE, policy="redact")], [], BADGES, [], ["BD-1234567", "BD-123456"], [("team", APART)]),
        ([_badge("mine", WIDE, policy="redact")], [_badge("team", policy="redact")], [], BADGES, [], ["BD-123456"], [("team", APART)]),
        # One name, two types: 0.9.7 found the file's values alone, yours alone finds yours.
        ([{"name": "badge", "type": "EMPID", "pattern": EMPID}], [_badge("badge")], [], "BD-123456 EMP-12345", ["BD-123456"], [], [("badge", RENAMED)]),
        # Yours of its name that a build does not keep (off, broken, shadowed by a global one): the file's stays as it is.
        ([_badge("team", enabled=False)], [_badge("team")], [], BADGES, ["BD-123456"], [], []),
        ([_badge("team", "(", policy="redact")], [_badge("team")], [], BADGES, ["BD-123456"], [], []),
        ([_badge("old")], [_badge("team")], [{"name": "old", "type": "OLDREF", "pattern": r"\bOLD-[0-9]{6}\b"}], BADGES, ["BD-123456"], [], []),
        # One of yours against two of the file's: a redacting one of its type, one of its name of another type.
        ([_badge("b")], [_badge("a", policy="redact"), {"name": "b", "type": "EMPID", "pattern": EMPID}], [], "BD-123456 EMP-12345", ["EMP-12345"], ["BD-123456"], [("a", APART), ("b", RENAMED)]),
        # B2: the file's of the name or the type of a badge you redact, finding nothing or masking (0.9.7: in clear, a fake).
        ([_badge("badge", policy="redact")], [{"name": "badge", "type": "BADGE2", "pattern": r"\bZZQX-[0-9]{9}\b"}], [], "badge BD-381904 ref ZZQX-123456789", ["ZZQX-123456789"], ["BD-381904"], [("badge", RENAMED)]),
        ([_badge("badge", policy="redact")], [_badge("other", policy="mask")], [], "badge BD-381904", [], ["BD-381904"], [("other", APART)]),
        # The file's of a stricter policy finding nothing, of your type or your name (re-review, Important 3).
        ([_badge("badge")], [_badge("other", NEVER, policy="redact")], [], "badge BD-381904", [], [], [("other", APART)]),
        ([_badge("badge")], [{"name": "badge", "type": "ZZQ", "pattern": NEVER, "policy": "redact"}], [], "badge BD-381904", [], [], [("badge", RENAMED)]),
        # The mirror of N4: yours redacting and narrower, the team's masking and broader (0.9.7: two fakes).
        ([_badge("badge-mine", policy="redact")], [_badge("badge-team", WIDE)], [], BADGES, ["BD-1234567", "BD-123456"], ["BD-123456"], [("badge-team", APART)]),
        # N4 itself: the team's redacting and broader, yours narrower (0.9.7: redacted twice).
        ([_badge("badge-mine")], [_badge("badge-team", WIDE, policy="redact")], [], BADGES, [], ["BD-1234567", "BD-123456"], [("badge-team", APART)]),
        # The file's of the name or the type of a global one of yours (0.9.7: the file's off).
        ([], [_badge("other", WIDE)], [_badge("badge", policy="redact")], BADGES, [], ["BD-123456"], [("other", APART)]),
    ],
    ids=["tie-file-broader", "tie-yours-broader", "tie-redact-file-broader", "tie-redact-yours-broader", "same-name-other-types",
         "yours-off", "yours-broken", "yours-shadowed", "one-of-yours-two-of-the-files", "b2-same-name", "b2-same-type",
         "finds-nothing-same-type", "finds-nothing-same-name", "n4-mirror", "n4", "global"],
)  # fmt: skip
def test_a_files_recognizer_and_yours_of_one_name_or_type_both_apply(
    mine, theirs, top, text, masked_097, redacted, apart
):
    """Ruling 098-R3: of a repository file's recognizer and one of yours (global, or for the folder) of one name or one
    type, both apply: yours as it is, the file's under a name and a type of its own, both types at the stricter of the
    two policies in force. So a project masks at least what 0.9.7 masked (``masked_097``: the values it masked, and
    ``redacted``: the ones it or yours alone redact) and at least what yours alone mask. The file's entries laid apart
    are said, naming no value; one of yours a build does not keep (off, broken, shadowed) counts for nothing."""
    yours = ProjectEntry(path="/work/acme", recognizers=mine)
    base = Config(recognizers=top, projects=[yours])
    alone = _sent(effective_config(base, None, yours)[0], text, SlotKind.USER_TEXT)[0]
    config, notices = effective_config(base, ProjectSettings(recognizers=theirs), yours)
    both = _sent(config, text, SlotKind.USER_TEXT)[0]
    values = re.findall(r"\b[A-Z]+-[0-9]+\b", text)  # whole: BD-123456 is inside BD-1234567
    left_alone, left_both = re.findall(r"\b[A-Z]+-[0-9]+\b", alone), re.findall(r"\b[A-Z]+-[0-9]+\b", both)
    for value in values:
        if value in masked_097 or value in redacted or value not in left_alone:
            assert value not in left_both, (value, both, alone)
    assert both.count("[REDACTED:") >= max(len(redacted), alone.count("[REDACTED:")), (both, alone)
    said = [n for n in notices if n.reason in (APART, RENAMED)]
    assert [(n.key, n.reason) for n in said] == [(f"recognizers.{name}", reason) for name, reason in apart]
    assert all(n.source == "repo" and n.level == "changed" and "BD-" not in n.message for n in said)
    assert all(n.detail is not None and set(n.detail) == {"name", "type"} for n in said)  # the dashboard shows them
    assert len([n for n in notices if n.key.startswith("recognizers.")]) == len(apart)


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "team", "type": "OLDREF", "pattern": r"\bOLD-[0-9]{6}\b", "enabled": False},
        {"name": "team", "type": "OLDREF", "pattern": "("},
        {"name": "team", "type": "OLDREF", "pattern": "[a-z]+"},
    ],
    ids=["off", "broken", "too-broad"],
)
def test_a_global_entry_of_the_files_name_that_a_build_does_not_keep_takes_the_files_recognizer_out_no_more(entry):
    """Round 5 (M1): a build keeps the first of a name whether or not it keeps the entry, so a global entry of yours
    that is off, broken or too broad put the file's recognizer of its name out of use (in 0.9.7 too). The file's is laid
    under a name of its own whenever an earlier global entry holds its name, and applies."""
    yours = ProjectEntry(path="/work/acme")
    config, notices = effective_config(
        Config(recognizers=[entry], projects=[yours]), ProjectSettings(recognizers=[_badge("team", WIDE)]), yours
    )
    masked = _sent(config, BADGES, SlotKind.USER_TEXT)[0]
    assert "BD-1234567" not in masked and "BD-123456" not in re.findall(r"\b[A-Z]+-[0-9]+\b", masked), masked
    assert [(r["name"], r.get("type")) for r in config.recognizers] == [("team", "OLDREF"), ("team-repo", "BADGE")]
    assert [(n.key, n.reason, n.detail) for n in notices] == [
        ("recognizers.team", RENAMED, {"name": "team-repo", "type": "BADGE"})
    ]


def test_the_light_pass_invents_no_type_let_through_and_the_derived_type_takes_no_term_or_field_rule():
    """Round 5 (I1): with BADGE let through by your policies, a BADGE recognizer of yours and the file's, the pass that
    works out the types left in clear laid the file's apart as BADGE_REPO with policy allow, so the file's term or field
    rule typed BADGE_REPO was set aside and left in clear, which 0.9.7 masked; and the derived type took a term of yours
    typed BADGE_REPO, redacted where you mask. A file recognizer whose type's policy in force is allow is left alone
    (the real pass sets it aside), and the derived type is unique against the types terms and field rules use too."""
    mine, team = [_badge("mine")], _badge("team", WIDE)
    yours = ProjectEntry(path="/work/acme", recognizers=mine, policies={"BADGE": "allow"})
    file = ProjectSettings(recognizers=[team], terms=[TermConfig(value="Fonderia Brambilla", type="BADGE_REPO")])
    config, notices = effective_config(Config(projects=[yours]), file, yours)
    assert "Fonderia Brambilla" not in _sent(config, "invoice for Fonderia Brambilla", SlotKind.USER_TEXT)[0]
    assert [(n.key, n.reason) for n in notices] == [("recognizers.team", IN_CLEAR)]
    file = ProjectSettings(recognizers=[team], fields=[FieldRule(name="cliente", type="BADGE_REPO")])
    config, notices = effective_config(Config(profile="max", projects=[yours]), file, yours)
    assert "Fonderia Brambilla" not in _sent(config, '{"cliente": "Fonderia Brambilla", "n": "1"}')[0]
    assert [(n.key, n.reason) for n in notices] == [("recognizers.team", IN_CLEAR)]
    # Your own term typed BADGE_REPO where the team's redacting recognizer is laid apart: the copy takes another type.
    yours = ProjectEntry(
        path="/work/acme", recognizers=mine, terms=[TermConfig(value="Fonderia Brambilla", type="BADGE_REPO")]
    )
    config, _ = effective_config(
        Config(projects=[yours]), ProjectSettings(recognizers=[{**team, "policy": "redact"}]), yours
    )
    assert [(r["name"], r["type"]) for r in config.recognizers] == [("mine", "BADGE"), ("team", "BADGE_REPO_2")]
    masked = _sent(config, "invoice for Fonderia Brambilla " + BADGES, SlotKind.USER_TEXT)[0]
    assert (
        "Fonderia Brambilla" not in masked and "[REDACTED:BADGE_REPO]" not in masked and masked.count("[REDACTED:") == 2
    )


def test_the_files_recognizer_laid_apart_keeps_its_own_name_and_type_but_for_what_yours_take():
    """Laid apart, the file's recognizer takes a name of its own only when yours has its name, and a type of its own only
    when yours has its type (`badge-repo`, `BADGE_REPO`, made unique where taken, by a recognizer or by `policies`),
    and comes after yours, so that yours names a value both find. Of one type, both take the stricter of the two policies
    in force, an entry of `policies` for the type reaching both: the file's carries it as its own, yours by an entry."""
    yours = ProjectEntry(path="/work/acme", recognizers=[{"name": "badge", "type": "MINE", "pattern": NARROW}])
    # Its name taken, its type free: the type stays its own, written out (it was derived from the name).
    file = ProjectSettings(recognizers=[{"name": "badge", "pattern": r"\bZZQX-[0-9]{9}\b"}])
    config, notices = effective_config(Config(projects=[yours]), file, yours)
    assert [(r["name"], r.get("type")) for r in config.recognizers] == [("badge", "MINE"), ("badge-repo", "BADGE")]
    assert [(n.reason, n.detail) for n in notices] == [(RENAMED, {"name": "badge-repo", "type": "BADGE"})]
    assert notices[0].as_dict()["detail"] == {"name": "badge-repo", "type": "BADGE"}
    # Its name and its type taken: the names and types in the list, and the types `policies` names, are never taken.
    top = Config(recognizers=[{"name": "zz", "type": "BADGE_REPO", "pattern": r"\bZZ-[0-9]{4}\b"}], projects=[yours])
    yours = ProjectEntry(
        path="/work/acme", recognizers=[_badge("badge"), {"name": "team", "type": "OTHER", "pattern": EMPID}]
    )
    file = ProjectSettings(
        recognizers=[_badge("team", WIDE, policy="redact"), {"name": "team-repo", "pattern": r"\bZZQX-[0-9]{9}\b"}]
    )
    config, notices = effective_config(top.model_copy(update={"projects": [yours]}), file, yours)
    assert [(r["name"], r.get("type")) for r in config.recognizers] == [
        ("zz", "BADGE_REPO"), ("team-repo", None), ("badge", "BADGE"), ("team", "OTHER"), ("team-repo-2", "BADGE_REPO_2"),
    ]  # fmt: skip
    assert config.recognizers[-1]["policy"] == "redact" and config.policies["BADGE"] == Policy.REDACT
    assert [(n.key, n.reason, n.level) for n in notices] == [("recognizers.team", APART, "changed")]
    assert "team-repo-2" in notices[0].message and "BADGE_REPO_2" in notices[0].message
    assert notices[0].detail == {"name": "team-repo-2", "type": "BADGE_REPO_2"}
    masked = _sent(config, BADGES, SlotKind.USER_TEXT)[0]
    assert masked == "badge [REDACTED:BADGE_REPO_2] and [REDACTED:BADGE]"  # yours names the value both find
    # An entry of `policies` for the type is the policy in force for both, over the file's own.
    file = ProjectSettings(recognizers=[_badge("team", WIDE, policy="redact")])
    yours = ProjectEntry(path="/work/acme", recognizers=[_badge("badge")], policies={"BADGE": "mask"})
    config, _ = effective_config(Config(projects=[yours]), file, yours)
    assert (config.policies["BADGE"], config.recognizers[-1]["policy"]) == (Policy.MASK, "mask")
    assert "BADGE_REPO" not in config.policies and "[REDACTED" not in _sent(config, BADGES, SlotKind.USER_TEXT)[0]
    # A duplicate within the file stays one: the second of a type the file holds twice is off, as it was.
    file = ProjectSettings(recognizers=[_badge("a"), _badge("b", WIDE)])
    yours = ProjectEntry(path="/work/acme", recognizers=[_badge("mine")])
    config, _ = effective_config(Config(projects=[yours]), file, yours)
    assert [(r["name"], r["type"]) for r in config.recognizers] == [
        ("mine", "BADGE"),
        ("a", "BADGE_REPO"),
        ("b", "BADGE_REPO"),
    ]
    assert "BD-1234567" in _sent(config, BADGES, SlotKind.USER_TEXT)[0]


def test_a_repository_files_term_never_narrows_yours_for_its_folder():
    """Found with B2: a folder's terms were added only where no layer before held their value and type, and the file
    comes first: its entry of a value of yours, its case its own, was the one in force, and `ACME CORP` left in clear.
    Both entries apply, as two of one value always do: the file adds forms to yours, never takes one away."""
    yours = ProjectEntry(path="/work/acme", terms=[TermConfig(value="Acme Corp", type="CLIENT")])
    exact = TermConfig(value="Acme Corp", type="CLIENT", case_sensitive=True)
    config, _ = effective_config(Config(projects=[yours]), ProjectSettings(terms=[exact]), yours)
    masked = _sent(config, "ACME CORP and Acme Corp", SlotKind.USER_TEXT)[0]
    assert "ACME CORP" not in masked and "Acme Corp" not in masked
    wider = ProjectEntry(path="/work/acme", terms=[exact])
    config, _ = effective_config(Config(projects=[wider]), ProjectSettings(terms=yours.terms), wider)
    assert "ACME CORP" not in _sent(config, "ACME CORP", SlotKind.USER_TEXT)[0]  # the file's adds a form


CODENAME_ROW = '{"codename": "Bluefin-Harbor-7781", "status": "open"}'


def test_your_global_choice_to_see_a_column_does_not_loosen_a_repository_files_rule():
    """Re-review N1 (Ruling 098-R2): your global `mask: false` for a column a team's latet.yaml masks: 0.9.7 masked it
    in that repository, the order of 8bbdf51b left it in clear on upgrade with no change by anyone. Of your first rule
    that matches and the file's, the stricter applies."""
    mine = Config(profile="max", fields=[FieldRule(name="codename", mask=False)])
    assert "Bluefin-Harbor-7781" in _sent(mine, CODENAME_ROW)[0]
    config, _ = effective_config(mine, ProjectSettings(fields=[FieldRule(name="codename")]), None)
    assert "Bluefin-Harbor-7781" not in _sent(config, CODENAME_ROW)[0]


def test_a_global_rule_added_does_not_turn_a_repository_files_redaction_into_a_fake():
    """Re-review N2: the file redacts `salary` (its type PAY, its policy redact); a global `sal*` added made it a
    reversible fake there, and nothing weighed it as a lowering, so an agent applied it unasked. The stricter applies."""
    pay = '{"salary": "52000", "status": "open"}'
    file = ProjectSettings(fields=[FieldRule(name="salary", type="PAY")], policies={"PAY": "redact"})
    for mine in (Config(profile="max"), Config(profile="max", fields=[FieldRule(name="sal*")])):
        config, _ = effective_config(mine, file, None)
        assert '"salary": "[REDACTED:PAY]"' in _sent(config, pay)[0], mine.fields


def test_in_the_safe_scope_the_stricter_of_every_layer_applies():
    """Re-review N3: on a server, while it cannot tell that you are in a folder, your settings for it are taken as only
    adding (``only_adding``): your folder masks `codename`, your global rules keep it in clear. 0.9.7 masked it there."""
    entry = ProjectEntry(path="C:/work/acme", fields=[FieldRule(name="codename")], policies={"URL": "allow"})
    mine = Config(profile="max", fields=[FieldRule(name="codename", mask=False)], projects=[entry])
    for only_adding in (False, True):
        config, _ = effective_config(mine, None, entry, only_adding=only_adding)
        assert "Bluefin-Harbor-7781" not in _sent(config, CODENAME_ROW)[0], only_adding


def test_your_rules_for_a_project_still_keep_a_column_in_clear_against_your_global_ones():
    """As in 0.9.7: your `mask: false` for a project (the columns panel's *This project*) comes before your global
    rules; a repository file's masking rule for that column still applies, being stricter."""
    mine = Config(profile="max", fields=[FieldRule(name="codename")])
    clear = ProjectEntry(path="/work/acme", fields=[FieldRule(name="codename", mask=False)])
    config, _ = effective_config(mine, None, clear)
    assert "Bluefin-Harbor-7781" in _sent(config, CODENAME_ROW)[0]
    config, _ = effective_config(mine, ProjectSettings(fields=[FieldRule(name="code*")]), clear)
    assert "Bluefin-Harbor-7781" not in _sent(config, CODENAME_ROW)[0]


def test_your_field_rules_win_over_the_repository_files():
    """Ruling 098-R1 (review minor 4): a latet.yaml's field rule came before yours, global or for the folder, and the
    first rule that matches a field wins: `sal*` in the file turned your redacted `salary` into a reversible fake. Yours
    come first; the file's still masks a field yours do not match."""
    row = '{"name": "x", "salary": "52000", "bonus": "7000"}'
    yours = Config(fields=[FieldRule(name="salary", type="SALARY")], policies={"SALARY": "redact"})
    file = ProjectSettings(fields=[FieldRule(name="sal*"), FieldRule(name="bonus", type="BONUS")])
    for mine, entry in (
        (yours, None),
        (Config(policies={"SALARY": "redact"}), ProjectEntry(path="/work/acme", fields=yours.fields)),
    ):
        config, _ = effective_config(mine, file, entry)
        masked = _sent(config, row)[0]
        assert '"salary": "[REDACTED:SALARY]"' in masked and "7000" not in masked, masked


def test_a_repository_files_masked_terms_and_stricter_rules_still_apply():
    """What masks still applies: a term of a type masked there, one of a type the file itself masks over your policy
    (URL let through by you, masked by the file), a field rule that masks, a stricter policy."""
    repo = ProjectSettings(
        terms=[TermConfig(value="Project Falcon", type="PROJECT"), TermConfig(value="Acme Corp", type="URL")],
        fields=[FieldRule(name="salary", type="SALARY")],
        policies={"URL": "mask", "EMAIL": "redact"},
    )
    config, notices = effective_config(Config(policies={"URL": "allow"}), repo, None)
    assert notices == []
    assert [t.value for t in config.terms] == ["Project Falcon", "Acme Corp"]
    assert [f.name for f in config.fields] == ["salary"]
    assert (config.policies["URL"], config.policies["EMAIL"]) == (Policy.MASK, Policy.REDACT)
    masked = _sent(config, "Acme Corp and Project Falcon", SlotKind.USER_TEXT)[0]
    assert "Acme Corp" not in masked and "Project Falcon" not in masked


def test_a_repository_file_cannot_set_a_policy_looser_than_a_recognizers_own():
    """A recognizer's own policy is its type's default: a latet.yaml's `mask` for a type your recognizer redacts made
    its values reversible fakes there, where they were redacted. Weighed against the policy in force, it is ignored."""
    badge = {"name": "badge", "type": "BADGE", "pattern": r"\bBD-\d{6}\b", "policy": "redact"}
    config, notices = effective_config(Config(recognizers=[badge]), ProjectSettings(policies={"BADGE": "mask"}), None)
    assert [n.key for n in notices] == ["policies.BADGE"] and "BADGE" not in config.policies
    assert _sent(config, "badge BD-381904", SlotKind.USER_TEXT)[0] == "badge [REDACTED:BADGE]"


def test_a_repository_file_cannot_make_anything_weaker():
    base = Config(profile="max", policies={"PHONE": "redact"}, include_private_ips=True)
    weaker = ProjectSettings(profile="secrets", policies={"PHONE": "allow"}, include_private_ips=False, ner="off")
    config, notices = effective_config(base, weaker, None)
    assert {n.key for n in notices} == {"profile", "policies.PHONE", "include_private_ips", "ner"}
    assert (config.profile.value, config.policies["PHONE"], config.include_private_ips, config.ner) == (
        "max",
        Policy.REDACT,
        True,
        "auto",
    )


def test_your_own_settings_may_loosen_and_add():
    personal = ProjectEntry(
        path="/p", profile="secrets", allow_values=["acme-dev"], policies={"EMAIL": "allow"},
        terms=[TermConfig(value="Acme", type="CLIENT")],
    )  # fmt: skip
    # A pattern that does not compile is refused where settings are read (tests/unit/test_patterns.py): one held anyway
    # is left out where they are applied.
    personal.allow_patterns = ["ok-[0-9]+", "(unclosed"]
    config, notices = effective_config(Config(), ProjectSettings(profile="max"), personal)
    assert config.profile.value == "secrets"  # after the repository's: yours win
    assert config.allow_values == ["acme-dev"] and config.policies["EMAIL"] == Policy.ALLOW
    assert config.allow_patterns == ["ok-[0-9]+"]
    assert [n.key for n in notices] == ["allow_patterns"]  # the broken pattern is reported, not applied


def test_a_term_of_signs_alone_is_left_out_and_said(tmp_path):
    root = _repo(tmp_path, "terms: [{value: '--'}, {value: Project Falcon, type: PROJECT}]\n")
    settings, _ = read_project_file(root / PROJECT_FILE)
    personal = ProjectEntry(path="/p", terms=[TermConfig(value="###"), TermConfig(value="Acme", type="CLIENT")])
    config, more = effective_config(Config(), settings, personal)
    assert [t.value for t in config.terms] == ["Project Falcon", "Acme"]
    assert [(n.source, n.key, n.level) for n in more] == [("repo", "terms", "error"), ("personal", "terms", "error")]
    assert "--" in more[0].message and "a letter or a digit" in more[0].message and "###" in more[1].message


def test_a_broken_or_huge_file_has_no_effect(tmp_path):
    root = _repo(tmp_path, "profile: [max\n")
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is None and notices[0].level == "error"
    (root / PROJECT_FILE).write_text("terms:\n" + "".join(f"  - {{value: Term {i}}}\n" for i in range(600)), "utf-8")
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is not None and len(settings.terms) == 500
    assert any(n.key == "terms" and n.level == "error" for n in notices)
    (root / PROJECT_FILE).write_bytes(b"#" * (300 * 1024))
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is None and "KB" in notices[0].message
    # Aliases are refused: a few hundred bytes of nested ones would expand to gigabytes.
    bomb = "a: &a [x, x]\n" + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)}, *{chr(97 + i)}]\n" for i in range(20)
    )
    (root / PROJECT_FILE).write_text(bomb, "utf-8")
    settings, notices = read_project_file(root / PROJECT_FILE)
    assert settings is None and "aliases" in notices[0].message


def test_the_written_file_keeps_only_what_it_may_set():
    text = render_project_file(ProjectSettings(profile="max", allow_values=["x"], id_regions=["FR"]))
    assert text.startswith("# yaml-language-server: $schema=https://docs.latet.ai/project.schema.json")
    assert "profile: max" in text and "FR" in text and "allow_values" not in text


def _engine(config=None, files=True):
    config = config or Config()
    vault = Vault(ROOT_KEY)
    built = build(config, vault)
    projects = Projects(config, vault, files=files)
    built.engine.scopes = projects
    return built.engine, projects, built.verifier


def test_two_projects_mask_the_same_text_by_their_own_rules(tmp_path):
    acme = _repo(tmp_path, "terms: [{value: Project Falcon, type: PROJECT}]\n", "acme")
    other = _repo(tmp_path, None, "other")
    engine, projects, _ = _engine()
    first, second = engine.session("s1"), engine.session("s2")
    projects.attach(first, str(acme / "src"))
    projects.attach(second, str(other))
    text = "status of Project Falcon?"
    assert engine.mask_text(text, SlotKind.USER_TEXT, second).text == text
    assert "Project Falcon" not in engine.mask_text(text, SlotKind.USER_TEXT, first).text  # same text, own rules
    assert projects.scope_for(second) is None  # nothing of its own: the global rules apply as they are
    # Masked once, a value is in the vault, and the vault masks it everywhere from then on.
    assert "Project Falcon" not in engine.mask_text(text + " ", SlotKind.USER_TEXT, second).text


def test_latet_off_in_a_project_masks_nothing_and_still_restores(tmp_path):
    root = _repo(tmp_path)
    config = Config(projects=[ProjectEntry(path=str(root), enabled=False)])
    engine, projects, verifier = _engine(config)
    on, off = engine.session("on"), engine.session("off")
    masked = engine.mask_text("mail a.b@acme.it", SlotKind.USER_TEXT, on).text
    projects.attach(off, str(root))
    assert engine.mask_text("mail a.b@acme.it", SlotKind.USER_TEXT, off).text == "mail a.b@acme.it"
    # A surrogate already in its conversation is still registered, so it is restored.
    engine.mask_text(masked, SlotKind.USER_TEXT, off, keep_surrogates=True)
    assert off.entity_ids
    result = verifier.verify([("mail a.b@acme.it", SlotKind.USER_TEXT, False)], "off", scope=engine.scope(off))
    assert result.blocking == []
    assert verifier.verify([("mail a.b@acme.it", SlotKind.USER_TEXT, False)], "on").blocking  # elsewhere it blocks


def test_a_changed_file_applies_at_once_and_new_rules_reach_every_project(tmp_path, monkeypatch):
    root = _repo(tmp_path, "terms: [{value: Falcon, type: PROJECT}]\n")
    engine, projects, _ = _engine()
    session = engine.session("s")
    projects.attach(session, str(root))
    assert "Falcon" not in engine.mask_text("Falcon and Heron", SlotKind.USER_TEXT, session).text
    (root / PROJECT_FILE).write_text("terms: [{value: Heron, type: PROJECT}]\n", "utf-8")
    os.utime(root / PROJECT_FILE, ns=(time.time_ns(), time.time_ns() + 10_000_000_000))
    projects.touch(session.project_id)
    assert "Heron" not in engine.mask_text("Falcon and Heron!", SlotKind.USER_TEXT, session).text
    projects.use(Config(allow_values=["Heron"]), None)  # new global rules: the project's are rebuilt
    assert "Heron" in engine.mask_text("Falcon and Heron!!", SlotKind.USER_TEXT, session).text


def test_a_space_past_its_share_builds_no_new_project_masker_and_builds_it_once_under_it():
    """Each project of yours that changes anything is a masker of its own in your space, holding the global rules again:
    two hundred of them grew a space past its share of a server's memory. Past it, the space builds no new one: the
    texts of a folder whose masker is not built are refused with the share's words, never masked by the global rules,
    which would leave the folder's own terms in clear, and the final check refuses alike; a folder whose masker is built
    goes on as before. Back under its share, the folder's masker is built at its next use."""
    falcon, heron = (
        TermConfig(value="Project Falcon", type="CODENAME"),
        TermConfig(value="Project Heron", type="CODENAME"),
    )
    other = "/home/emily/heron"
    config = Config(projects=[ProjectEntry(path=FALCON, terms=[falcon]), ProjectEntry(path=other, terms=[heron])])
    engine, projects, verifier = _engine(config, files=False)
    past: list[str | None] = [None]
    projects.past_share = lambda _weight: past[0]
    built = engine.session("cc:built")
    projects.attach(built, FALCON)  # built while the space is under its share
    past[0] = "this identity's space holds its share of the memory this instance of the server gives the spaces"
    refused = engine.session("cc:refused")
    project = projects.attach(refused, other)
    assert project is not None and [n.message for n in project.notices if n.level == "error"] == [past[0]]
    # with a reason the dashboard words in its reader's language (RP6)
    assert [n.as_dict().get("reason") for n in project.notices if n.level == "error"] == ["space_past_share"]
    with pytest.raises(MaskingError, match="share of the memory"):
        engine.mask_text("the Project Heron launch is on Friday", SlotKind.USER_TEXT, refused)
    with pytest.raises(MaskingError, match="share of the memory"):
        verifier.verify([("Project Heron", SlotKind.USER_TEXT, False)], "cc:refused", scope=engine.scope(refused))
    assert "Project Falcon" not in engine.mask_text("the Project Falcon launch", SlotKind.USER_TEXT, built).text
    past[0] = None  # values forgotten: under its share again
    assert "Project Heron" not in engine.mask_text("the Project Heron launch", SlotKind.USER_TEXT, refused).text


def test_a_projects_masker_is_weighed_whole_under_the_lock_before_it_is_kept():
    """RP3: a masker is weighed under the projects' lock, with every masker built before it: by its rules before it is
    built, then whole, its terms' matcher too (a megabyte at least on a server, which its rules do not say), before it
    is kept; one that would carry the space past its share is let go. What a weighing read before, outside the lock,
    is never what decides: two clients at once built 38 maskers of 40 so. At room for two and a half maskers two are
    kept, the third folder's texts are refused with the share's words, and its masker is built once it fits."""
    folders = [f"/home/emily/repo-{n}" for n in range(3)]
    terms = [[TermConfig(value=f"Codename {n}", type="CODENAME")] for n in range(3)]
    config = Config(projects=[ProjectEntry(path=folder, terms=terms[n]) for n, folder in enumerate(folders)])
    _, measured, _ = _engine(config, files=False)
    measured.resolve(folders[0])
    one = measured._weigh()  # its projects, one masker built
    engine, projects, _ = _engine(config, files=False)
    words = "this identity's space holds its share of the memory this instance of the server gives the spaces"
    room = [5 * one // 2]
    projects.past_share = lambda weight: words if weight > room[0] else None
    sessions = [engine.session(f"cc:{n}") for n in range(3)]
    for session, folder in zip(sessions, folders, strict=True):
        projects.attach(session, folder)
    assert len(projects._scopes) == 2
    with pytest.raises(MaskingError, match="share of the memory"):
        engine.mask_text("Codename 2 ships on Friday", SlotKind.USER_TEXT, sessions[2])
    room[0] = 4 * one  # values forgotten: room for it
    assert "Codename 2" not in engine.mask_text("Codename 2 ships on Friday", SlotKind.USER_TEXT, sessions[2]).text
    assert len(projects._scopes) == 3


def test_a_masker_kept_counts_at_once_in_the_weight_read_while_the_projects_lock_stays_busy():
    """The residuals' check: a value kept is weighed against its space's projects as ``weight`` answers, which never
    waits for their lock and, while it is busy, answers what it found last. Builds hand the lock from one to the next,
    so through a burst of builds a masking call weighed its values against the projects of before the burst, and could
    keep past the share by the maskers built in it. Each masker counts in that answer as it is kept."""
    folders = [f"/home/emily/repo-{n}" for n in range(2)]
    terms = [[TermConfig(value=f"Codename {n}", type="CODENAME")] for n in range(2)]
    config = Config(projects=[ProjectEntry(path=folder, terms=terms[n]) for n, folder in enumerate(folders)])
    _, projects, _ = _engine(config, files=False)
    before = projects.weight
    projects.resolve(folders[0])  # a masker built and kept
    busy, done = threading.Event(), threading.Event()

    def building():  # the burst's next build, holding the lock
        with projects._lock:
            busy.set()
            done.wait(5)

    builder = threading.Thread(target=building)
    builder.start()
    try:
        assert busy.wait(5)
        seen = projects.weight  # what a value kept now is weighed against, the lock busy
    finally:
        done.set()
        builder.join(5)
    assert seen >= before + sum(projects._masker_weights.values()) > before


@pytest.mark.parametrize("files", [True, False], ids=["a-computer", "a-server"])
def test_a_project_whose_rules_cannot_be_built_stops_its_requests_and_never_falls_back(monkeypatch, files):
    """A project whose rules cannot be built (a term no RE2 budget compiles, say) never runs on the global rules, which
    would leave its own terms in clear: its texts are refused until its rules can be built, and the dashboard says why
    (S-03). The global rules elsewhere are untouched."""
    from latet import projects as projects_module

    term = TermConfig(value="Project Falcon", type="CODENAME")
    config = Config(projects=[ProjectEntry(path=FALCON, terms=[term])])
    engine, projects, verifier = _engine(config, files)
    real = projects_module.build_masker

    def failing(rules, *args, **kwargs):
        if any(t.value == "Project Falcon" for t in rules.terms):
            raise MemoryError("the project's terms do not compile")  # as an alternation past RE2_MEMORY would
        return real(rules, *args, **kwargs)

    monkeypatch.setattr(projects_module, "build_masker", failing)
    session = engine.session("cc:s1")
    project = projects.attach(session, FALCON)
    assert project is not None and project.scope is not None
    assert [n.message for n in project.notices if n.level == "error"] == [
        "its rules could not be built: its texts are refused until they can be"
    ]
    # With a reason the dashboard words in its reader's language (E0c's Task 11), as a space past its share is.
    assert [n.as_dict().get("reason") for n in project.notices if n.level == "error"] == ["project_unbuilt"]
    with pytest.raises(MaskingError):
        engine.mask_text("the Project Falcon launch is on Friday", SlotKind.USER_TEXT, session)
    with pytest.raises(MaskingError):  # the final check refuses alike: nothing of it leaves
        verifier.verify([("Project Falcon", SlotKind.USER_TEXT, False)], "cc:s1", scope=engine.scope(session))
    elsewhere = engine.session("cc:s2")
    assert engine.mask_text("mail a.b@acme.it", SlotKind.USER_TEXT, elsewhere).text != "mail a.b@acme.it"


def test_the_repositories_a_computer_has_seen_are_weighed_with_a_change_of_its_settings(tmp_path):
    """What ``Projects.repositories`` lists is what a change of the global settings is weighed in too: a repository's
    own latet.yaml at max, defeated by one line written at the top as the global profile has it. A server reads no
    repository file, and lists none."""
    root = _repo(tmp_path, "profile: max\n", "build-tools")
    _, projects, _ = _engine()
    projects.resolve(str(root / "src"))
    [(folder, settings)] = projects.repositories()
    assert folder == str(root) and settings.profile == "max"
    assert loosened(Config(), Config(include_private_ips=False), projects.repositories()) == [
        "project build-tools: private IP addresses: no longer masked"
    ]
    _, on_a_server, _ = _engine(files=False)
    on_a_server.resolve(str(root / "src"))
    assert on_a_server.repositories() == ()


def _remembering(tmp_path):
    """A computer's projects that remember the repositories they see in ``tmp_path/data``, as a daemon's start makes
    them (each start makes them anew); and their list."""
    seen = SeenRepositories(LocalRepositories(tmp_path / "data"))
    return Projects(Config(), Vault(ROOT_KEY), seen=seen), seen


def test_a_repository_seen_is_weighed_once_the_daemon_has_started_again_its_file_read_again(tmp_path):
    """R3-4's residual: a repository seen before the daemon last started is weighed with a change of its settings
    though no conversation has reached it since, with its latet.yaml as it is now. In the process that saw it, it is
    weighed once while its file is as it was worked out, and also as it is now once it changed."""
    root = _repo(tmp_path, "profile: max\n", "build-tools")
    projects, _ = _remembering(tmp_path)
    projects.resolve(str(root / "src"))
    assert [folder for folder, _ in projects.repositories()] == [str(root)]  # read again the same: weighed once
    (root / PROJECT_FILE).write_text("profile: max\npolicies: {EMAIL: redact}\n", "utf-8")  # changed since
    assert [settings.policies for _, settings in projects.repositories()] == [{}, {"EMAIL": Policy.REDACT}]
    again, _ = _remembering(tmp_path)  # the daemon started again
    assert again.count == 0  # no conversation in it yet
    [(folder, settings)] = again.repositories()
    assert folder == str(root) and settings.profile == "max" and settings.policies == {"EMAIL": Policy.REDACT}
    assert loosened(Config(), Config(include_private_ips=False), again.repositories()) == [
        "project build-tools: private IP addresses: no longer masked"
    ]


def test_a_repository_seen_after_a_start_joins_those_seen_before_it(tmp_path):
    """Each start remembers what the starts before it saw: a repository seen later is the most recent, and none seen
    before is lost."""
    first = _repo(tmp_path, "profile: max\n", "first")
    second = _repo(tmp_path, "profile: max\n", "second")
    projects, _ = _remembering(tmp_path)
    projects.resolve(str(first))
    again, _ = _remembering(tmp_path)  # the daemon started again
    again.resolve(str(second))
    assert LocalRepositories(tmp_path / "data").load() == [str(second), str(first)]


def test_the_list_of_repositories_is_read_again_at_every_reload(tmp_path):
    """What the list holds that this process does not know (another start's, written meanwhile) is weighed too."""
    strict = _repo(tmp_path, "profile: max\n", "strict")
    projects, _ = _remembering(tmp_path)
    assert projects.repositories() == ()
    LocalRepositories(tmp_path / "data").save([str(strict)])
    assert [folder for folder, _ in projects.repositories()] == [str(strict)]


def test_a_folder_or_latet_yaml_not_there_now_stays_in_the_list_and_is_weighed_again_once_back(tmp_path):
    """Nothing missing drops an entry: a folder deleted, or its latet.yaml gone with a branch checked out, has nothing to
    weigh now and stays; back, it is weighed again with no conversation needed. Only the list's bounds let one go."""
    kept = _repo(tmp_path, "profile: max\n", "kept")
    gone = _repo(tmp_path, "profile: max\n", "gone")
    emptied = _repo(tmp_path, "profile: max\n", "emptied")
    projects, seen = _remembering(tmp_path)
    for root in (kept, gone, emptied):
        projects.resolve(str(root))
    shutil.rmtree(gone)
    (emptied / PROJECT_FILE).unlink()
    assert [folder for folder, _ in seen.weighed()] == [str(kept)]
    assert LocalRepositories(tmp_path / "data").load() == [str(emptied), str(gone), str(kept)]
    again, _ = _remembering(tmp_path)  # the daemon started again
    assert [folder for folder, _ in again.repositories()] == [str(kept)]
    _repo(tmp_path, "profile: max\n", "gone")  # cloned again
    (emptied / PROJECT_FILE).write_text("profile: max\n", "utf-8")  # its branch checked out again
    assert [folder for folder, _ in again.repositories()] == [str(emptied), str(gone), str(kept)]


def _link_folder(target, link):
    """``link``, a link to the folder ``target``: a junction on Windows, which takes no privilege, a symbolic link
    elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_link_is_followed_and_an_entry_with_nothing_to_weigh_stays_while_the_others_are_weighed(
    tmp_path, monkeypatch
):
    """The list is a security input, which another program may have written. An entry that is a link (a junction, a
    symbolic link) is weighed through it, as a conversation reaching the repository through it is worked out: a link
    reaches nothing an entry could not name, and a repository file only adds protection. An entry whose latet.yaml is not
    a regular file, is larger than a project file may be, is not valid or nests past what Python reads has nothing to
    weigh, and neither of the first two is opened. Never a reason to weigh none of the others, and each stays."""
    strict = _repo(tmp_path, "profile: max\n", "strict")
    elsewhere = _repo(tmp_path, "profile: max\n", "elsewhere")
    linked = tmp_path / "linked"
    _link_folder(elsewhere, linked)
    a_folder = _repo(tmp_path, None, "a-folder")
    (a_folder / PROJECT_FILE).mkdir()
    large = _repo(tmp_path, "profile: max\n" + "#" * MAX_FILE_BYTES, "large")
    broken = _repo(tmp_path, "profile: [max\n", "broken")
    nested = _repo(tmp_path, "[" * 100_000, "nested")
    entries = [str(p) for p in (linked, a_folder, large, broken, nested, strict)]
    LocalRepositories(tmp_path / "data").save(entries)
    opened, real = [], os.open

    def recording(path, flags, *args, **kwargs):
        opened.append(Path(path))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording)
    projects, _ = _remembering(tmp_path)
    assert [folder for folder, _ in projects.repositories()] == [str(linked), str(strict)]
    assert not any(path.is_relative_to(p) for path in opened for p in (a_folder, large))  # neither opened
    assert LocalRepositories(tmp_path / "data").load() == entries  # each stays


def test_a_latet_yaml_reached_through_a_link_is_read_through_it(tmp_path, monkeypatch):
    """Followed as the file itself: lstat answers for this latet.yaml as for a symbolic link (Windows refuses a real one
    to a test without a privilege; the POSIX test below makes one), and the file is read through it."""
    strict = _repo(tmp_path, "profile: max\n", "strict")
    real_lstat = os.lstat

    def as_a_link(path, *args, **kwargs):
        found = real_lstat(path, *args, **kwargs)
        if Path(path) == strict / PROJECT_FILE:
            return os.stat_result((stat.S_IFLNK | 0o777, *tuple(found)[1:]))
        return found

    monkeypatch.setattr(os, "lstat", as_a_link)
    LocalRepositories(tmp_path / "data").save([str(strict)])
    projects, _ = _remembering(tmp_path)
    assert [folder for folder, _ in projects.repositories()] == [str(strict)]


def _refusing_the_list(monkeypatch, path):
    """The list at ``path`` there and refused to every read, as a sharing violation or a permission refuses it; a way to
    let it be read again."""
    from latet.state import local as local_module

    real = local_module.own_file

    def refused(target, most, **kwargs):
        if Path(target) == path:
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real(target, most, **kwargs)

    monkeypatch.setattr(local_module, "own_file", refused)
    return lambda: monkeypatch.setattr(local_module, "own_file", real)


def test_a_list_that_cannot_be_read_now_is_never_written_over_and_is_merged_once_it_can(tmp_path, monkeypatch, caplog):
    """A list that is there and cannot be read now (refused at the start and again when a repository is seen) is left
    whole, said by its file and the error's type, the folder kept in memory for this run; once it can be read, the next
    folder seen is written with the list's folders and the one kept in memory (before: the folder seen replaced every
    folder remembered)."""
    first, second, third = (_repo(tmp_path, "profile: max\n", name) for name in ("first", "second", "third"))
    store = LocalRepositories(tmp_path / "data")
    store.save([str(first)])
    before = store.path.read_bytes()
    readable = _refusing_the_list(monkeypatch, store.path)
    projects, _ = _remembering(tmp_path)
    projects.resolve(str(second))
    assert store.path.read_bytes() == before
    said = [r.getMessage() for r in caplog.records if r.name == "latet.state.local"]
    assert said and all(str(store.path) in s and "PermissionError" in s and "left as it is" in s for s in said)
    assert not any(name in caplog.text for name in ("first", "second"))
    readable()
    projects.resolve(str(third))
    assert store.load() == [str(third), str(first), str(second)]


def test_a_list_that_cannot_be_read_at_a_reload_is_weighed_as_read_at_the_start(tmp_path, monkeypatch):
    strict = _repo(tmp_path, "profile: max\n", "strict")
    store = LocalRepositories(tmp_path / "data")
    store.save([str(strict)])
    projects, _ = _remembering(tmp_path)  # read at the start
    _refusing_the_list(monkeypatch, store.path)  # refused at the reload
    assert [folder for folder, _ in projects.repositories()] == [str(strict)]


def test_the_entries_with_nothing_to_weigh_are_said_by_count_and_kind_never_by_folder(tmp_path, caplog):
    strict = _repo(tmp_path, "profile: max\n", "strict")
    away = tmp_path / "payroll-away"  # never made: a drive not there now
    broken = _repo(tmp_path, "profile: [max\n", "payroll-broken")
    LocalRepositories(tmp_path / "data").save([str(away), str(broken), str(strict)])
    caplog.set_level(logging.INFO, logger="latet.projects")
    projects, _ = _remembering(tmp_path)
    assert [folder for folder, _ in projects.repositories()] == [str(strict)]
    [record] = [r for r in caplog.records if r.name == "latet.projects"]
    said = record.getMessage()
    assert record.levelno == logging.INFO and said.startswith("2 of the 3 repositories Latet has seen")
    assert "FileNotFoundError" in said and "not valid" in said and "payroll" not in caplog.text


def test_what_reading_a_remembered_file_should_never_raise_is_said_by_its_type_and_stops_no_other(
    tmp_path, caplog, monkeypatch
):
    """One hostile file must not stop a start: an error the reading of a repository's file was never meant to raise
    leaves it nothing to weigh, said by the error's type, never its words or the folder; the others are weighed."""
    from latet import projects as projects_module

    strict = _repo(tmp_path, "profile: max\n", "strict")
    odd = _repo(tmp_path, "profile: max\n# odd\n", "payroll-odd")
    real = projects_module._project_settings

    def failing(raw):
        if b"# odd" in raw:
            raise LookupError("payroll-odd")
        return real(raw)

    monkeypatch.setattr(projects_module, "_project_settings", failing)
    LocalRepositories(tmp_path / "data").save([str(odd), str(strict)])
    projects, _ = _remembering(tmp_path)
    assert [folder for folder, _ in projects.repositories()] == [str(strict)]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "LookupError" in warning.getMessage() and "payroll" not in caplog.text


def test_a_conversation_in_a_repository_whose_latet_yaml_nests_too_deep_is_answered(tmp_path):
    """Such a file is read as one that is not valid YAML, as read_project_file promises, never raising: the plugin's
    call is answered and masked by the rules in use (before: RecursionError, and a 500)."""
    from starlette.testclient import TestClient

    from latet.config import IdentityConfig
    from latet.gateway.app import create_app
    from latet.runtime import make_runtime

    nested = _repo(tmp_path, "[" * 100_000, "nested")
    settings, notices = read_project_file(nested / PROJECT_FILE)
    assert settings is None and [n.message for n in notices] == ["not valid YAML: nested too deep"]
    rt = make_runtime(Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data",
                      config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)  # fmt: skip
    try:
        items = [{"text": "write to emily.carter@acme.com", "kind": "user_text"}]
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283", raise_server_exceptions=False) as c:
            answer = c.post("/_latet/engine/v1/mask", json={"session": "s1", "cwd": str(nested), "items": items},
                            headers={"x-latet-token": rt.gateway.bridge_token})  # fmt: skip
        assert answer.status_code == 200 and "emily.carter@acme.com" not in answer.text
    finally:
        rt.close()


def test_only_a_folder_given_whole_is_remembered(tmp_path, monkeypatch):
    """A folder a client reports relative to where it runs is no folder the daemon could find again: it is not
    remembered (its project is weighed while this process remembers it, as before)."""
    _repo(tmp_path, "profile: max\n", "relative")
    monkeypatch.chdir(tmp_path)
    projects, seen = _remembering(tmp_path)
    projects.resolve("relative")
    assert seen.weighed() == () and LocalRepositories(tmp_path / "data").load() == []
    assert [folder for folder, _ in projects.repositories()] == ["relative"]


@pytest.mark.skipif(os.name == "nt", reason="symbolic links take a privilege on Windows, and it has no named pipes")
def test_a_latet_yaml_that_is_a_link_is_weighed_and_one_that_is_a_pipe_is_never_opened(tmp_path, monkeypatch):
    """A remembered repository's latet.yaml made a symbolic link to a file elsewhere is weighed through it; one that is
    a named pipe, which a read would wait on for ever, has nothing to weigh and is never opened."""
    strict = _repo(tmp_path, "profile: max\n", "strict")
    linked = _repo(tmp_path, None, "linked")
    os.symlink(strict / PROJECT_FILE, linked / PROJECT_FILE)
    piped = _repo(tmp_path, None, "piped")
    os.mkfifo(piped / PROJECT_FILE)
    LocalRepositories(tmp_path / "data").save([str(linked), str(piped), str(strict)])
    opened, real = [], os.open

    def recording(path, flags, *args, **kwargs):
        opened.append(Path(path))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording)
    projects, _ = _remembering(tmp_path)
    assert [folder for folder, _ in projects.repositories()] == [str(linked), str(strict)]
    assert piped / PROJECT_FILE not in opened


@pytest.mark.skipif(os.name == "nt", reason="Windows has no named pipes")
def test_a_pipe_put_in_a_latet_yamls_place_after_its_look_is_never_read(tmp_path, monkeypatch):
    """What took the file's place between its look and its opening is checked again once open: a pipe there is opened
    without waiting for a writer and never read."""
    piped = _repo(tmp_path, None, "piped")
    os.mkfifo(piped / PROJECT_FILE)
    real_stat = os.stat

    def a_file_when_looked_at(path, *args, **kwargs):
        found = real_stat(path, *args, **kwargs)
        if Path(path) == piped / PROJECT_FILE:
            return os.stat_result((stat.S_IFREG | 0o644, *tuple(found)[1:6], 10, *tuple(found)[7:]))
        return found

    monkeypatch.setattr(os, "stat", a_file_when_looked_at)
    LocalRepositories(tmp_path / "data").save([str(piped)])
    projects, _ = _remembering(tmp_path)
    from latet.state.local import own_file

    with _pipe_read_watchdog():
        # Keep the primitive witness: a supervisor's earlier timeout must not hide a blocking open.
        assert own_file(piped / PROJECT_FILE, MAX_FILE_BYTES, follow=True) is None
        # Then the supervised check: the pipe is known not to be a regular file, nothing to weigh, and the snapshot
        # complete (a read stuck past the helper's deadline would leave it incomplete, never empty and complete).
        scan = projects.repositories()
        assert scan == () and scan.complete


class _PipeReadDeadline(BaseException):
    """Outside the repository reader's broad Exception recovery."""


@contextmanager
def _pipe_read_watchdog():
    """A local POSIX guard; the mutation runner also owns an outer 15-second process deadline."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("the pipe watchdog needs the POSIX main thread")
    previous = signal.getsignal(signal.SIGALRM)
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    if 0 < remaining <= 5:
        pytest.skip("an existing tighter alarm owns this thread")
    started = time.monotonic()

    def expired(signum, frame):
        raise _PipeReadDeadline

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            yield
        except _PipeReadDeadline:
            pytest.fail("repository pipe read exceeded its local five-second watchdog", pytrace=False)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining - (time.monotonic() - started)), interval)


@pytest.mark.skipif(os.name == "nt", reason="the local watchdog is POSIX-only")
@pytest.mark.parametrize("outcome", ["success", "deadline", "reader-error"])
def test_the_local_pipe_watchdog_restores_prior_signal_and_elapsed_timer(monkeypatch, outcome):
    prior = object()
    handlers, timers = [], []
    ticks = iter((100.0, 101.5))
    monkeypatch.setattr(signal, "getsignal", lambda kind: prior)
    monkeypatch.setattr(signal, "getitimer", lambda kind: (9.0, 3.0))
    monkeypatch.setattr(signal, "signal", lambda kind, handler: handlers.append(handler))
    monkeypatch.setattr(signal, "setitimer", lambda kind, delay, interval=0: timers.append((delay, interval)))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    expected = pytest.fail.Exception if outcome == "deadline" else ValueError
    if outcome == "success":
        with _pipe_read_watchdog():
            pass
    else:

        def failing_read():
            with _pipe_read_watchdog():
                if outcome == "deadline":
                    handlers[0](signal.SIGALRM, None)
                raise ValueError("reader failed")

        with pytest.raises(expected):
            failing_read()
    assert handlers[-1] is prior
    assert timers == [(5, 0), (0, 0), (7.5, 3.0)]


@pytest.mark.skipif(os.name == "nt", reason="the local watchdog is POSIX-only")
def test_the_local_pipe_watchdog_does_not_replace_a_tighter_timer(monkeypatch):
    monkeypatch.setattr(signal, "getsignal", lambda kind: signal.SIG_DFL)
    monkeypatch.setattr(signal, "getitimer", lambda kind: (0.25, 0))

    def changed(*args):
        pytest.fail("a tighter timer was replaced")

    monkeypatch.setattr(signal, "signal", changed)
    monkeypatch.setattr(signal, "setitimer", changed)
    with pytest.raises(pytest.skip.Exception), _pipe_read_watchdog():
        pytest.fail("a tighter timer was ignored")


def test_the_repositories_remembered_are_the_most_recent(tmp_path, monkeypatch):
    """At most ``REPOSITORIES_KEPT`` (2,000), here two: the least recently seen goes, from the list and from what the
    process weighs, and comes back once seen again."""
    from latet import projects as projects_module
    from latet.state import local as local_module

    monkeypatch.setattr(projects_module, "REPOSITORIES_KEPT", 2)
    monkeypatch.setattr(local_module, "REPOSITORIES_KEPT", 2)
    roots = [_repo(tmp_path, "profile: max\n", name) for name in ("first", "second", "third")]
    projects, seen = _remembering(tmp_path)
    for root in roots:
        projects.resolve(str(root))
    assert [folder for folder, _ in seen.weighed()] == [str(roots[2]), str(roots[1])]
    assert LocalRepositories(tmp_path / "data").load() == [str(roots[2]), str(roots[1])]
    projects.use(Config(), None)  # new settings: each project is worked out again at its next use
    projects.resolve(str(roots[0]))
    assert [folder for folder, _ in seen.weighed()] == [str(roots[0]), str(roots[2])]


def test_a_space_on_a_server_weighs_no_list_of_repositories(tmp_path):
    """A server reads no repository file: a list in its data folder, naming a stricter repository of the server's own
    disk, is weighed by none of its spaces."""
    from dataclasses import replace

    from latet.runtime import make_runtime
    from latet.state import local_backends

    from .server.conftest import EMILY, SHARED, MemorySettings

    strict = _repo(tmp_path, "profile: max\n", "strict")
    LocalRepositories(tmp_path / "models").save([str(strict)])
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=MemorySettings(SHARED))
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    try:
        assert rt.projects is not None and rt.projects.repositories() == ()
    finally:
        rt.close()


def test_identical_rules_share_one_masker(tmp_path):
    a = _repo(tmp_path, "profile: max\n", "a")
    b = _repo(tmp_path, "profile: max\n", "b")
    _, projects, _ = _engine()
    assert projects.resolve(str(a)).scope.masker is projects.resolve(str(b)).scope.masker


def test_your_settings_for_a_folder_replace_the_old_ones():
    config = with_personal(Config(), "C:/dev/acme", ProjectEntry(path="ignored", allow_values=["x"]))
    config = with_personal(config, "c:\\dev\\acme", ProjectEntry(path="ignored", allow_values=["y"]))
    assert [(e.path, e.allow_values) for e in config.projects] == [("c:\\dev\\acme", ["y"])]
    assert with_personal(config, "C:/dev/acme", None).projects == []


def test_the_working_directory_from_codex_opencode_or_a_header():
    codex = {
        "model": "m",
        "instructions": "You are Codex.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n  "
             "<cwd>C:\\dev\\acme</cwd>\n  <approval_policy>on-request</approval_policy>\n</environment_context>"}]},
        ],
    }  # fmt: skip
    assert cwd_from({}, openai_responses.scan_request(codex)) == "C:\\dev\\acme"
    opencode = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "Here is some useful information:\n<env>\n  Working directory: /home/me/acme\n"
             "  Is directory a git repo: yes\n</env>"},
            {"role": "user", "content": "hi"},
        ],
    }  # fmt: skip
    assert cwd_from({}, openai_chat.scan_request(opencode)) == "/home/me/acme"
    assert cwd_from({"x-latet-project": "/srv/app"}, openai_chat.scan_request(opencode)) == "/srv/app"
    assert cwd_from({}, openai_chat.scan_request({"messages": [{"role": "user", "content": "hi"}]})) is None


def test_the_name_model_is_loaded_once(tmp_path, monkeypatch):
    from latet.engine.detectors import ner

    monkeypatch.setattr(ner, "runtime_available", lambda: True)
    monkeypatch.setattr(ner, "is_installed", lambda data: True)
    first, second = ner.detector_for(tmp_path, "balanced"), ner.detector_for(tmp_path, "max")
    assert first is not None and second is not None and first.model is second.model


FALCON = "/home/emily/billing-api"


def _from_elsewhere(session, folder):
    """``session`` given its project and folder as another instance of a Latet server kept them (in Valkey): no client
    reported the folder to this process."""
    session.project_id, session.cwd = project_id(folder), folder


@pytest.mark.parametrize("files", [True, False], ids=["a-computer", "a-server"])
@pytest.mark.parametrize("reported", [False, True], ids=["the-proxys-path", "the-plugins-call"])
def test_a_conversation_whose_project_another_instance_resolved_keeps_its_rules_here(reported, files):
    """This process never resolved the folder: the project's rules apply all the same, whether a client reports the
    folder here first (the plugin's call: resolved there and then) or not (the proxy's path: resolved when used). On a
    server, which reads no project file, alike."""
    config = Config(projects=[ProjectEntry(path=FALCON, terms=[TermConfig(value="Project Falcon", type="CODENAME")])])
    engine, projects, _ = _engine(config, files)
    session = engine.session("cc:s1")
    _from_elsewhere(session, FALCON)
    if reported:
        project = projects.attach(session, FALCON)
        assert project is not None and project.id == session.project_id == project_id(FALCON)
    text = "the Project Falcon launch is on Friday"
    assert "Project Falcon" not in engine.mask_text(text, SlotKind.USER_TEXT, session).text


@pytest.mark.parametrize("files", [True, False], ids=["a-computer", "a-server"])
def test_what_a_project_lowers_applies_once_a_client_reports_its_folder_and_what_it_adds_at_once(files):
    """A project of yours that adds a term and lets an address through: taken from elsewhere (on a server, from
    Valkey), the term is masked and the address too; once a client reports the folder, the address goes through. On a
    computer a client always reported it: a project's settings apply as they say. A server reads no project file, and
    this holds there alike: only a client's report sets the folder reported (``Session.reported``)."""
    address = "emily.carter@gmail.com"  # a provider's domain: the vault keeps no entry for it beside the address
    term = TermConfig(value="Project Falcon", type="CODENAME")
    config = Config(projects=[ProjectEntry(path=FALCON, terms=[term], allow_values=[address])])
    engine, projects, _ = _engine(config, files)
    session = engine.session("cc:s1")
    _from_elsewhere(session, FALCON)
    masked = engine.mask_text(f"Project Falcon: write to {address}", SlotKind.USER_TEXT, session).text
    assert "Project Falcon" not in masked and address not in masked and session.reported is None
    projects.attach(session, FALCON)
    masked = engine.mask_text(f"Project Falcon: write to {address}!", SlotKind.USER_TEXT, session).text
    assert "Project Falcon" not in masked and address in masked
    engine, projects, _ = _engine(Config(projects=[ProjectEntry(path=FALCON, enabled=False)]), files)
    session = engine.session("cc:s2")
    _from_elsewhere(session, FALCON)
    assert address not in engine.mask_text(f"write to {address}", SlotKind.USER_TEXT, session).text  # not off yet
    projects.attach(session, FALCON)
    assert engine.mask_text(f"write to {address}", SlotKind.USER_TEXT, session).text == f"write to {address}"


@pytest.mark.parametrize("files", [True, False], ids=["a-computer", "a-server"])
def test_a_folders_live_reviewer_applies_before_a_client_reports_the_folder_though_the_folder_lowers_something(files):
    """Your settings for a folder turn the Live Reviewer on there and let an address through: taken from elsewhere (on
    a server, from Valkey), what the folder lowers waits for a client to report it, and the Live Reviewer, which only
    adds protection, applies at once (not the global rules', off); a folder's Live Reviewer turned off waits like the
    rest of what it lowers."""
    address = "emily.carter@gmail.com"
    entry = ProjectEntry(path=FALCON, live_review=True, allow_values=[address])
    engine, projects, _ = _engine(Config(projects=[entry]), files)
    session = engine.session("cc:s1")
    _from_elsewhere(session, FALCON)
    scope = projects.scope_for(session)
    assert scope is not None and scope.live is not None and scope.live.enabled
    assert address not in engine.mask_text(f"write to {address}", SlotKind.USER_TEXT, session).text
    projects.attach(session, FALCON)
    scope = projects.scope_for(session)
    assert scope is not None and scope.live is not None and scope.live.enabled
    assert address in engine.mask_text(f"write to {address}!", SlotKind.USER_TEXT, session).text
    on = Config(live_review={"enabled": True}, projects=[ProjectEntry(path=FALCON, live_review=False)])
    engine, projects, _ = _engine(on, files)
    session = engine.session("cc:s2")
    _from_elsewhere(session, FALCON)
    scope = projects.scope_for(session)
    assert scope is None or (scope.live is not None and scope.live.enabled)  # the global rules': on
    projects.attach(session, FALCON)
    scope = projects.scope_for(session)
    assert scope is not None and scope.live is not None and not scope.live.enabled


def test_on_a_server_a_folder_is_its_own_project_and_your_settings_for_a_folder_around_it_apply(tmp_path):
    """A server has none of a computer's folders: the folder a client reports is the project, whatever a folder of the
    server of that name holds (no repository root, no latet.yaml looked for), and your settings for a folder around it
    apply there."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "latet.yaml").write_text("terms: [{value: Orion}]\n", "utf-8")
    (tmp_path / "src").mkdir()
    config = Config(projects=[ProjectEntry(path=str(tmp_path), profile="max")])
    _, projects, _ = _engine(config, files=False)
    project = projects.resolve(str(tmp_path / "src"))
    assert project.root == str(tmp_path / "src") and project.repo_file is None and project.repo is None
    assert project.config.profile == "max" and [t.value for t in project.config.terms] == []


@pytest.mark.parametrize("cwd", ["", "   ", None, 42])
def test_no_working_directory_leaves_the_global_rules(cwd):
    _, projects, _ = _engine()
    engine = Engine(Vault(ROOT_KEY), build(Config(), Vault(ROOT_KEY)).masker)
    session = engine.session("s")
    assert projects.attach(session, cwd) is None and session.project_id is None


def test_verifier_clean_cache_is_kept_apart_per_project(tmp_path):
    root = _repo(tmp_path, "terms: [{value: Falcon, type: PROJECT}]\n")
    engine, projects, verifier = _engine()
    strict = engine.session("strict")
    projects.attach(strict, str(root))
    slots = [("Falcon flies", SlotKind.USER_TEXT, False)]
    assert verifier.verify(slots, "plain").blocking == []  # no such rule globally: clean
    assert verifier.verify(slots, "strict", scope=engine.scope(strict)).blocking  # the project's term
    assert isinstance(verifier, Verifier)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    from latet.cli import admin

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)
    return tmp_path


def _cli(*args, input=None):
    from typer.testing import CliRunner

    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def test_project_init_check_show_and_off(isolated):
    from latet.config import load_config

    root = _repo(isolated)
    assert _cli("project", "init", str(root / "src")).exit_code == 0
    assert (root / PROJECT_FILE).read_text("utf-8").startswith("# yaml-language-server")
    assert _cli("project", "init", str(root)).exit_code == 1  # never over an existing file
    assert _cli("project", "check", str(root)).exit_code == 0  # only comments: all is well
    (root / PROJECT_FILE).write_text("profile: max\nallow_values: [x]\n", "utf-8")
    checked = _cli("project", "check", str(root))
    assert checked.exit_code == 1 and "allow_values" in checked.output
    shown = _cli("project", "show", str(root / "src" / "deep"), "--json")
    assert '"profile": "max"' in shown.output
    assert '"data": "strict"' in shown.output  # max brings strict data mode with it
    assert "data:          strict" in _cli("project", "show", str(root)).output
    assert _cli("project", "off", str(root)).exit_code == 0
    [entry] = load_config().projects
    assert entry.enabled is False and norm_path(entry.path) == norm_path(str(root))
    assert "OFF" in _cli("project", "show", str(root)).output
    assert _cli("project", "on", str(root)).exit_code == 0
    assert load_config().projects == []  # nothing left of your settings: the entry goes


def test_rules_add_to_a_project_or_its_repository(isolated):
    from latet.config import load_config
    from latet.projects import read_project_file

    root = _repo(isolated)
    rule = isolated / "rule.yaml"
    rule.write_text(
        "name: badge\npattern: 'BD-[0-9]{6}'\nexamples:\n  match: ['BD-123456']\n  no_match: ['BD-12']\n", "utf-8"
    )
    assert _cli("rules", "add", str(rule), "--repo", str(root)).exit_code == 0
    settings, _ = read_project_file(root / PROJECT_FILE)
    assert [r["name"] for r in settings.recognizers] == ["badge"]
    assert load_config().recognizers == []  # the global ones are untouched
    assert _cli("rules", "add", str(rule), "--project", str(root)).exit_code == 0
    assert [r["name"] for r in load_config().projects[0].recognizers] == ["badge"]
    listed = _cli("rules", "list", "--repo", str(root))
    assert "badge" in listed.output and "active" in listed.output
    assert _cli("rules", "remove", "badge", "--repo", str(root)).exit_code == 0
    assert read_project_file(root / PROJECT_FILE)[0].recognizers == []
    assert _cli("rules", "add", str(rule), "--repo", str(root), "--project", str(root)).exit_code == 2


def test_rules_add_to_a_repository_refuses_a_recognizer_that_lets_what_it_finds_through(isolated):
    """A latet.yaml cannot hold a recognizer whose own policy is allow (it is set aside there): `latet rules add
    --repo` says so and writes nothing; into your own settings for the project, it goes."""
    from latet.config import load_config

    root = _repo(isolated)
    rule = isolated / "rule.yaml"
    rule.write_text(
        "name: quiet\ntype: QUIET\npattern: 'QT-[0-9]{6}'\npolicy: allow\n"
        "examples:\n  match: ['QT-123456']\n  no_match: ['QT-12']\n",
        "utf-8",
    )
    refused = _cli("rules", "add", str(rule), "--repo", str(root))
    assert refused.exit_code == 2 and "quiet" in refused.output and "--project" in refused.output
    assert not (root / PROJECT_FILE).exists()
    assert _cli("rules", "add", str(rule), "--project", str(root)).exit_code == 0
    assert [r["name"] for r in load_config().projects[0].recognizers] == ["quiet"]


def test_rules_add_to_a_repository_refuses_a_recognizer_of_a_type_your_policies_let_through(isolated):
    """Review B1: a recognizer with no policy of its own, of a type your policies let through, would leave what it finds
    in clear in the repository (set aside there): `latet rules add --repo` refuses it, and writes nothing."""
    from latet.config import save_config

    root = _repo(isolated)
    save_config(Config(policies={"INTERNAL_ID": "allow"}), Path(os.environ["LATET_CONFIG"]))
    rule = isolated / "rule.yaml"
    rule.write_text(
        "name: amount\ntype: INTERNAL_ID\npattern: 'AM-[0-9]{6}'\nexamples:\n  match: ['AM-123456']\n  no_match: ['AM-12']\n",
        "utf-8",
    )
    refused = _cli("rules", "add", str(rule), "--repo", str(root))
    assert refused.exit_code == 2 and "amount" in refused.output and "--project" in refused.output
    assert not (root / PROJECT_FILE).exists()


def test_the_project_schema_lists_only_what_the_file_may_set():
    from latet.schema import project_schema

    schema = project_schema()
    assert schema["additionalProperties"] is False
    assert "allow_values" not in schema["properties"] and "profile" in schema["properties"]
    assert _cli("config", "schema", "--project").output.startswith("{")
