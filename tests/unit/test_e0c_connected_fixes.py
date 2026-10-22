"""The E0c final review's fixes on a connected computer (R3, R5, R7, R8, R9, R19, C-M1, C-M4, C-M8): what a person
meets once connected says its cause and what to do, `latet connect` and `latet disconnect` never stop half-way in
silence, and no agent gets a token nothing could use. Every unsafe or platform case is synthetic."""

import json
import os
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from latet import claude_bridge, wrappers
from latet.cli import admin as admin_cli
from latet.cli import connect as connect_cli
from latet.config import data_dir
from latet.doctor import checks
from latet.engine.reviewer import certificate as certificates
from latet.integrations import agents, base
from latet.server import client as client_module
from latet.server import connection
from latet.server.certificate import make_certificate
from latet.server.tokens import route_key
from latet.storage import folders

from . import test_agents_connected as existing
from .conftest import connect_to, run_cli

_agents_here = existing._agents_here
claude_home = existing.claude_home
connected = existing.connected
trusted_helper_install = existing.trusted_helper_install
applied = existing.applied
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1003"


def _token() -> str:
    [kept] = connection.kept_tokens(data_dir())
    return kept.token


# --- R3: the helper's installation refusal names what failed and why --------------------------------------------------


@WINDOWS
@pytest.mark.parametrize(
    ("where", "mask"),
    [("file", 0x4), ("file", 0x1F01FF), ("parent", 0x1F01FF)],
    ids=["install-write", "namespace-full-control", "parent-full-control"],
)
def test_the_helper_refusal_names_the_part_of_the_installation_the_folder_and_the_account(monkeypatch, where, mask):
    python = Path(sys.executable).resolve()
    unsafe = python if where == "file" else python.parent
    monkeypatch.setattr(folders, "my_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(folders, "_account", lambda sid: "PC\\remoteadm" if sid == OTHER else "")

    def grants(folder):
        extra = [folders.Grant(OTHER, mask, False)] if Path(folder) == unsafe else []
        return "S-1-5-21-1-2-3-1001", extra

    monkeypatch.setattr(folders, "folder_grants", grants)
    with pytest.raises(connection.NotConnected) as refused:
        claude_bridge._install_path(python, directory=False, what="Python (sys.executable)")
    words = refused.value.message
    assert "Python (sys.executable)" in words and str(unsafe) in words
    assert f"PC\\remoteadm ({OTHER})" in words and "icacls" in words and "Troubleshooting" in words
    assert "LATET_DATA_DIR" not in words and "data folder" not in words  # Minor 3: the installation's own fix alone


# --- R5: Claude Code through a server is Windows-only in E0c -----------------------------------------------------------


def test_latet_claude_says_up_front_that_a_server_needs_a_windows_terminal(connected, monkeypatch):
    monkeypatch.setattr(claude_bridge, "platform_problem", lambda: claude_bridge.HELD_BACK)
    monkeypatch.setattr("latet.cli.main._require", lambda command: None)
    launched = []
    monkeypatch.setattr(wrappers, "run", lambda argv, env: launched.append(argv) or 0)
    done = run_cli("claude")
    assert done.exit_code == 2 and "needs a Windows terminal in this version" in done.output
    assert launched == [] and not (data_dir() / "claude-settings").exists()


def test_agents_apply_writes_no_token_where_no_binding_can_be_made(connected, claude_home, monkeypatch):
    monkeypatch.setattr(claude_bridge, "platform_problem", lambda: claude_bridge.HELD_BACK)
    claude = agents.ClaudeCode()
    claude.apply(agents.ClaudeOptions())
    env = json.loads((claude_home / "settings.json").read_text("utf-8"))["env"]
    assert "LATET_BRIDGE_TOKEN" not in env and claude_bridge.HELPER_KEY not in env
    assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")  # the plugin and the route: held back, never direct
    assert any("needs a Windows terminal in this version" in line for line in claude.said)


def test_doctor_reports_a_connected_claude_code_that_cannot_be_bound(connected, monkeypatch):
    monkeypatch.setattr(claude_bridge, "platform_problem", lambda: claude_bridge.HELD_BACK)
    prepared = connection._prepare_token(connection._prepare_connection())
    [found] = checks.helper_checks(prepared, {"env": {}})
    assert found.status == checks.FAIL and "needs a Windows terminal" in found.detail


@WINDOWS
def test_doctor_tells_a_missing_or_stale_helper_binding_apart(connected, claude_home, trusted_helper_install):
    agents.ClaudeCode().apply(agents.ClaudeOptions())
    prepared = connection._prepare_token(connection._prepare_connection())
    settings = json.loads((claude_home / "settings.json").read_text("utf-8"))
    assert checks.helper_checks(prepared, settings)[0].status == checks.OK
    descriptor = claude_bridge._binding_path(data_dir())
    descriptor.write_text(descriptor.read_text("utf-8").replace('"v":1', '"v":1 '), "utf-8")
    [stale] = checks.helper_checks(prepared, settings)
    assert stale.status == checks.FAIL and "another installation" in stale.detail
    descriptor.unlink()
    [missing] = checks.helper_checks(prepared, settings)
    assert (
        missing.status == checks.FAIL and "missing" in missing.detail and "latet agents apply claude" in missing.detail
    )


# --- R7: a refused token says what to do --------------------------------------------------------------------------------


def test_status_doctor_and_an_agents_start_say_what_to_do_with_a_refused_token(latet_server, quiet):
    assert connect_to(latet_server).exit_code == 0
    latet_server.tokens.clear()  # revoked by an administrator, or the identity disabled
    words = "the server no longer accepts this computer's token (revoked, or your identity disabled)"
    status = run_cli("status")
    assert status.exit_code == 3 and words in " ".join(status.output.split())
    assert "latet disconnect" in status.output and f"latet connect {latet_server.url}" in status.output


def test_latets_own_calls_say_what_to_do_with_a_refused_token(latet_server, quiet):
    from starlette.responses import JSONResponse

    assert connect_to(latet_server).exit_code == 0
    latet_server.space_answers["_latet/admin/user-name"] = JSONResponse({"error": "unauthorized"}, status_code=401)
    with pytest.raises(wrappers.DaemonError) as refused:
        wrappers._space_call(connection.load_connection(), "/_latet/admin/user-name", {"name": ""})
    assert refused.value.status == 401 and "no longer accepts this computer's token" in str(refused.value)


def test_a_missing_token_says_the_same_advice(connected, monkeypatch):
    monkeypatch.setattr(connection, "_copies", lambda data: iter(()))
    prepared = connection._prepare_connection()
    with pytest.raises(connection.NotConnected) as refused:
        client_module._connection_credential(prepared)
    assert "latet disconnect" in refused.value.message and "administrator to reconnect" not in refused.value.message


# --- R8: connect and disconnect ---------------------------------------------------------------------------------------


def test_reapplying_goes_on_past_an_agent_that_fails_and_says_its_words(computer, monkeypatch, capsys):
    record = base.Record("x", 1.0)
    monkeypatch.setattr(base, "load_records", lambda: {"claude": record, "codex": record})
    applied = []

    def refused(self, options=None, config=None, **kwargs):
        raise connection.NotConnected("TASK_HELPER_WORDS: use a private installation folder")

    monkeypatch.setattr(agents.ClaudeCode, "applied_options", lambda self: agents.ClaudeOptions())
    monkeypatch.setattr(agents.ClaudeCode, "apply", refused)
    monkeypatch.setattr(agents.Codex, "apply", lambda self, *a, **k: applied.append("codex"))
    monkeypatch.setattr(admin_cli, "say_applied", lambda agent: None)
    with pytest.raises(admin_cli.NotAllApplied) as failed:
        admin_cli.refresh_applied()
    assert applied == ["codex"] and failed.value.names == ["Claude Code"] and failed.value.done == ["Codex"]
    assert "TASK_HELPER_WORDS" in capsys.readouterr().out


def test_connect_checks_the_claude_binding_before_the_code_is_spent(latet_server, quiet, monkeypatch):
    monkeypatch.setattr(claude_bridge, "platform_problem", lambda: None)
    monkeypatch.setattr(claude_bridge, "installation_problem", lambda: "TASK_INSTALL_WORDS")
    monkeypatch.setattr(connect_cli, "_applied", lambda: [("claude", "Claude Code")])
    monkeypatch.setattr(agents, "_token_kept_out", lambda settings: None)
    code = latet_server.code
    done = connect_to(latet_server)
    assert done.exit_code == 2 and "TASK_INSTALL_WORDS" in done.output and "Nothing was sent" in done.output
    assert latet_server.code == code and latet_server.labels == []


def test_connect_lists_what_still_points_at_the_local_address(latet_server, quiet, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:5283/openai/v1")
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    assert "$OPENAI_BASE_URL" in done.output and "Still pointed at this computer's Latet" in done.output


def test_a_disconnect_that_stops_says_the_agents_go_without_latet_and_a_second_keeps_revoked(latet_server, quiet):
    assert connect_to(latet_server).exit_code == 0
    settings = agents.ClaudeCode().settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"env": {"PLANTED": _token()}}), "utf-8")
    first = run_cli("disconnect", "--yes")
    assert first.exit_code == 1 and "your agents go to their providers without Latet" in " ".join(first.output.split())
    settings.unlink()
    second = run_cli("disconnect", "--yes")
    assert second.exit_code == 0, second.output
    assert "in an earlier run" in second.output and "no token was revoked in this run" not in second.output


def test_disconnect_removes_the_helpers_empty_folders_with_the_connection(latet_server, quiet):
    assert connect_to(latet_server).exit_code == 0
    helper = connection.connection_dir() / "claude-helper"
    for folder in (helper.parent, helper):  # private, as Latet makes them: on macOS one others can read is unsafe
        folder.mkdir(mode=0o700, exist_ok=True)
    for child in ("cwd", "temp", "homes/user", "homes/codex", "homes/claude", "homes/xdg", "homes/roaming"):
        folder = helper
        for part in child.split("/"):
            folder /= part
            folder.mkdir(mode=0o700, exist_ok=True)
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 0, done.output
    assert not connection.connection_dir().exists()


def test_only_this_servers_values_keep_a_disconnect_from_finishing():
    token = "latet_pat_" + "t" * 43
    known = [token, route_key(token), "/data/connection"]
    theirs = f"https://other.example/u/{route_key('latet_key_' + 'k' * 43)}"
    assert connect_cli._holds(f"https://latet.acme.lan/u/{route_key(token)}/anthropic", known)
    assert not connect_cli._holds(theirs, known)  # a person's own API-key space elsewhere is theirs
    assert connect_cli._holds("latet_pat_" + "o" * 43, known)  # a personal token, by its shape: Latet writes no other
    unknown = ["https://latet.acme.lan/u/", "/data/connection"]  # no copy of the token could be read
    assert connect_cli._holds("https://latet.acme.lan/u/abc", unknown)
    assert not connect_cli._holds(theirs, unknown)
    assert connect_cli._holds(theirs, known, anywhere=True)  # a data folder others may change: nothing is trusted


# --- R9: a repository's latet.yaml on a connected computer ---------------------------------------------------------------


def test_a_projects_latet_yaml_is_said_not_applied_on_a_connected_computer(tmp_path):
    from latet.projects import not_applied_here

    project = tmp_path / "repository"
    (project / ".git").mkdir(parents=True)
    (project / "src").mkdir()
    assert not_applied_here(project / "src") is None
    (project / "latet.yaml").write_text("profile: max\n", "utf-8")
    note = not_applied_here(project / "src")
    assert note is not None and str(project / "latet.yaml") in note and "not applied" in note
    assert "for everyone" not in note  # a Community server sets no rule for everyone
    [found] = checks.check_project(project / "src")
    assert found.status == checks.WARN and found.title == "Project"


# --- R19 and the minors -------------------------------------------------------------------------------------------------


def test_a_client_left_at_its_deadline_keeps_its_captured_verify_context(connected, monkeypatch):
    prepared = connection._prepare_token(connection._prepare_connection())
    pem = client_module._connection_pin(prepared)
    made = []
    real = client_module._http

    def recorded(url, fingerprint, timeout, *, _verify=None):
        made.append(_verify)
        return real(url, fingerprint, timeout, _verify=_verify)

    monkeypatch.setattr(client_module, "_http", recorded)
    assert prepared.connection is not None
    with client_module.ServerClient(
        prepared.connection.url, prepared.token, prepared.connection.fingerprint, _prepared=prepared, _pem=pem
    ) as server:
        monkeypatch.setattr(client_module, "tls_context", lambda fingerprint: pytest.fail("the pin was read again"))
        server._leave()
    assert len(made) == 2 and made[0] is not None and made[1] is made[0]


def test_an_expired_pin_is_said_as_expired_not_as_another_certificate(computer):
    crt, _, fingerprint = make_certificate("https://latet.acme.lan", computer / "tls")
    pem = crt.read_text("utf-8")
    refusal = ssl.SSLCertVerificationError(1, "certificate verify failed")
    refusal.verify_code = 10  # X509_V_ERR_CERT_HAS_EXPIRED
    failure = httpx.ConnectError("handshake")
    failure.__cause__ = refusal
    with client_module.ServerClient("https://latet.acme.lan", None, fingerprint) as server:
        server._pem = pem
        said = server._unreachable(failure)
    assert f"expired on {certificates.expires(pem).date().isoformat()}" in said
    assert "latet disconnect" in said and "posing as your server" not in said


def test_latet_start_on_a_connected_computer_says_so_and_starts_nothing(connected, monkeypatch):
    from latet.service import daemon

    monkeypatch.setattr(daemon, "start", lambda *a, **k: pytest.fail("the local daemon was started"))
    done = run_cli("start")
    assert done.exit_code == 2 and "connected to" in done.output and "latet disconnect" in done.output


def test_identity_send_when_not_connected_says_so_rather_than_to_retry(computer):
    done = run_cli("identity", "send")
    assert done.exit_code == 2 and "not connected" in done.output and "Retry" not in done.output


def test_a_refused_token_names_no_secret_in_any_of_its_words(latet_server, quiet):
    assert connect_to(latet_server).exit_code == 0
    token = _token()
    latet_server.tokens.clear()
    for said in (run_cli("status").output, run_cli("doctor").output):
        assert token not in said and route_key(token) not in said


# --- the re-review's minors -------------------------------------------------------------------------------------------


def test_disconnect_finds_a_backup_holding_the_token_before_anything_is_put_back(applied, latet_server):
    """C-I6 (Minor 5): a backup the revert leaves stops the run first, the agents still going through the server."""
    kept = data_dir() / "backups" / "20261016-120000" / "settings.json.0123abcd"
    kept.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private, as Latet makes its backups
    kept.write_text(json.dumps({"env": {"LATET_BRIDGE_TOKEN": applied["token"]}}), "utf-8")
    before = {path: path.read_bytes() for path in applied["files"][:3]}
    done = run_cli("disconnect", "--yes")
    said = " ".join(done.output.split())
    assert done.exit_code == 1 and str(kept) in said and "Nothing was put back" in said
    assert {path: path.read_bytes() for path in before} == before
    assert connection.load_connection() is not None


def test_a_disconnect_that_cannot_forget_the_connection_says_the_agents_go_without_latet(
    applied, latet_server, monkeypatch
):
    def refused():
        raise PermissionError(13, "refused")

    monkeypatch.setattr(connection, "forget_connection", refused)
    done = run_cli("disconnect", "--yes")
    said = " ".join(done.output.split())
    assert done.exit_code == 1 and "go to their providers without Latet" in said


def test_a_disconnect_from_a_folder_others_may_change_says_the_agents_go_without_latet(applied, latet_server):
    existing._open_to_others(data_dir())
    done = run_cli("disconnect", "--yes")
    assert "go to their providers without Latet" in " ".join(done.output.split())


def test_latet_start_with_a_connection_file_that_is_refused_says_why_and_exits_2(computer, monkeypatch):
    data_dir().mkdir(parents=True)
    connection.connection_path().write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")
    monkeypatch.setattr(connection, "private_folder_problem", lambda folder: f"{folder} is open to others")
    done = run_cli("start")
    assert done.exit_code == 2 and "open to others" in done.output and "Traceback" not in done.output
    assert isinstance(done.exception, SystemExit)


def test_an_earlier_runs_revocation_counts_for_its_own_token_alone(latet_server, quiet, keyring_on):
    """C-M3 (Minor 8): two copies, one revoked by an earlier run: the other is still said as not revoked."""
    from latet.config import write_atomic

    assert connect_to(latet_server).exit_code == 0
    current = connection.load_connection()
    assert current is not None
    other = "latet_pat_" + "b" * 43  # a copy the server never knew
    write_atomic(data_dir() / "server.token", connection._record(current, other), private=True)
    settings = agents.ClaudeCode().settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    [mine] = [kept.token for kept in connection.token_copies(data_dir())[0] if kept.token != other]
    settings.write_text(json.dumps({"env": {"PLANTED": mine}}), "utf-8")
    assert run_cli("disconnect", "--yes").exit_code == 1  # revokes the first, stops at the planted copy
    settings.unlink()
    second = " ".join(run_cli("disconnect", "--yes").output.split())
    assert "revoked this computer's token in an earlier run" in second
    assert "did not revoke this computer's token" in second and "may remain valid" in second


def test_agents_refresh_says_which_agent_failed_without_a_traceback(computer, monkeypatch):
    def partly(skip=None):
        raise admin_cli.NotAllApplied(["Claude Code"], ["Codex"])

    monkeypatch.setattr(admin_cli, "refresh_applied", partly)
    done = run_cli("agents", "refresh")
    assert done.exit_code == 1 and "Codex: refreshed" in done.output and "Claude Code" in done.output
    assert isinstance(done.exception, SystemExit)


def test_a_missing_pin_says_disconnect_then_connect_with_the_servers_fingerprint(connected):
    prepared = connection._prepare_token(connection._prepare_connection())
    assert prepared.connection is not None and prepared.connection.fingerprint is not None
    for pem in (data_dir() / "certificates").iterdir():
        pem.unlink()
    with pytest.raises(client_module.ServerError) as refused:
        client_module._connection_pin(prepared)
    assert "`latet disconnect`" in refused.value.message
    refusal = ssl.SSLCertVerificationError(1, "certificate verify failed")
    refusal.verify_code = 20
    failure = httpx.ConnectError("handshake")
    failure.__cause__ = refusal
    with client_module.ServerClient(prepared.connection.url, None, prepared.connection.fingerprint) as server:
        said = server._unreachable(failure)
    assert "missing" in said and "`latet disconnect`" in said and "new fingerprint" not in said


def _unused() -> None:  # keeps the shared imports used by every platform's selection
    _ = (os, SimpleNamespace)
