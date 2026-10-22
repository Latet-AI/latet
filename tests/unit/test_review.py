"""Claude Code's auto mode reviewer: per-detail settings, what each value found is, what goes through."""

import json

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from latet import wrappers
from latet.config import AutoModeReview, Config, IdentityConfig, detect_identity, load_config, save_config
from latet.doctor.checks import user_name_checks
from latet.engine.build import build
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.terms import IdentityDetector, LocalIdentity
from latet.engine.review import ReviewPolicy, block_reason, detail
from latet.engine.types import EntityClass, SlotKind, Span
from latet.engine.vault import Vault

from ..conftest import ROOT_KEY

GH_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
IDENTITY = LocalIdentity(
    usernames=("jdoe",), full_name="Jane Doe", emails=("jane.doe@studio-doe.it",), hostname="WS-JDOE-01"
)
REVIEW = "User identity: `jdoe`\nHome: /home/jdoe\nAuthor: Jane Doe <jane.doe@studio-doe.it> on WS-JDOE-01"


BLOCK_ALL = {"user_name": "block", "paths": "block", "host_name": "block", "git_author": "block"}
ALLOW_ALL = dict.fromkeys(BLOCK_ALL, "allow")


@pytest.mark.parametrize(
    ("written", "read", "saved"),
    [
        ("block", BLOCK_ALL, "block"),  # files written before the per-detail form look the same after a save
        ("allow-identity", ALLOW_ALL, "allow-identity"),
        ({"paths": "allow"}, BLOCK_ALL | {"paths": "allow"}, BLOCK_ALL | {"paths": "allow"}),
        (ALLOW_ALL, ALLOW_ALL, "allow-identity"),
        (
            ALLOW_ALL | {"user_name": "surrogate"},
            ALLOW_ALL | {"user_name": "surrogate"},
            ALLOW_ALL | {"user_name": "surrogate"},
        ),
    ],
)
def test_review_settings_keep_the_short_forms(tmp_path, written, read, saved):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"auto_mode_review": written}), "utf-8")
    config = load_config(path)
    assert {d: getattr(config.auto_mode_review, d) for d in BLOCK_ALL} == read
    save_config(config, path)
    assert yaml.safe_load(path.read_text("utf-8"))["auto_mode_review"] == saved


def test_review_settings_reject_unknown_values():
    assert Config().model_dump(mode="json")["auto_mode_review"] == "block"
    surrogate = Config.model_validate({"auto_mode_review": {"user_name": "surrogate", "paths": "allow"}})
    assert surrogate.auto_mode_review.allowed() == {"paths"}  # a surrogate user name: the real one still blocks
    for bad in ("maybe", {"user": "allow"}, {"paths": "surrogate"}):
        with pytest.raises(ValidationError):
            Config.model_validate({"auto_mode_review": bad})


def _spans(text: str) -> list[Span]:
    return list(IdentityDetector(IDENTITY).detect(text, DetectContext(SlotKind.SYSTEM)))


def test_short_user_name_in_the_reviewers_user_identity_line():
    found = {(REVIEW[s.start : s.end], s.rule) for s in _spans(REVIEW)}
    assert ("jdoe", "identity:user-identity") in found
    assert ("jdoe", "identity:home-path") in found
    assert not _spans("ask jdoe about it")  # elsewhere a short name is a common word


def test_each_value_is_classified_as_its_detail():
    by_text = {(REVIEW[s.start : s.end], detail(REVIEW, s)) for s in _spans(REVIEW)}
    assert by_text >= {
        ("jdoe", "user_name"),
        ("jdoe", "paths"),
        ("Jane Doe", "git_author"),
        ("jane.doe@studio-doe.it", "git_author"),
        ("studio-doe.it", "git_author"),  # the domain of your email, wherever it appears
        ("WS-JDOE-01", "host_name"),
    }
    # A user name found as a known value is a path too when it follows a home folder (JSON escaped here).
    text = r'{"cwd": "C:\\Users\\jdoe\\project"}'
    start = text.index("jdoe")
    assert detail(text, Span(start, start + 4, "USERNAME", EntityClass.IDENTITY, "known-values")) == "paths"
    assert detail(text, Span(start, start + 4, "EMAIL", EntityClass.PII, "pii:email")) is None


def test_a_value_inside_another_belongs_to_it_but_a_secret_never_goes_through():
    text = "Author: Jane Doe <jdoe@studio-doe.it> " + GH_PAT
    email = text.index("jdoe@")
    spans = [
        Span(email, email + 18, "EMAIL", EntityClass.IDENTITY, "identity:email"),
        Span(email, email + 18, "EMAIL", EntityClass.PII, "pii:email"),  # the same email, as personal data
        Span(email, email + 4, "USERNAME", EntityClass.IDENTITY, "known-values"),  # the user name inside it
        Span(8, 16, "PERSON", EntityClass.IDENTITY, "identity:person"),
        Span(email, email + 8, "ASSIGNED_SECRET", EntityClass.SECRET, "made-up"),
        Span(len(text) - 40, len(text), "GITHUB_PAT", EntityClass.SECRET, "gitleaks:github-pat"),
    ]
    policy = ReviewPolicy(frozenset({"git_author"}))
    details = policy.details(text, spans)
    assert details == ["git_author", "git_author", "git_author", "git_author", None, None]
    assert policy.let_through(details) == [True, True, True, True, False, False]
    assert not any(ReviewPolicy(frozenset({"user_name"})).let_through(details))


def test_verifier_lets_allowed_details_through_and_never_secrets():
    identity = IdentityConfig(
        usernames=list(IDENTITY.usernames), full_name=IDENTITY.full_name, emails=list(IDENTITY.emails),
        hostname=IDENTITY.hostname,
    )  # fmt: skip
    verifier = build(Config(), Vault(ROOT_KEY), identity).verifier
    slots = [(REVIEW, SlotKind.SYSTEM, False)]
    everything = ReviewPolicy(frozenset({"user_name", "paths", "host_name", "git_author"}))
    assert verifier.verify(slots, "s1", ReviewPolicy()).blocking
    result = verifier.verify(slots, "s2", everything)
    assert result.leaks and not result.blocking
    assert all(leak.allowed for leak in result.leaks)
    with_secret = [(REVIEW + f"\nexport T={GH_PAT}", SlotKind.SYSTEM, False)]
    assert [leak.etype for leak in verifier.verify(with_secret, "s3", everything).blocking] == ["GITHUB_PAT"]
    partial = verifier.verify(slots, "s4", ReviewPolicy(frozenset({"user_name"})))
    blocking = {(leak.detail, leak.eclass) for leak in partial.blocking}
    assert {d for d, _ in blocking} == {"paths", "git_author", "host_name"}
    assert ("git_author", EntityClass.PII) in blocking  # the email as personal data: part of the git author


def test_block_reason_names_the_details_and_the_keys():
    one = block_reason(ReviewPolicy(), [("user_name", "USERNAME[4]")])
    assert one.startswith("auto mode's reviewer would receive your user name, so auto mode cannot approve this action")
    assert "To let it through to the reviewer, set auto_mode_review: {user_name: allow}" in one
    two = block_reason(ReviewPolicy(), [("paths", "USERNAME[4]"), ("user_name", "USERNAME[4]")])
    assert "your user name and your user folder in paths" in two and "{user_name: allow, paths: allow}" in two
    secret = block_reason(ReviewPolicy(), [(None, "GITHUB_PAT[40]"), ("host_name", "HOSTNAME[10]")])
    assert "receive GITHUB_PAT[40] and your host name" in secret and "To let your host name through" in secret
    surrogate = block_reason(ReviewPolicy(surrogate_user_name=True), [("user_name", "USERNAME[4]")])
    assert "latet agents apply claude" in surrogate and "user_name: allow" not in surrogate


def test_detect_identity_does_not_trust_the_environment(monkeypatch):
    monkeypatch.setenv("USERNAME", "kestrel42")
    monkeypatch.setenv("USER", "kestrel42")
    assert "kestrel42" not in detect_identity().usernames


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for name, sub in (("LATET_DATA_DIR", "data"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", "t-123")
    return tmp_path


def test_global_install_takes_the_details_to_let_through(isolated, monkeypatch):
    import latet.cli.admin  # noqa: F401 - registers global-install
    from latet.cli.main import app
    from latet.service import daemon

    monkeypatch.setattr(daemon, "start", lambda: daemon.Status(False, port=5283))
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=5283))
    runner = CliRunner()
    args = ["global-install", "--yes", "--no-identity", "--agents", "none", "--no-ner", "--no-autostart"]
    ok = runner.invoke(app, [*args, "--auto-mode-review", "user-name=surrogate,paths"])
    assert ok.exit_code == 0, ok.output
    saved = yaml.safe_load((isolated / "config.yaml").read_text("utf-8"))["auto_mode_review"]
    assert saved == {"user_name": "surrogate", "paths": "allow", "host_name": "block", "git_author": "block"}
    assert runner.invoke(app, [*args, "--auto-mode-review", "allow-identity"]).exit_code == 0
    assert yaml.safe_load((isolated / "config.yaml").read_text("utf-8"))["auto_mode_review"] == "allow-identity"
    bad = runner.invoke(app, [*args, "--auto-mode-review", "paths=surrogate"])
    assert bad.exit_code != 0 and "user_name=surrogate" in bad.output


def test_claude_code_gets_the_surrogate_user_name(isolated, monkeypatch):
    from latet.integrations.agents import ClaudeCode, ClaudeOptions

    save_config(Config(auto_mode_review=AutoModeReview(user_name="surrogate")), isolated / "config.yaml")
    monkeypatch.setattr(ClaudeCode, "_claude", lambda self, *args: {"outcome": "ok"})
    monkeypatch.setattr(wrappers, "user_name_surrogate", lambda config=None, **_kwargs: "mirko71")
    settings = isolated / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"env": {"MY_VAR": "1"}}), "utf-8")
    claude = ClaudeCode()
    record = claude.apply(ClaudeOptions())
    env = json.loads(settings.read_text("utf-8"))["env"]
    assert env["USERNAME"] == env["USER"] == "mirko71" and env["MY_VAR"] == "1"
    assert record.data["user_name_surrogate"] is True
    data = json.loads(settings.read_text("utf-8"))
    data["outputStyle"] = "Concise"  # edited since: revert removes only what Latet set
    settings.write_text(json.dumps(data), "utf-8")
    claude.revert()
    assert json.loads(settings.read_text("utf-8")) == {"env": {"MY_VAR": "1"}, "outputStyle": "Concise"}
    monkeypatch.setattr(wrappers, "user_name_surrogate", lambda config=None, **_kwargs: None)  # no daemon
    assert claude.apply(ClaudeOptions()).data["user_name_surrogate"] is False
    assert "USERNAME" not in json.loads(settings.read_text("utf-8"))["env"]
    [warning] = user_name_checks(claude)  # doctor says so
    assert warning.status == "warn" and "latet agents apply claude" in warning.detail
    save_config(Config(), isolated / "config.yaml")
    assert user_name_checks(claude) == []
