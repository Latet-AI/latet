"""`latet server approvals` (Latet Enterprise): who decides on what lowers protection, shown and changed through E5's
administration route with an administration token; a refusal of the server changes nothing and says why."""

import pytest
from typer.testing import CliRunner

from latet.server.client import ServerError

SAVED = {"changes": "self", "live_review": "self"}


@pytest.fixture
def server(monkeypatch):
    """E0c's administration call, answered as E5's route answers: the section, or the section set. Patched where the
    command reads it (`approvals_cli` imports it by name, R36), as E4's tests/ee/events/test_cli.py patches events_cli."""
    from latet.ee import approvals_cli

    saved, calls = dict(SAVED), []

    def admin_call(method, path, body=None, **_options):
        calls.append((method, path, body))
        if method == "PUT":
            saved.update(body)
        return {"approvals": dict(saved)}

    monkeypatch.setattr(approvals_cli, "admin_call", admin_call)
    return calls


def _cli(*args):
    import latet.cli.admin  # noqa: F401 - registers the commands, Latet Enterprise's too
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args))


def test_who_decides_is_shown_and_changed_through_the_server(server):
    shown = _cli("server", "approvals", "show")
    assert shown.exit_code == 0, shown.output
    assert "whoever's settings they are decides" in shown.output and "whoever sends them decides" in shown.output
    changed = _cli("server", "approvals", "set", "--changes", "security")
    assert changed.exit_code == 0, changed.output
    assert server[-1] == ("PUT", "ee/approvals/settings", {"changes": "security"})
    assert "the security team decides, on API keys' too" in changed.output
    assert "Tell people first" not in changed.output
    assert _cli("server", "approvals", "set").exit_code == 2  # nothing to change


def test_texts_sent_to_the_security_team_are_said_to_be_told_to_people_first(server):
    """R46 (the plan review's L5): people learn of the routing at their first text, after it started: the command says
    the company tells them first, and what keeps a person from turning the Live Reviewer off to decide alone."""
    changed = _cli("server", "approvals", "set", "--live-review", "security")
    assert changed.exit_code == 0, changed.output
    assert "Tell people first" in changed.output and "--changes security" in changed.output
    # The company's policy cannot keep it on: a policy is a rules file, which has no Live Reviewer (transfer.KEYS).
    assert "policy" not in changed.output


def test_instances_of_another_release_alive_are_said_as_the_server_says_them(monkeypatch):
    """The whole-branch security review's L5: what the server says of instances that do not run its release with the
    Enterprise Edition is printed after who decides, as `latet server policy set` prints its problems; nothing when it
    says none."""
    from latet.ee import approvals_cli

    problem = "1 instance does not run this release of Latet with the Enterprise Edition: stop it"
    answers = [{"approvals": {"changes": "security", "live_review": "self"}, "problems": [problem]},
               {"approvals": {"changes": "security", "live_review": "self"}, "problems": []}]  # fmt: skip
    monkeypatch.setattr(approvals_cli, "admin_call", lambda method, path, body=None, **_options: answers.pop(0))
    warned = _cli("server", "approvals", "set", "--changes", "security")
    assert warned.exit_code == 0 and problem in warned.output
    assert "the security team decides" in warned.output.split(problem)[0]  # who decides first, then what is not right
    quiet = _cli("server", "approvals", "set", "--changes", "security")
    assert quiet.exit_code == 0 and "instance" not in quiet.output


def test_a_refusal_of_the_server_changes_nothing_and_says_why(monkeypatch):
    from latet.ee import approvals_cli

    def refusing(method, path, body=None, **_options):
        raise ServerError(403, "the trial of Latet Enterprise ended: what it holds can be read, not changed")

    monkeypatch.setattr(approvals_cli, "admin_call", refusing)
    refused = _cli("server", "approvals", "set", "--live-review", "security")
    assert refused.exit_code == 2 and "can be read, not changed" in refused.output


def test_who_decides_is_changed_by_you_never_by_an_agent(monkeypatch):
    """E0c's `admin_call` refuses an agent's change before it reads a token or reaches a server (`_mutation`)."""
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = _cli("server", "approvals", "set", "--changes", "security")
    assert refused.exit_code == 2 and "run it yourself" in refused.output
