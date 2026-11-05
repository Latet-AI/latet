"""The rules a request is decided under (``gateway/request_policy.py``): built-in rules and exceptions (P2-6)."""

from __future__ import annotations

import ast
import base64
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from latet.config import Config, IdentityConfig, ProjectEntry, RequestException
from latet.gateway import outcome, request_policy, request_profiles
from latet.project_file import REPO_KEYS, repo_settings
from latet.protection import loosened
from latet.server.shared import OWN_KEYS

from ..conftest import ROOT_KEY

SOURCE = Path(__file__).resolve().parents[2] / "src" / "latet"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
# A header a user set for an agent (Claude Code's ANTHROPIC_CUSTOM_HEADERS), refused naming it until named here (D4).
TEAM = {"destination": "llm-acme", "profile": "openai.chat.1", "reason": "custom-header"}


def _exception(**fields) -> RequestException:
    return RequestException.model_validate({**TEAM, "components": [{"kind": "header", "name": "x-team"}], **fields})


def _approved(**fields) -> RequestException:
    stamps = {"id": "x0123456789", "granted": NOW, "revision": request_profiles.REVISION, "issuer": "owner"}
    return _exception(**{**stamps, **fields})


def _with(*rows: RequestException) -> Config:
    return Config(identity=IdentityConfig(enabled=False), request_exceptions=list(rows))


# --- who may set one: its owner's settings alone (R-20) -------------------------------------------------------------


def test_an_exception_is_its_owners_setting_alone():
    assert "request_exceptions" in Config.model_fields
    assert "request_exceptions" not in REPO_KEYS  # a repository file never adds one
    settings, notices = repo_settings(
        {"request_exceptions": [{**TEAM, "components": [{"kind": "header", "name": "x"}]}]}
    )
    assert [(n.key, n.level) for n in notices] == [("request_exceptions", "ignored")]
    assert settings == repo_settings({})[0]
    assert "request_exceptions" in OWN_KEYS  # on a server, each identity's own, never the shared settings'


@pytest.mark.parametrize(
    "component",
    [
        {"kind": "header", "name": "X-Team"},  # a header's name in lower case
        {"kind": "header", "name": "x-*"},  # no wildcard
        {"kind": "header", "name": "x-latet-launch-key"},  # Latet's own
        {"kind": "header", "name": "host"},  # framing
        {"kind": "header", "name": "a" * 65},
        {"kind": "query", "name": "a b"},
        {"kind": "body", "name": "/messages/0"},  # a body member names the type of its value (body_selectors)
    ],
)
def test_an_exception_names_each_component_exactly(component):
    with pytest.raises(ValidationError):
        _exception(components=[component])


def test_an_exception_is_bounded():
    with pytest.raises(ValidationError):
        _exception(components=[{"kind": "header", "name": f"x-h{i}"} for i in range(65)])
    with pytest.raises(ValidationError):
        _exception(components=[{"kind": "header", "name": "x-team"}, {"kind": "header", "name": "x-team"}])
    with pytest.raises(ValidationError):
        _exception(description="a\nb")
    with pytest.raises(ValidationError):
        _exception(id="x0123456789")  # Latet's stamps come together, as it approves
    with pytest.raises(ValidationError):
        _with(*[_exception(project="/repo") for _ in range(33)])
    with pytest.raises(ValidationError):
        _with(*[_exception() for _ in range(129)])


# --- how long it lasts (D3 as the owner changed it) -----------------------------------------------------------------


def test_an_exception_expires_after_30_days_by_default_and_90_at_most():
    assert _approved().expires() == NOW + timedelta(days=30)
    assert _approved(days=90).expires() == NOW + timedelta(days=90)
    for days in (0, 91):
        with pytest.raises(ValidationError):
            _exception(days=days)
    assert _approved(permanent=True).expires() is None
    assert not _exception().approved and _exception().expires() is None  # pending: no lifetime, no authority


def test_a_moved_destination_says_its_exceptions_go_with_it():
    """Minor 9: an exception names its destination, so it follows the destination to a new address: the move's
    approval says so."""
    from latet.config import Destination

    before = _with(_approved()).model_copy(
        update={"upstreams": [Destination(name="llm-acme", url="https://a.example")]}
    )
    moved = before.model_copy(update={"upstreams": [Destination(name="llm-acme", url="https://b.example")]})
    assert loosened(before, moved) == [
        "destination llm-acme: its address changed, and the request exception that names it goes with it"
    ]
    two = _approved(id="x1111111111", components=[{"kind": "header", "name": "x-cost"}])
    both = before.model_copy(update={"request_exceptions": [_approved(), two]})
    assert loosened(both, moved.model_copy(update={"request_exceptions": [_approved(), two]})) == [
        "destination llm-acme: its address changed, and the 2 request exceptions that name it go with it"
    ]
    alone = before.model_copy(update={"request_exceptions": []})
    assert loosened(alone, moved.model_copy(update={"request_exceptions": []})) == [
        "destination llm-acme: its address changed"
    ]


def test_a_permanent_exception_is_chosen_on_purpose_at_approval():
    assert RequestException.model_fields["permanent"].default is False  # never a default
    assert "permanent" not in REPO_KEYS
    assert loosened(_with(_approved()), _with(_approved(permanent=True))) != []  # a loosening: it waits


CREDENTIAL = [{"kind": "header", "name": "x-acme-pass", "role": "credential"}]


@pytest.mark.parametrize(
    ("before", "after", "words"),
    [
        pytest.param((), (_exception(),), "added for 30 days", id="added"),
        pytest.param((), (_exception(days=90),), "added for 90 days", id="added-90-days"),
        pytest.param((), (_exception(permanent=True),), "added, permanent", id="added-permanent"),
        pytest.param(
            (),
            (_exception(components=CREDENTIAL, reason="destination-credential"),),
            "added for 30 days, lets a credential through unread",
            id="added-credential",
        ),
        pytest.param(
            (),
            (_exception(components=CREDENTIAL, reason="destination-credential", permanent=True),),
            "added, permanent, lets a credential through unread",
            id="added-permanent-credential",
        ),
        pytest.param(
            (_approved(),),
            (_approved(components=[{"kind": "header", "name": "x-team", "role": "credential"}]),),
            "widened for 30 days, lets a credential through unread",
            id="widened-to-credential",
        ),
        pytest.param((_approved(),), (_approved(permanent=True),), "made permanent", id="made-permanent"),
        pytest.param((_approved(),), (_approved(days=60),), "renewed for 60 days", id="renewed"),
    ],
)
def test_the_approval_says_what_is_approved(before, after, words):
    """I-2, D3: what you approve is said as you approve it: a permanent exception never passes as an ordinary one, nor
    one that lets a credential through unread, nor its lifetime; by category alone, never the name it gives."""
    assert loosened(_with(*before), _with(*after)) == [f"a request exception for llm-acme: {words}"]


# --- what is a loosening: adding, widening, renewing, making permanent (D3, R-108) ------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param((), (_exception(),), id="added"),
        pytest.param((_approved(),), (_approved(), _exception(destination="openai")), id="another-added"),
        pytest.param(
            (_approved(),),
            (_approved(components=[{"kind": "header", "name": "x-team"}, {"kind": "header", "name": "x-cost"}]),),
            id="widened-component",
        ),
        pytest.param(
            (_approved(components=[{"kind": "header", "name": "x-team"}]),),
            (_approved(components=[{"kind": "header", "name": "x-team", "role": "credential"}]),),
            id="widened-role",
        ),
        pytest.param((_approved(project="/repo"),), (_approved(),), id="project-to-identity"),
        pytest.param((_approved(),), (_approved(profile="openai.responses.1"),), id="another-profile"),
        pytest.param((_approved(),), (_approved(days=60),), id="renewed-longer"),
        pytest.param((_approved(),), (_approved(granted=NOW + timedelta(days=1)),), id="renewed-later"),
        pytest.param((_approved(),), (_approved(permanent=True),), id="made-permanent"),
        pytest.param((_approved(),), (_approved(revision="http-envelope-0"),), id="revision-rewritten"),
        pytest.param((_approved(),), (_approved(id="x9999999999"),), id="id-rewritten"),
        pytest.param((_approved(),), (_approved(issuer="administration"),), id="issuer-rewritten"),
    ],
)
def test_exception_add_widen_renew_requires_existing_owner_approval(before, after):
    reasons = loosened(_with(*before), _with(*after))
    assert reasons and all("request exception" in reason for reason in reasons)
    assert all("x-team" not in reason and "x-cost" not in reason for reason in reasons)  # never what it names


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param((_approved(),), (), id="removed"),
        pytest.param(
            (_approved(components=[{"kind": "header", "name": "x-team"}, {"kind": "header", "name": "x-cost"}]),),
            (_approved(),),
            id="narrowed",
        ),
        pytest.param((_approved(days=60),), (_approved(),), id="shortened"),
        pytest.param((_approved(permanent=True),), (_approved(),), id="no-longer-permanent"),
        pytest.param((_approved(),), (_approved(project="/repo"),), id="identity-to-project"),
        pytest.param((_approved(),), (_approved(description="for the team's gateway"),), id="described"),
    ],
)
def test_removing_narrowing_or_shortening_an_exception_applies_at_once(before, after):
    assert loosened(_with(*before), _with(*after)) == []


# --- pending, rejected and approved: only an approved one authorizes (R-20) -------------------------------------------


def test_only_an_approved_exception_in_its_lifetime_is_in_force():
    pending, approved = _exception(), _approved()
    expired = _approved(id="x1111111111", granted=NOW - timedelta(days=31))
    permanent = _approved(id="x2222222222", granted=NOW - timedelta(days=400), permanent=True)
    revised = _approved(id="x3333333333", revision="http-envelope-0")
    found = request_policy.in_force(_with(pending, approved, expired, permanent, revised), NOW)
    assert [row.id for row in found] == [approved.id, permanent.id]


def _runtime(tmp_path, monkeypatch, config: Config):
    from latet.runtime import make_runtime

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    return make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")


def test_an_exception_added_waits_for_approval_and_is_stamped_by_latet(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, Config(identity=IdentityConfig(enabled=False), ner="off"))
    try:
        runtime.exception_clock = lambda: NOW
        # Hand-written stamps are caller input: Latet replaces them with its own as you approve.
        wanted = runtime.config.model_copy(update={"request_exceptions": [_approved(id="x5555555555")]})
        change = runtime.propose(wanted, "file")
        assert change is not None and any("request exception" in reason for reason in change.loosened)
        assert request_policy.in_force(runtime.config, NOW) == ()  # pending: nothing in force
        runtime.exception_clock = lambda: NOW + timedelta(hours=1)
        assert runtime.decide_change(change.id, approve=True) == "approved"
        [row] = runtime.config.request_exceptions
        assert row.approved and row.id != "x5555555555" and row.granted == NOW + timedelta(hours=1)
        assert row.issuer == "owner"  # who approved it, recorded by Latet, never taken from the settings written
        assert row.revision == request_profiles.REVISION
        assert request_policy.in_force(runtime.config, NOW + timedelta(hours=2)) == (row,)
        # Removing it needs no approval and applies at once.
        assert runtime.propose(runtime.config.model_copy(update={"request_exceptions": []}), "file") is None
    finally:
        runtime.close()


def test_a_rejected_exception_authorizes_nothing(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, Config(identity=IdentityConfig(enabled=False), ner="off"))
    try:
        wanted = runtime.config.model_copy(update={"request_exceptions": [_exception()]})
        change = runtime.propose(wanted, "file")
        assert change is not None
        assert runtime.decide_change(change.id, approve=False) == "rejected"
        assert runtime.config.request_exceptions == [] and request_policy.in_force(runtime.config, NOW) == ()
    finally:
        runtime.close()


def test_stamps_with_no_approval_behind_them_wait_for_you(tmp_path, monkeypatch):
    """Minor 5: at a start with no settings you approved (a computer's first, or a snapshot that cannot be read), an
    exception carrying Latet's stamps (written by hand: permanent, say, or approved in the future) is not taken as
    approved: it waits for you as a new one, and grants nothing until you approve it, as Latet then stamps it."""
    stamped = _approved(permanent=True, granted=NOW - timedelta(days=1), issuer="administration")
    runtime = _runtime(tmp_path, monkeypatch, _with(stamped).model_copy(update={"ner": "off"}))
    try:
        assert request_policy.in_force(runtime.config, NOW) == ()
        [pending] = runtime.config.request_exceptions  # R-3: no stamp of its own left, nor who it says approved it
        assert (pending.id, pending.granted, pending.revision, pending.issuer) == (None, None, None, None)
        [change] = list(runtime.mediator.approvals.changes.values())
        assert any("request exception for llm-acme: added, permanent" in reason for reason in change.loosened)
        runtime.exception_clock = lambda: NOW
        assert runtime.decide_change(change.id, approve=True) == "approved"
        [row] = runtime.config.request_exceptions
        assert row.approved and row.id != stamped.id and row.granted == NOW and row.issuer == "owner"
    finally:
        runtime.close()


def test_a_project_scoped_exception_names_a_canonical_folder():
    assert _exception(project="C:/Users/u/repo/").project == r"C:\Users\u\repo"
    assert ProjectEntry(path="/repo/").path == _exception(project="/repo/").project


# The symbols of Phase 1's internal rule that named 0.10's behaviour (RC-REC-1): P2-6 deletes every one of them.
_LEGACY_SYMBOLS = ("LegacyEvidence", "legacy_evidence", "LEGACY")


def test_no_legacy_rule_remains():
    assert "legacy-0.10" not in request_policy.BUILT_IN
    assert not any(hasattr(module, name) for module in (outcome, request_policy) for name in _LEGACY_SYMBOLS)
    assert not hasattr(outcome, "legacy")  # no decision rests on 0.10's behaviour any more
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text("utf-8")
        if "legacy-0.10" in text:
            found.append(f"{path.relative_to(SOURCE)}: legacy-0.10")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(node, ast.FunctionDef | ast.ClassDef)
                else None
            )
            if name in _LEGACY_SYMBOLS or (name == "legacy" and isinstance(node, ast.FunctionDef | ast.Attribute)):
                found.append(f"{path.relative_to(SOURCE)}:{getattr(node, 'lineno', 0)}: {name}")
            if isinstance(node, ast.alias) and re.fullmatch(r"legacy|LEGACY|legacy_evidence|LegacyEvidence", node.name):
                found.append(f"{path.relative_to(SOURCE)}:{node.lineno}: import {node.name}")
    assert found == []


# --- visible: listed by state, warned before expiry, suspended by a profile revision (D3, R-100) ----------------------


def _states(*rows: RequestException, now: datetime = NOW) -> dict[str | None, str]:
    return {row["id"]: row["state"] for row in request_policy.listed(_with(*rows), now)}


def test_an_exception_warns_seven_days_before_it_expires():
    row = _approved()
    assert _states(row, now=NOW + timedelta(days=22))[row.id] == "active"
    [listed] = request_policy.listed(_with(row), NOW + timedelta(days=23, hours=1))
    assert listed["state"] == "expiring" and listed["days_left"] == 6
    assert _states(row, now=NOW + timedelta(days=30))[row.id] == "expired"


def test_a_listed_exception_says_its_scope_category_never_its_description_or_what_it_names():
    rows = (_approved(description="for the acme team"), _approved(id="x2222222222", project="/repo", permanent=True))
    listed = request_policy.listed(_with(*rows), NOW)
    assert [(row["scope"], row["permanent"]) for row in listed] == [("identity", False), ("project", True)]
    shown = repr(listed)
    assert "acme team" not in shown and "x-team" not in shown and "/repo" not in shown


def test_a_profile_revision_that_keeps_the_selected_components_keeps_the_exception(monkeypatch):
    rows = (_approved(), _approved(id="x2222222222", permanent=True))
    monkeypatch.setattr(request_profiles, "REVISION", "http-envelope-9")
    monkeypatch.setattr(request_profiles, "CARRIES_FROM", frozenset({rows[0].revision}))
    assert [row.id for row in request_policy.in_force(_with(*rows), NOW)] == [rows[0].id, rows[1].id]


def test_a_profile_revision_that_changes_a_selected_component_suspends_the_exception(monkeypatch):
    rows = (_approved(), _approved(id="x2222222222", permanent=True))
    monkeypatch.setattr(request_profiles, "REVISION", "http-envelope-9")
    monkeypatch.setattr(request_profiles, "CARRIES_FROM", frozenset())
    assert request_policy.in_force(_with(*rows), NOW) == ()  # refuses, the permanent one too
    assert set(_states(*rows).values()) == {"needs approval again (profile revised)"}


def test_no_exception_widens_by_an_upgrade(monkeypatch):
    """A revision that carries an exception over carries it as approved: the same components, profile, project and
    lifetime; nothing of an upgrade adds to what it lets through or how long."""
    row = _approved()
    monkeypatch.setattr(request_profiles, "REVISION", "http-envelope-9")
    monkeypatch.setattr(request_profiles, "CARRIES_FROM", frozenset({row.revision}))
    [kept] = request_policy.in_force(_with(row), NOW)
    assert kept is row and kept.expires() == row.expires() and kept.components == row.components


def test_an_edition_can_forbid_permanent_exceptions():
    from latet.edition import Community

    assert Community().permanent_exceptions() is True  # the Community Edition allows them
    rows = (_approved(), _approved(id="x2222222222", permanent=True))
    allowed = request_policy.in_force(_with(*rows), NOW, permanent_allowed=True)
    forbidden = request_policy.in_force(_with(*rows), NOW, permanent_allowed=False)
    assert [row.id for row in allowed] == [rows[0].id, rows[1].id] and [row.id for row in forbidden] == [rows[0].id]
    assert _states(*rows)["x2222222222"] == "permanent"


def test_doctor_lists_permanent_and_suspended_exceptions(monkeypatch):
    from latet.doctor import checks

    rows = (_approved(), _approved(id="x2222222222", permanent=True), _approved(id="x3333333333", revision="old-1"))
    monkeypatch.setattr(checks, "load_config", lambda: _with(*rows))
    monkeypatch.setattr(checks, "_approved_settings", lambda: _with(*rows))
    monkeypatch.setattr(request_policy, "_now", lambda: NOW)
    found = checks.check_exceptions()
    said = {check.title: (check.status, check.detail) for check in found}
    assert said["Permanent request exception x2222222222"][0] == "warn"
    assert "identity" in said["Permanent request exception x2222222222"][1]
    assert said["Request exception x3333333333"] == ("warn", "needs approval again (profile revised)")
    assert all("x-team" not in detail for _, detail in said.values())


def test_doctor_lists_the_exceptions_that_run_not_those_written(monkeypatch):
    """Minor 3: while a change waits, the daemon runs the settings you approved: an edit of config.yaml that drops a
    permanent exception, waiting for you, does not hide it from `latet doctor` while it is in force; one written with
    Latet's stamps and never approved is listed as waiting, never as permanent."""
    from latet.doctor import checks

    permanent = _approved(id="x2222222222", permanent=True)
    forged = _approved(id="x4444444444", permanent=True, components=[{"kind": "header", "name": "x-cost"}])
    monkeypatch.setattr(checks, "load_config", lambda: _with(forged))  # written meanwhile, waiting for you
    monkeypatch.setattr(checks, "_approved_settings", lambda: _with(permanent))
    monkeypatch.setattr(request_policy, "_now", lambda: NOW)
    said = {check.title: (check.status, check.detail) for check in checks.check_exceptions()}
    assert said["Permanent request exception x2222222222"][0] == "warn"
    assert "Permanent request exception x4444444444" not in said
    assert said["Request exception (not approved yet)"] == ("warn", "pending")
    monkeypatch.setattr(checks, "_approved_settings", lambda: None)  # none approved yet: a first start
    said = {check.title: (check.status, check.detail) for check in checks.check_exceptions()}
    assert "Permanent request exception x4444444444" not in said
    assert said["Request exception (not approved yet)"] == ("warn", "pending")


def test_a_trusted_project_is_compared_as_projects_are_on_a_server(tmp_path):
    """I-4: a server reads no file, so a folder's own project is that folder (``resolve``), while projects are compared
    by the nearest folder of the settings that holds them (``compared_id``). A trusted project bound in a subfolder of
    a project of the settings is compared by that id on both sides: the project's exceptions apply to it."""
    from latet.engine.vault import Vault
    from latet.gateway.launch_authority import key_project
    from latet.projects import Projects

    root = str((tmp_path / "repo").resolve())
    (tmp_path / "repo" / "src").mkdir(parents=True)
    sub = str((tmp_path / "repo" / "src").resolve())
    config = Config(identity=IdentityConfig(enabled=False), ner="off", projects=[ProjectEntry(path=root)])
    server = Projects(config, Vault(b"\x07" * 32), config.identity, files=False)
    launched = server.resolve(sub)
    chosen = key_project(server, sub, launched.root, launched.id, None)
    assert chosen is not None and chosen.id != server.compared_id(root)  # the ids a folder and a project have differ
    rows = (_approved(project=root),)
    compared = server.compared_id(chosen.root)  # what the launch decision records (``RequestScope.compared``)
    assert request_policy.trusted_folders(rows, binding="trusted", compared=compared, projects=server) == {root}


def test_a_project_body_grant_is_chosen_only_for_a_trusted_project():
    """RC-REC-7, D7: the body selectors of a project's exception are taken only by a request whose project Latet
    trusts; a claimed one, or none, takes the identity's alone. Its folders are compared as projects are (R-105): an
    exception on the project's root and one on a subfolder of it apply together (Minor 6)."""
    body = {"components": [{"kind": "body", "name": "/x_tenant", "type": "string"}], "reason": "other"}
    other = {"components": [{"kind": "body", "name": "/x_region", "type": "string"}], "reason": "other"}
    shared = _approved(id="x1111111111", **body)
    scoped = _approved(id="x2222222222", project="/repo", **body)
    deeper = _approved(id="x3333333333", project="/repo/src", **other)
    rows = (shared, scoped, deeper)
    [(profile, grants)] = request_policy.body_grants(rows)
    projects = type("P", (), {"compared_id": staticmethod(lambda folder: "p-repo")})()

    def trusted(binding, compared):
        return request_policy.trusted_folders(rows, binding=binding, compared=compared, projects=projects)

    assert profile == "openai.chat.1"
    assert trusted("trusted", "p-repo") == {"/repo", "/repo/src"}
    assert trusted("claimed", "p-repo") == trusted(None, None) == trusted("trusted", "p-other") == frozenset()
    chosen = grants.chosen(trusted("trusted", "p-repo"))
    assert chosen is not None and chosen is not grants.identity
    assert chosen is grants.chosen(trusted("trusted", "p-repo"))  # built once for that set
    assert chosen.admits(("x_tenant",), "acme") is not None and chosen.admits(("x_region",), "eu") is not None
    assert grants.chosen(frozenset()) is grants.identity
    assert grants.identity is not None and grants.identity.admits(("x_region",), "eu") is None


@pytest.mark.parametrize(
    ("profile", "protocol"),
    [
        ("anthropic.messages.1", "anthropic"),
        ("anthropic.count_tokens.1", "anthropic"),
        ("bedrock.invoke.1", "anthropic"),
        ("company.bedrock.invoke-with-response-stream.1", "anthropic"),
        ("vertex.anthropic.rawPredict.1", "anthropic"),
        ("company.vertex.streamRawPredict.1", "anthropic"),
        ("openai.chat.1", "chat"),
        ("openai.completions.1", "chat"),
        ("azure-openai.chat.1", "chat"),
        ("gemini.v1beta.openai.completions.1", "chat"),
        ("openai.responses.1", "responses"),
        ("chatgpt.codex.responses.1", "responses"),
        ("openai.embeddings.1", "embeddings"),
        ("gemini.v1beta.generateContent.1", "gemini"),
        ("gemini.v1beta.cache-create.1", "gemini"),
        ("openai.models.1", None),
        ("bedrock.inference-profiles.1", None),
        ("gemini.v1beta.model.1", None),
    ],
)
def test_the_body_protocol_of_each_profile(profile, protocol):
    """The map an exception's body revision is stamped by (R-100): the protocol mediate reads that profile's body by."""
    assert request_policy.body_protocol(profile) == protocol


def test_a_pass_under_an_exception_is_kept_in_the_safe_record_by_its_category():
    """A Pass resting on an exception is never dropped from the safe record (a bounded loss), nor named by its id: its
    rules are counted under one exception category, whether the exception admitted body members or envelope ones."""
    from latet.gateway import request_audit as module
    from latet.gateway.outcome import Pass, PolicyEvidence, ResponsePlan

    audit = module.RequestAudit()
    for evidence in (
        PolicyEvidence(("exception x1111111111", "checked only"), "chat.body.1", ("x1111111111",)),
        PolicyEvidence((), "openai.chat.1", ("x2222222222",)),
    ):
        decision = Pass(None, b"", (), evidence, None, ResponsePlan("chat"))  # type: ignore[arg-type]
        module.emit_decision(audit, decision, source=1, method=2)
    assert audit.sequence == 2 and audit.overflow == 0
    assert "x1111111111" not in repr(audit._events) and "x2222222222" not in repr(audit._events)
    # Root's ruling on F-5: the exception category has a code of its own (8); 7 meant legacy-0.10 and stays retired.
    events = [json.loads(entry.payload) for entry in audit._events.values() if entry.payload is not None]
    assert [sorted(code for code, _ in event[16]) for event in events] == [[1, 8], [8]]
    assert not module._valid(module.SafeDecisionRecord(source=1, outcome=2, rules=((7, 1),)))
