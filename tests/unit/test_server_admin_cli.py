"""`latet server` from any computer, through the administration API: what each command asks and how it says the
answer. The API itself is a fake here (tests/server/test_e0c_end_to_end.py asks the real one)."""

import json
import os
import sys
from pathlib import Path

import pytest
import typer

from latet.config import Config
from latet.server.client import ServerError
from latet.server.tokens import route_key

from .conftest import run_cli

URL = "https://latet.acme.lan"
KEY = "latet_key_" + "k" * 43


def _path(path, kwargs):
    from urllib.parse import urlencode

    return path + "?" + urlencode(kwargs["query"]) if kwargs.get("query") else path


@pytest.fixture(autouse=True)
def own_command(computer, request: pytest.FixtureRequest):
    """Clear host markers after computer installs its owned config paths; this does not fake a TTY."""
    request.getfixturevalue("quiet")


def test_owned_command_fixture_keeps_fallback_home_private_and_agent_markers_absent(computer):
    from latet.cli.agent import agent_name

    assert agent_name(strict=True) is None
    assert "CODEX_HOME" not in os.environ
    assert Path.home() == computer / "home"
    assert Path(os.environ["CLAUDE_CONFIG_DIR"]) == computer / "home" / ".claude"
    assert Path(os.environ["XDG_CONFIG_HOME"]) == computer / "home" / ".config"


@pytest.mark.parametrize(
    "command",
    [
        ("people", "remove", "Emily Carter", "--yes"),
        ("people", "code", "Emily Carter"),
        ("keys", "rotate", "Billing CI", "--yes"),
        ("keys", "rotate", "Billing CI", "--token", "t_aaaaaaaaaa", "--yes"),
        ("keys", "add-token", "Billing CI", "logs", "--mask"),
        ("keys", "tokens", "Billing CI", "--revoke", "t_aaaaaaaaaa", "--yes"),
        ("keys", "limit", "Billing CI", "5000"),
        ("admin-token", "issue"),
        ("config", "set", "history", "metadata"),
        ("config", "--key", "Billing CI", "set", "profile", "max"),
    ],
)
def test_agents_cannot_change_administration(admin, monkeypatch, command):
    monkeypatch.setenv("CLAUDECODE", "1")
    done = run_cli("server", *command)
    assert done.exit_code == 2, done.output
    assert all(method == "GET" for method, _, _ in admin.calls)


@pytest.mark.parametrize("marker", ["CLAUDE_CODE_UNRECOGNIZED", "CODEX_HOME"])
def test_values_refuse_strict_agent_marker(admin, computer, monkeypatch, marker):
    monkeypatch.setenv(marker, str(computer / "home" / ".codex") if marker == "CODEX_HOME" else "1")
    exported = computer / "rules.yaml"  # an agent would read the file back: refused as the terminal's copy is
    for command in (("config", "get"), ("rules", "export"), ("rules", "export", str(exported))):
        done = run_cli("server", *command)
        assert done.exit_code == 2
    assert not admin.calls and not exported.exists()


@pytest.mark.parametrize("command", [("people", "list"), ("status",)])
def test_agents_can_read_nonsecret_administration(admin, monkeypatch, command):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr("latet.engine.reviewer.certificate.fetch", lambda url: (None, None))
    done = run_cli("server", *command)
    assert done.exit_code == 0, done.output
    assert admin.calls and all(method == "GET" for method, _, _ in admin.calls)


@pytest.mark.parametrize(
    "command",
    [
        ("admin-token", "revoke", "../identities/u_emilycarte"),
        ("people", "tokens", "Emily Carter", "--revoke", "t_abcdefghij&token=t_x"),
        ("keys", "tokens", "Billing CI", "--revoke", "t_abcdefghij&token=t_x"),
        ("keys", "tokens", "Billing CI", "--revoke", "latet_key_" + "z" * 43),
        ("keys", "rotate", "Billing CI", "--token", "t_abcdefghij&token=t_x"),
        ("keys", "rotate", "Billing CI", "--token", "latet_key_" + "z" * 43),
        ("keys", "add-token", "Billing CI", "--scan", "latet_key_" + "z" * 43),
        ("keys", "add-token", "latet_key_" + "z" * 43, "logs", "--scan"),
        ("keys", "limit", "latet_key_" + "z" * 43, "5000"),
        ("admin-token", "revoke", "latet_adm_" + "z" * 43),
        ("config", "--key", "latet_key_" + "z" * 43, "get"),
    ],
)
def test_ids_and_pasted_secrets_are_rejected_before_reads(admin, command):
    done = run_cli("server", *command)
    assert done.exit_code == 2, done.output
    assert not admin.calls
    assert command[-1] not in done.output


def test_ambiguous_identity_has_no_name_email_precedence(admin, monkeypatch):
    def answer(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        return {"identities": [EMILY, {**EMILY, "id": "u_aaaaaaaaaa", "name": EMILY["email"]}]}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "people", "disable", EMILY["email"])
    assert done.exit_code == 1 and "u_aaaaaaaaaa" in done.output and EMILY["id"] in done.output


def test_bad_ids_from_server_are_left_out(admin, monkeypatch):
    def answer(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        assert method == "GET"
        return {"identities": [{**BILLING, "id": "../shared?x="}]}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "keys", "disable", "Billing CI")
    assert done.exit_code == 1 and "left out" in done.output


def test_never_needs_label_and_days_are_bounded(admin):
    for args in (("--never",), ("--days", "0"), ("--days", "3651")):
        assert run_cli("server", "admin-token", "issue", *args).exit_code == 2
    assert not admin.calls


def test_config_reset_does_not_replace_settings_that_open(admin):
    done = run_cli("server", "config", "reset", "--yes")
    assert done.exit_code == 1
    assert all(method == "GET" for method, _, _ in admin.calls)


def test_wrapper_removes_admin_token_from_child(computer, monkeypatch):
    from latet import wrappers

    seen = []
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "x" * 43)
    monkeypatch.setattr(wrappers.shutil, "which", lambda _: "stand-in")
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: seen.append(env) or 0)
    assert wrappers.run(["stand-in"], {"LATET_SERVER_ADMIN_TOKEN": "also stripped"}) == 0
    assert "LATET_SERVER_ADMIN_TOKEN" not in seen[0]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_public_admin_call_guards_mutations_before_token(computer, monkeypatch, method):
    from latet.cli import server

    monkeypatch.setenv("CLAUDECODE", "1")

    def no_target(*args, **kwargs):
        raise AssertionError("resolved a target before the mutation guard")

    monkeypatch.setattr("latet.server.connection.server_target", no_target)
    monkeypatch.setattr(server, "_admin_token", lambda *args, **kwargs: pytest.fail("read a token"))
    with pytest.raises(typer.Exit) as stopped:
        server.admin_call(method, "shared")
    assert getattr(stopped.value, "exit_code", None) == 2


@pytest.mark.parametrize("target", ["https://other.acme.lan", None])
def test_env_token_bound_to_env_server(computer, monkeypatch, target):
    from latet.cli import server
    from latet.server.connection import NotConnected

    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    if target:
        monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setattr("latet.server.connection.server_target", lambda *args, **kwargs: (target or URL, None))
    monkeypatch.setattr(server, "_target", {"server": target, "fingerprint": None, "token_file": None})
    monkeypatch.setattr("latet.server.client.ServerClient", lambda *args, **kwargs: pytest.fail("sent a token"))
    with pytest.raises(NotConnected, match="goes only"):
        server.admin_call("GET", "status")


@pytest.mark.parametrize(
    "path", ["../shared", "admin-tokens/../shared", "shared?x=y", "shared#fragment", "%2e%2e/shared"]
)
def test_admin_paths_refuse_traversal_and_embedded_queries(computer, monkeypatch, path):
    from latet.cli import server

    def no_target(*args, **kwargs):
        raise AssertionError("resolved a target before checking the administration path")

    monkeypatch.setattr("latet.server.connection.server_target", no_target)
    with pytest.raises(typer.Exit) as stopped:
        server.admin_call("GET", path)
    assert getattr(stopped.value, "exit_code", None) == 2


def test_admin_call_reads_within_the_time_it_is_given(computer, monkeypatch):
    """A call may wait longer for the server's answer than the 10 s read and the 60 s in all of every other (a key's
    first forget after an upgrade, review 11b's N5): the client reads within what it is given."""
    import httpx

    from latet.cli import server

    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setattr("latet.server.connection.server_target", lambda *args, **kwargs: (URL, None))
    monkeypatch.setattr(server, "_target", {"server": URL, "fingerprint": None, "token_file": None})
    waits: list[tuple[str, float]] = []

    class Client:
        def __init__(self, *args, timeout=10.0, **kwargs):
            waits.append(("read", timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, *args, deadline=60.0, **kwargs):
            waits.append(("in all", deadline))
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("latet.server.client.ServerClient", Client)
    assert server.admin_call("GET", "status") == {"ok": True}
    assert server.admin_call("GET", "status", timeout=300.0, deadline=300.0) == {"ok": True}
    assert waits == [("read", 10.0), ("in all", 60.0), ("read", 300.0), ("in all", 300.0)]


def test_terminal_confirmation_even_with_yes(admin, monkeypatch):
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    done = run_cli("server", "people", "remove", "Emily Carter", "--yes")
    assert done.exit_code == 1 and not any(method == "DELETE" for method, _, _ in admin.calls)
    assert URL in questions[0] and EMILY["id"] in questions[0]


@pytest.mark.parametrize(
    "command",
    [
        ("people", "list"),
        ("log",),
        ("log", "--json"),
        ("config", "get", "upstreams"),
        ("config", "get", "upstreams", "--json"),
        ("config", "--key", "Billing CI", "get", "terms"),
        ("config", "--key", "Billing CI", "get", "terms", "--json"),
        ("rules", "--key", "Billing CI", "list", "--json"),
    ],
)
def test_server_output_escapes_every_terminal_control(admin, monkeypatch, command):
    danger = "name" + "".join(chr(n) for n in (27, 155, 8238, 8232, 133))
    original = admin.__call__

    def answer(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        if path == "identities?kind=person":
            return {"identities": [{**EMILY, "name": danger}]}
        if path.startswith("log?"):
            return {"entries": [{"id": 1, "ts": 1, "actor": danger, "action": danger, "details": {"x": danger}}]}
        if path == "shared":
            return {"settings": {**admin.shared, "upstreams": [{"name": "llm-acme-com", "url": danger}]}, "etag": "e1"}
        if path == "identities/u_billingci2/settings":
            return {
                "settings": {
                    **admin.own,
                    "terms": [{"value": danger}],
                    "recognizers": [{"name": danger, "type": "INVOICE", "pattern": "INV-[0-9]{6}"}],
                },
                "etag": "k1",
            }
        return original(method, path, body)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", *command)
    assert done.exit_code == 0, done.output
    assert all(chr(n) not in done.output for n in (27, 155, 8238, 8232, 133))


@pytest.mark.parametrize("command", [("config", "get"), ("rules", "forget", "--yes")])
def test_the_rules_kept_on_the_server_are_said_without_its_terminal_controls(admin, monkeypatch, command):
    danger = "name" + "".join(chr(n) for n in (27, 155, 8238, 8232, 133))
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    admin.kept = {danger: "max", "profile": "max"}
    done = run_cli("server", *command)
    assert "Traceback" not in done.output and isinstance(done.exception, SystemExit | None)
    shown = done.output + "".join(questions)
    assert all(chr(n) not in shown for n in (27, 155, 8238, 8232, 133))
    assert not any(method == "PUT" for method, _, _ in admin.calls)


def test_large_timestamp_is_a_question_mark(admin, monkeypatch):
    monkeypatch.setattr(
        "latet.cli.server.admin_call", lambda *args, **kwargs: {"identities": [{**EMILY, "last_seen": 1e300}]}
    )
    done = run_cli("server", "people", "list")
    assert done.exit_code == 1 and "?" in done.output


def test_invalid_code_is_not_printed(admin, monkeypatch):
    monkeypatch.setattr(
        "latet.cli.server.admin_call", lambda *args, **kwargs: {"identity": EMILY, "code": "unsafe;code"}
    )
    done = run_cli("server", "people", "add", "Emily Carter")
    assert done.exit_code == 1 and "unsafe;code" not in done.output


def test_zero_revoked_is_not_a_success(admin, monkeypatch):
    original = admin.__call__
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda method, path, body=None, **kwargs: (
            {"revoked": 0} if method == "DELETE" else original(method, path, body, **kwargs)
        ),
    )
    done = run_cli("server", "people", "tokens", "Emily Carter", "--revoke", "t_aaaaaaaaaa")
    assert done.exit_code == 1 and "nothing was revoked" in done.output


def test_term_caps_checked_before_write(admin):
    for value in ("!", "a" * 8193):
        term = json.dumps({"value": value})
        assert run_cli("server", "config", "--key", "Billing CI", "add", "terms", term).exit_code == 2
    assert not any(method == "PUT" for method, _, _ in admin.calls)


def test_id_patterns_match_server():
    from latet.cli import server
    from latet.server import identities
    from latet.server.connection import USER_ID

    assert server.TOKEN_ID.pattern == identities.TOKEN_ID.pattern
    assert server.ADMIN_TOKEN_ID.pattern == identities.ADMIN_TOKEN_ID.pattern
    assert USER_ID.pattern == identities.IDENTITY_ID.pattern


def test_private_token_bom_stdin_and_prompt(computer, monkeypatch):
    import io

    from latet.cli import server
    from latet.storage import folders

    token = "latet_adm_" + "r" * 43
    file = computer / "admin.token"
    file.write_bytes(b"\xef\xbb\xbf" + token.encode() + b"\n")
    file.chmod(0o600)
    monkeypatch.setattr(folders, "private_folder_problem", lambda _: None)
    monkeypatch.setattr(folders, "_windows_folder_problem", lambda _: None)
    monkeypatch.setattr(server, "_target", {"server": URL, "token_file": file, "fingerprint": None})
    assert server._admin_token(URL) == token
    server._target["token_file"] = "-"
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    assert server._admin_token(URL) == token
    server._target["token_file"] = None
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda: True)
    prompts = []
    monkeypatch.setattr(typer, "prompt", lambda question, hide_input: prompts.append((question, hide_input)) or token)
    assert server._admin_token(URL) == token
    assert prompts == [(f"Administration token for {URL}", True)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor permissions")
def test_private_token_refuses_public_file_and_fifo(computer, monkeypatch):
    from latet.cli import server
    from latet.server.connection import NotConnected

    token = "latet_adm_" + "r" * 43
    file = computer / "token"
    file.write_text(token)
    file.chmod(0o644)
    with pytest.raises(NotConnected, match="chmod 600"):
        server._private_read(file)
    file.unlink()
    os.mkfifo(file)
    with pytest.raises(NotConnected, match="regular"):
        server._private_read(file)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_private_token_refuses_windows_file_or_folder_grant(computer, monkeypatch):
    from latet.cli import server
    from latet.server.connection import NotConnected
    from latet.storage import folders

    file = computer / "token"
    file.write_text("latet_adm_" + "r" * 43)
    me = "S-1-5-21-1-2-3-1001"
    monkeypatch.setattr(folders, "my_sid", lambda: me)
    for exposed in (file, file.parent):
        monkeypatch.setattr(
            folders,
            "folder_grants",
            lambda path, exposed=exposed: (me, [folders.Grant("S-1-5-32-545", 1, False)] if path == exposed else []),
        )
        with pytest.raises(NotConnected, match="icacls"):
            server._private_read(file)


def test_token_file_record_bound_to_server(computer, monkeypatch):
    from latet.cli import server
    from latet.server.connection import NotConnected

    secret = "latet_adm_" + "r" * 43
    monkeypatch.setattr(server, "_target", {"server": URL, "token_file": computer / "token", "fingerprint": None})
    monkeypatch.setattr(server, "_private_read", lambda _: json.dumps({"url": URL, "token": secret}))
    with pytest.raises(NotConnected, match="another server"):
        server._admin_token("https://other.acme.lan")


@pytest.mark.parametrize(
    "command",
    [
        ("keys", "add", "Billing CI"),
        ("keys", "rotate", "Billing CI", "--yes"),
        ("keys", "add-token", "Billing CI", "logs", "--mask", "--unmask"),
        ("admin-token", "issue"),
    ],
)
def test_out_keeps_secret_out_of_output(admin, computer, monkeypatch, command):
    from latet.storage import folders

    monkeypatch.setattr(folders, "private_folder_problem", lambda _: None)
    file = computer / "issued.secret"
    done = run_cli("server", *command, "--out", str(file))
    assert done.exit_code == 0, done.output
    assert "latet_adm_" not in done.output and "latet_key_" not in done.output
    assert file.read_text()
    if sys.platform != "win32":
        assert file.stat().st_mode & 0o077 == 0
    before = list(admin.calls)
    repeated = run_cli("server", *command, "--out", str(file))
    assert repeated.exit_code == 2
    assert admin.calls == before  # an existing output file must not issue another unseen credential


def test_private_output_creation_is_exclusive_even_after_a_preflight_race(computer, monkeypatch):
    from latet.cli import server

    file = computer / "issued.secret"
    marker = "another writer's existing content"
    monkeypatch.setattr(server, "_check_out", lambda path: path.write_text(marker))
    with pytest.raises(typer.Exit) as stopped:
        server._private_write(file, "new secret, which must not overwrite the winner\n")
    assert getattr(stopped.value, "exit_code", None) == 1
    assert file.read_text() == marker


def test_log_full_page_is_bounded_and_announces_exact_continuation(admin, monkeypatch):
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(kwargs)
        return {
            "entries": [
                {"id": i, "ts": 123.5, "actor": "admin:a_aaaaaaaaaa", "action": "person.added"} for i in range(10000)
            ]
        }

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "10001", "--json")
    assert done.exit_code == 0, done.output[:200]
    assert len(done.stdout.splitlines()) == 10000
    assert "Older entries" in done.stderr and "exact ts and id" in done.stderr
    assert seen[0]["query"]["limit"] == 10000


def test_log_reads_two_valid_pages_with_exact_cursor_and_remaining_limit(admin, monkeypatch):
    from latet.log_paging import LogCursor, LogFilters

    filters = LogFilters(since=100.0, until=200.0, action="person.added", actor="admin:a_aaaaaaaaaa")
    entries = [
        {
            "id": row,
            "ts": 123.5,
            "actor": filters.actor,
            "action": filters.action,
            "target": None,
            "details": {},
            "address": None,
            "prev": "",
            "hash": "ab" * 32,
        }
        for row in (3, 2, 1)
    ]
    cursor = LogCursor(123.5, 2, 3, filters.digest()).encode()
    pages = [
        {"entries": entries[:2], "next_cursor": cursor},
        {"entries": entries[2:], "next_cursor": None},
    ]
    seen = []

    def answer(method, path, body=None, **kwargs):
        assert (method, path, body) == ("GET", "log", None)
        seen.append(dict(kwargs["query"]))
        assert len(seen) <= len(pages), "the exhausted selection must not be fetched again"
        return pages[len(seen) - 1]

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli(
        "server",
        "log",
        "--limit",
        "3",
        "--json",
        "--since",
        "100",
        "--until",
        "200",
        "--action",
        "person.added",
        "--actor",
        "admin:a_aaaaaaaaaa",
    )
    assert done.exit_code == 0, done.output[:200]
    emitted = [json.loads(line) for line in done.stdout.splitlines()]
    assert emitted[:2] == entries[:2]
    assert len(seen) == 2
    fixed = {"since": 100.0, "until": 200.0, "action": "person.added", "actor": "admin:a_aaaaaaaaaa"}
    assert seen == [{"limit": 3, **fixed}, {"limit": 1, **fixed, "cursor": cursor}]
    assert emitted == entries


def test_log_accepts_one_valid_page_with_explicit_exhaustion(admin, monkeypatch):
    entry = {
        "id": 1,
        "ts": 123.5,
        "actor": "admin:a_aaaaaaaaaa",
        "action": "person.added",
        "target": None,
        "details": {},
        "address": None,
        "prev": "",
        "hash": "ab" * 32,
    }
    seen = []

    def answer(method, path, body=None, **kwargs):
        assert (method, path, body) == ("GET", "log", None)
        seen.append(dict(kwargs["query"]))
        assert len(seen) == 1, "explicit exhaustion must not fetch another page"
        return {"entries": [entry], "next_cursor": None}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 0, done.output[:200]
    assert seen == [{"limit": 3}]
    assert [json.loads(line) for line in done.stdout.splitlines()] == [entry]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_metadata",
        "missing_entries",
        "ascending",
        "cursor_pair",
        "ceiling",
        "empty",
        "repeated_cursor",
        "count",
        "actor",
        "action",
        "window",
    ],
)
def test_log_rejects_bad_second_page_without_emitting_it(admin, monkeypatch, fault):
    from latet.log_paging import LogCursor, LogFilters

    filters = LogFilters(since=100.0, until=200.0, action="person.added", actor="admin:a_aaaaaaaaaa")
    entries = [
        {
            "id": row,
            "ts": 123.5,
            "actor": filters.actor,
            "action": filters.action,
            "target": None,
            "details": {},
            "address": None,
            "prev": "",
            "hash": "ab" * 32,
        }
        for row in (5, 4, 3, 2)
    ]
    cursor = LogCursor(123.5, 4, 5, filters.digest()).encode()
    second = {"entries": entries[2:], "next_cursor": None}
    if fault == "missing_metadata":
        second.pop("next_cursor")
    elif fault == "missing_entries":
        second.pop("entries")
    elif fault == "ascending":
        second["entries"] = list(reversed(entries[2:]))
    elif fault == "cursor_pair":
        second["next_cursor"] = LogCursor(123.5, 3, 5, filters.digest()).encode()
    elif fault == "ceiling":
        second["next_cursor"] = LogCursor(123.5, 2, 6, filters.digest()).encode()
    elif fault == "empty":
        second = {"entries": [], "next_cursor": LogCursor(123.5, 3, 5, filters.digest()).encode()}
    elif fault == "repeated_cursor":
        second["next_cursor"] = cursor
    elif fault == "count":
        second["entries"] = [*entries[2:], {**entries[-1], "id": 1}]
    elif fault == "actor":
        second["entries"] = [{**entries[2], "actor": "admin:a_aaaaaaaaba"}, entries[3]]
    elif fault == "action":
        second["entries"] = [{**entries[2], "action": "person.removed"}, entries[3]]
    else:
        second["entries"] = [{**entries[2], "ts": 99.0}, {**entries[3], "ts": 98.0}]
    pages = [{"entries": entries[:2], "next_cursor": cursor}, second]
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        # An overfetch remains observable without a fake exception masquerading as a controlled CLI refusal.
        return pages[len(seen) - 1] if len(seen) <= 2 else {"entries": [], "next_cursor": None}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli(
        "server",
        "log",
        "--limit",
        "4",
        "--json",
        "--since",
        "100",
        "--until",
        "200",
        "--action",
        "person.added",
        "--actor",
        "admin:a_aaaaaaaaaa",
    )
    assert done.exit_code == 1, done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == entries[:2]
    assert len(seen) == 2
    assert "incomplete" in done.stderr.lower() and "2 entries" in done.stderr
    assert cursor in done.stderr


def test_log_validates_the_whole_first_page_before_any_output(admin, monkeypatch):
    entries = [
        {
            "id": row,
            "ts": 123.5,
            "actor": "server",
            "action": "server.started",
            "target": None,
            "details": {},
            "address": None,
            "prev": "",
            "hash": "ab" * 32,
        }
        for row in (1, 2)
    ]
    monkeypatch.setattr(
        "latet.cli.server.admin_call", lambda *args, **kwargs: {"entries": entries, "next_cursor": None}
    )
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 1, done.output[:200]
    assert done.stdout == ""
    assert "incomplete" in done.stderr.lower() and "0 entries" in done.stderr


def _cli_log_row(row, stamp=123.5):
    return {
        "id": row,
        "ts": stamp,
        "actor": "server",
        "action": "server.started",
        "target": None,
        "details": {},
        "address": None,
        "prev": "",
        "hash": "ab" * 32,
    }


@pytest.mark.parametrize("page_count", [2, 3, 4])
def test_log_request_budget_stops_before_an_extra_fetch(admin, monkeypatch, page_count):
    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    monkeypatch.setattr(server, "_LOG_REQUEST_MOST", 3)
    rows = [_cli_log_row(row) for row in range(4, 4 - page_count, -1)]
    cursors = [LogCursor(123.5, row["id"], 4, LogFilters().digest()).encode() for row in rows]
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        index = len(seen) - 1
        return {"entries": [rows[index]], "next_cursor": cursors[index] if index + 1 < page_count else None}

    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "log", "--limit", "10", "--json")
    assert done.exit_code == (1 if page_count > 3 else 0), done.output[:200]
    assert len(seen) == min(page_count, 3)
    assert [json.loads(line) for line in done.stdout.splitlines()] == rows[:3]
    if page_count > 3:
        assert "incomplete" in done.stderr.lower() and "3 entries" in done.stderr
        assert cursors[2] in done.stderr


@pytest.mark.parametrize("allowance_delta", [-1, 0, 1])
def test_log_receive_budget_bounds_two_actual_https_pages(latet_server, monkeypatch, allowance_delta):
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    from .fake_latet_server import ADMIN_TOKEN, API

    rows = [_cli_log_row(2), _cli_log_row(1)]
    cursor = LogCursor(123.5, 2, 2, LogFilters().digest()).encode()
    pages = [
        JSONResponse({"entries": rows[:1], "next_cursor": cursor}),
        JSONResponse({"entries": rows[1:], "next_cursor": None}),
    ]
    monkeypatch.setattr(server, "_LOG_RECEIVE_MOST", sum(len(page.body) for page in pages) + allowance_delta)
    monkeypatch.setenv("LATET_SERVER_URL", latet_server.url)
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", latet_server.fingerprint)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", ADMIN_TOKEN)
    seen = []

    async def page(request):
        assert request.headers["x-latet-admin"] == ADMIN_TOKEN
        seen.append(dict(request.query_params))
        return pages[0 if "cursor" not in request.query_params else 1]

    latet_server.app.router.routes.append(Route(f"{API}/admin/log", page, methods=["GET"]))
    done = run_cli("server", "log", "--limit", "2", "--json")
    assert done.exit_code == (1 if allowance_delta < 0 else 0), done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == (rows[:1] if allowance_delta < 0 else rows)
    assert len(seen) == 2
    if allowance_delta < 0:
        assert "incomplete" in done.stderr.lower() and "1 entry" in done.stderr
        assert cursor in done.stderr


@pytest.mark.parametrize("allowance_delta", [-1, 0, 1])
def test_log_render_budget_counts_escaped_utf8_before_output(admin, monkeypatch, allowance_delta):
    from latet.cli import server

    entry = _cli_log_row(1)
    entry["details"] = {"kind": chr(0xE9) + chr(0x202E)}
    expected = json.dumps(entry, ensure_ascii=True) + "\n"
    monkeypatch.setattr(server, "_LOG_RENDER_MOST", len(expected.encode("utf-8")) + allowance_delta)
    monkeypatch.setattr(server, "admin_call", lambda *args, **kwargs: {"entries": [entry], "next_cursor": None})
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == (1 if allowance_delta < 0 else 0), done.output[:200]
    assert done.stdout == ("" if allowance_delta < 0 else expected)
    if allowance_delta < 0:
        assert "incomplete" in done.stderr.lower() and "0 entries" in done.stderr
        assert "fewer rows" in done.stderr.lower()


def test_log_unprinted_second_page_resumes_from_its_request_cursor(admin, monkeypatch):
    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    rows = [_cli_log_row(row) for row in (3, 2, 1)]
    cursor = LogCursor(123.5, 3, 3, LogFilters().digest()).encode()
    rendered = [json.dumps(row, ensure_ascii=True) + "\n" for row in rows]
    monkeypatch.setattr(server, "_LOG_RENDER_MOST", sum(len(line.encode("utf-8")) for line in rendered) - 1)
    pages = [{"entries": rows[:1], "next_cursor": cursor}, {"entries": rows[1:], "next_cursor": None}]
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        return pages[len(seen) - 1]

    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 1, done.output[:200]
    assert done.stdout == rendered[0]
    assert len(seen) == 2
    assert "incomplete" in done.stderr.lower() and "1 entry" in done.stderr
    assert cursor in done.stderr


@pytest.mark.parametrize("stage", ["read", "render", "write"])
def test_log_total_deadline_covers_read_render_and_write_before_another_fetch(admin, monkeypatch, stage):
    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    now = [0.0]
    monkeypatch.setattr(server, "_LOG_SECONDS", 10.0)
    monkeypatch.setattr(server, "_log_clock", lambda: now[0])
    rows = [_cli_log_row(2), _cli_log_row(1)]
    cursor = LogCursor(123.5, 2, 2, LogFilters().digest()).encode()
    pages = [{"entries": rows[:1], "next_cursor": cursor}, {"entries": rows[1:], "next_cursor": None}]
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        if stage == "read":
            now[0] = 11.0
        return pages[len(seen) - 1]

    original_json, original_echo = server.json.dumps, server.typer.echo

    def render(value, *args, **kwargs):
        result = original_json(value, *args, **kwargs)
        if isinstance(value, dict) and "actor" in value:
            now[0] = 11.0
        return result

    def write(value, *args, **kwargs):
        result = original_echo(value, *args, **kwargs)
        if not kwargs.get("err", False):
            now[0] = 11.0
        return result

    monkeypatch.setattr(server, "admin_call", answer)
    if stage == "render":
        monkeypatch.setattr(server.json, "dumps", render)
    if stage == "write":
        monkeypatch.setattr(server.typer, "echo", write)
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 1, done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == (rows[:1] if stage == "write" else [])
    assert len(seen) == 1
    assert "incomplete" in done.stderr.lower()
    assert f"{1 if stage == 'write' else 0} entr" in done.stderr


def test_log_short_legacy_page_is_explicitly_incomplete(admin, monkeypatch):
    entry = _cli_log_row(1)
    monkeypatch.setattr("latet.cli.server.admin_call", lambda *args, **kwargs: {"entries": [entry]})
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 0, done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == [entry]
    assert "legacy" in done.stderr.lower() and "incomplete" in done.stderr.lower()


@pytest.mark.parametrize("row_id", [2, 4])
def test_log_initial_cursor_keeps_exact_filters_and_its_boundary(admin, monkeypatch, row_id):
    from latet.log_paging import LogCursor, LogFilters

    cursor = LogCursor(123.5, 3, 3, LogFilters(since=100.0, until=200.0).digest()).encode()
    entry = _cli_log_row(row_id)
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        return {"entries": [entry], "next_cursor": None}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "1", "--json", "--since", "100", "--until", "200", "--cursor", cursor)
    assert done.exit_code == (1 if row_id == 4 else 0), done.output[:200]
    assert seen == [{"limit": 1, "since": 100.0, "until": 200.0, "cursor": cursor}]
    assert [json.loads(line) for line in done.stdout.splitlines()] == ([] if row_id == 4 else [entry])
    if row_id == 4:
        assert "incomplete" in done.stderr.lower() and "0 entries" in done.stderr
        assert cursor in done.stderr


@pytest.mark.parametrize("fault", ["malformed", "oversized", "filters"])
def test_log_invalid_initial_cursor_never_fetches_or_echoes_input(admin, monkeypatch, fault):
    from latet.log_paging import LogCursor, LogFilters

    cursor = (
        "untrusted-cursor-marker"
        if fault == "malformed"
        else "a" * 1025
        if fault == "oversized"
        else LogCursor(123.5, 3, 3, LogFilters(since=101.0, until=200.0).digest()).encode()
    )
    seen = []

    def answer(*args, **kwargs):
        seen.append(kwargs)
        return {"entries": [], "next_cursor": None}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "1", "--json", "--since", "100", "--until", "200", "--cursor", cursor)
    assert done.exit_code != 0
    assert done.stdout == "" and seen == []
    assert cursor not in done.stderr


def test_log_numeric_filters_keep_finite_values_outside_datetime_display(admin, monkeypatch):
    seen = []
    entry = _cli_log_row(1, 1e299)

    def answer(*args, **kwargs):
        seen.append(dict(kwargs["query"]))
        return {"entries": [entry], "next_cursor": None}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "1", "--json", "--since=-1e300", "--until=1e300")
    assert done.exit_code == 0, done.output[:200]
    assert seen == [{"limit": 1, "since": -1e300, "until": 1e300}]
    assert [json.loads(line) for line in done.stdout.splitlines()] == [entry]


def test_log_relative_filter_is_resolved_once_while_wall_clock_advances(admin, monkeypatch):
    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    now, clock_reads = [1000.0], []

    def wall_clock():
        clock_reads.append(now[0])
        return now[0]

    monkeypatch.setattr(server.time, "time", wall_clock)
    rows = [_cli_log_row(2, 950.0), _cli_log_row(1, 940.0)]
    cursor = LogCursor(950.0, 2, 2, LogFilters(since=940.0, until=2000.0).digest()).encode()
    seen = []

    def answer(method, path, body=None, **kwargs):
        seen.append(dict(kwargs["query"]))
        now[0] += 100.0
        return (
            {"entries": rows[:1], "next_cursor": cursor}
            if len(seen) == 1
            else {"entries": rows[1:], "next_cursor": None}
        )

    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "log", "--limit", "2", "--json", "--since", "1m", "--until", "2000")
    assert done.exit_code == 0, done.output[:200]
    assert clock_reads == [1000.0]
    assert seen == [
        {"limit": 2, "since": 940.0, "until": 2000.0},
        {"limit": 1, "since": 940.0, "until": 2000.0, "cursor": cursor},
    ]
    assert [json.loads(line) for line in done.stdout.splitlines()] == rows


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", True),
        ("id", 0),
        ("id", 2**63),
        ("ts", True),
        ("ts", float("nan")),
        ("ts", "123.5"),
        ("actor", None),
        ("action", []),
        ("target", {}),
        ("details", []),
        ("address", 7),
        ("prev", None),
        ("hash", False),
    ],
)
def test_log_rejects_invalid_record_schema_atomically(admin, monkeypatch, field, value):
    entry = {**_cli_log_row(1), field: value}
    monkeypatch.setattr(
        "latet.cli.server.admin_call", lambda *args, **kwargs: {"entries": [entry], "next_cursor": None}
    )
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 1, done.output[:200]
    assert done.stdout == ""
    assert "incomplete" in done.stderr.lower() and "0 entries" in done.stderr


@pytest.mark.parametrize("status", [0, 422])
def test_log_transport_failure_keeps_emitted_count_and_previous_cursor(admin, monkeypatch, status):
    from latet.log_paging import LogCursor, LogFilters

    entry = _cli_log_row(2)
    cursor = LogCursor(123.5, 2, 2, LogFilters().digest()).encode()
    seen = []

    def answer(*args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            return {"entries": [entry], "next_cursor": cursor}
        raise ServerError(status, "the log entry cannot be returned" if status == 422 else "transport interrupted")

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code == 1, done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == [entry]
    assert len(seen) == 2
    assert "incomplete" in done.stderr.lower() and "1 entry" in done.stderr
    assert cursor in done.stderr


@pytest.mark.parametrize("failure", [BrokenPipeError, KeyboardInterrupt])
def test_log_output_interruption_is_controlled_and_never_fetches_again(admin, monkeypatch, failure):
    from latet.cli import server
    from latet.log_paging import LogCursor, LogFilters

    entry = _cli_log_row(2)
    cursor = LogCursor(123.5, 2, 2, LogFilters().digest()).encode()
    seen = []
    original_echo = server.typer.echo

    def answer(*args, **kwargs):
        seen.append(kwargs)
        return {"entries": [entry], "next_cursor": cursor}

    def write(*args, **kwargs):
        if not kwargs.get("err", False):
            raise failure
        return original_echo(*args, **kwargs)

    monkeypatch.setattr(server, "admin_call", answer)
    monkeypatch.setattr(server.typer, "echo", write)
    done = run_cli("server", "log", "--limit", "3", "--json")
    assert done.exit_code != 0
    assert done.stdout == "" and len(seen) == 1
    assert "incomplete" in done.stderr.lower() and "0 entries" in done.stderr


@pytest.mark.parametrize("setup_seconds", [1.0, 11.0])
def test_log_absolute_deadline_is_not_restarted_after_admin_token_setup(latet_server, monkeypatch, setup_seconds):
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from latet.cli import server

    from .fake_latet_server import ADMIN_TOKEN, API

    now = [0.0]
    monkeypatch.setattr(server, "_LOG_SECONDS", 10.0)
    monkeypatch.setattr(server, "_log_clock", lambda: now[0])
    monkeypatch.setenv("LATET_SERVER_URL", latet_server.url)
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", latet_server.fingerprint)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", ADMIN_TOKEN)
    original = server._admin_token
    entry = _cli_log_row(1)
    seen = []

    async def page(request):
        assert request.headers["x-latet-admin"] == ADMIN_TOKEN
        seen.append(dict(request.query_params))
        return JSONResponse({"entries": [entry], "next_cursor": None})

    latet_server.app.router.routes.append(Route(f"{API}/admin/log", page, methods=["GET"]))

    def delayed(url):
        token = original(url)
        now[0] = setup_seconds
        return token

    monkeypatch.setattr(server, "_admin_token", delayed)
    done = run_cli("server", "log", "--limit", "1", "--json")
    if setup_seconds > 10.0:
        assert seen == []
        assert done.exit_code != 0 and done.stdout == ""
        assert "incomplete" in done.stderr.lower()
    else:
        assert seen == [{"limit": "1"}]
        assert done.exit_code == 0, done.output[:200]
        assert [json.loads(line) for line in done.stdout.splitlines()] == [entry]


@pytest.mark.parametrize("elapsed", [9.0, 10.0, 11.0])
def test_log_total_deadline_accepts_below_and_exact_but_refuses_above(admin, monkeypatch, elapsed):
    from latet.cli import server

    now = [0.0]
    monkeypatch.setattr(server, "_LOG_SECONDS", 10.0)
    monkeypatch.setattr(server, "_log_clock", lambda: now[0])
    entry = _cli_log_row(1)

    def answer(*args, **kwargs):
        now[0] = elapsed
        return {"entries": [entry], "next_cursor": None}

    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "log", "--limit", "1", "--json")
    assert done.exit_code == (1 if elapsed > 10.0 else 0), done.output[:200]
    assert [json.loads(line) for line in done.stdout.splitlines()] == ([] if elapsed > 10.0 else [entry])


def test_write_timeout_says_check_list_and_is_not_retried(admin, monkeypatch):
    original = admin.__call__
    writes = []

    def answer(method, path, body=None, **kwargs):
        if method == "POST":
            writes.append(path)
            raise ServerError(0, "did not answer in time")
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 1 and "may have made" in done.output and "keys tokens u_billingci2" in done.output
    assert len(writes) == 1  # a rotation changes a token: the key's tokens are what to check


def test_status_does_not_fetch_answer_address(admin, monkeypatch):
    asked = []
    monkeypatch.setattr(
        "latet.engine.reviewer.certificate.fetch", lambda url: asked.append(url) or (_ for _ in ()).throw(OSError())
    )
    original = admin.__call__
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda *args, **kwargs: {**original(*args, **kwargs), "url": "https://10.9.9.9:22"},
    )
    done = run_cli("server", "status")
    assert done.exit_code == 0
    assert asked == [URL]


EMILY = {"id": "u_emilycarte", "name": "Emily Carter", "email": "emily.carter@acme.com", "kind": "person",
         "disabled": False, "created": 0, "last_seen": None, "source": "local", "tokens": 1}  # fmt: skip
BILLING = {"id": "u_billingci2", "name": "Billing CI", "email": None, "kind": "key",
           "disabled": False, "created": 0, "last_seen": None, "source": "local", "tokens": 0}  # fmt: skip


EVERY = ["mask", "scan", "unmask"]


def _a_token(token_id, name, permissions, used=None, agents=False):
    return {"id": token_id, "label": name, "name": name, "created": 1_790_000_000, "last_used": used,
            "tampered": False, "permissions": permissions, "agents": agents}  # fmt: skip


class Admin:
    """The administration API, as Ruling 1 says it answers: what was asked, and the answers."""

    def __init__(self):
        from latet.server.shared import SETTING_KEYS

        self.calls = []
        self.tokens = [_a_token("t_aaaaaaaaaa", "key", EVERY, agents=True)]  # Billing CI's, as the server lists them
        self.secrets = []  # the secrets it issued to Billing CI, in order
        dumped = Config().model_dump(mode="json")
        self.shared = {key: dumped[key] for key in SETTING_KEYS}  # the settings the server shares
        self.kept = {}  # the rules for everyone an earlier version kept among them
        self.own = Config().model_dump(mode="json")  # Billing CI's: the shared ones with its own on top
        self.etag = "e1"
        self.limit = None  # Billing CI's own limit of values in its vault; None: the server's
        self.values = 0  # the values its vault holds
        self.vault: set[str] = set()  # the values its vault holds, for a forget
        self.forget_waits: list[tuple[float | None, float | None]] = []  # each forget's read timeout and deadline

    def _secret(self):
        """A new secret for Billing CI, one letter for each issued."""
        self.secrets.append("latet_key_" + chr(ord("b") + len(self.secrets)) * 43)
        return self.secrets[-1]

    def _token_made(self, name, permissions, agents=False):
        made = _a_token(f"t_{chr(ord('b') + len(self.secrets)) * 10}", name, permissions, agents=agents)
        self.tokens.append(made)
        return made

    def _billing(self, method, path, body):
        """Billing CI's tokens, as the server answers them: the permissions as Latet reads them (mask brings scan),
        addresses only for a token that serves agents and holds mask and unmask (the key's first: an added token has
        none, Ruling T13b), a rotation that names its token when the key has several, and a token that is none of the
        key's is none."""
        if (method, path) == ("GET", "identities/u_billingci2/tokens"):
            return {"tokens": json.loads(json.dumps(self.tokens))}
        if (method, path) == ("POST", "identities/u_billingci2/tokens"):
            assert sorted(body) == ["name", "permissions"] and body["permissions"], body
            granted = sorted({*body["permissions"], *(["scan"] if "mask" in body["permissions"] else [])})
            secret, made = self._secret(), self._token_made(body["name"], granted)
            return {"token": {key: made[key] for key in ("id", "name", "permissions", "created")}, "key": secret}
        if method == "DELETE" and path.startswith("identities/u_billingci2/tokens?token="):
            gone = [t for t in self.tokens if t["id"] == path.rsplit("=", 1)[1]]
            self.tokens = [t for t in self.tokens if t not in gone]
            return {"revoked": len(gone)}
        assert (method, path) == ("POST", "identities/u_billingci2/rotate"), (method, path)
        named = (body or {}).get("token")
        if named is None and len(self.tokens) > 1:
            raise ServerError(409, f"this key has {len(self.tokens)} tokens: name the one to rotate")
        old = next((t for t in self.tokens if t["id"] == named), None) if named else next(iter(self.tokens), None)
        if named and old is None:
            raise ServerError(404, "there is no such token of this API key")
        name, granted, agents = (old["name"], old["permissions"], old["agents"]) if old else ("key", EVERY, True)
        self.tokens = [t for t in self.tokens if t is not old]
        secret = self._secret()
        made = self._token_made(name, granted, agents)
        return {"key": secret, "name": name, "permissions": granted, "token": made["id"]} | (
            self._addresses(secret) if agents and {"mask", "unmask"} <= set(granted) else {}
        )

    def _limited(self, body):
        """Billing CI's vault limit set, as the server answers: the range kept, the values its vault holds said with it."""
        assert set(body) == {"vault_limit"}, body
        limit = body["vault_limit"]
        if limit is not None and (type(limit) is not int or not 1000 <= limit <= 100_000_000):
            raise ServerError(
                400, "vault_limit is a whole number from 1000 to 100000000 values, or null for the server's limit"
            )
        self.limit = limit
        return {"identity": {**BILLING, "vault_limit": limit, "vault_values": self.values}}

    @staticmethod
    def _addresses(secret):
        base = f"{URL}/u/{route_key(secret)}"
        return {"base": base, "anthropic": f"{base}/anthropic", "openai": f"{base}/openai/v1"}

    def __call__(self, method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        self.calls.append((method, path, body))
        if path.startswith(("identities/u_billingci2/tokens", "identities/u_billingci2/rotate")):
            return self._billing(method, path, body)
        if (method, path) == ("POST", "identities/u_billingci2/forget"):
            assert set(body) == {"values"} and 1 <= len(body["values"]) <= 1000, body
            self.forget_waits.append((kwargs.get("timeout"), kwargs.get("deadline")))
            asked = set(body["values"])
            gone = asked & self.vault
            self.vault -= gone
            return {"forgotten": len(gone), "unknown": len(asked) - len(gone)}
        if path == "identities/u_billingci2/settings":
            if method == "GET":
                return {"settings": json.loads(json.dumps(self.own)), "etag": "k1"}
            self.own = {**self.own, **{key: value for key, value in body.items() if key != "etag"}}
            return {"settings": self.own, "etag": "k2"}
        if (method, path) == ("GET", "identities?kind=person"):
            return {"identities": [EMILY]}
        if (method, path) == ("GET", "identities?kind=key"):
            return {"identities": [BILLING]}
        if (method, path) == ("POST", "identities"):
            if body["kind"] == "key":
                return {"identity": BILLING, "key": KEY}
            return {"identity": {**EMILY, "name": body["name"]}, "code": "code_1234"}
        if method == "PATCH" and path == "identities/u_billingci2" and "vault_limit" in body:
            return self._limited(body)
        if method == "PATCH":
            return {"identity": {**EMILY, **body}}
        if path.endswith("/rotate"):
            return {"key": "latet_key_" + "n" * 43}
        if path.endswith("/code"):
            return {"code": "code_5678"}
        if (method, path) == ("GET", "shared"):
            return {
                "settings": json.loads(json.dumps(self.shared)),
                "etag": self.etag,
                "kept_rules": json.loads(json.dumps(self.kept)),
            }
        if (method, path) == ("PUT", "shared"):
            from latet.server.shared import RULE_KEYS, RULES_FOR_EVERYONE

            if any(key in RULE_KEYS for key in body):
                raise ServerError(400, RULES_FOR_EVERYONE)
            if body.get("etag") is not None and body["etag"] != self.etag:
                raise ServerError(409, "these settings changed since you read them: read them again")
            if body.get("forget_kept_rules") is True:
                self.kept = {}
            settings = {key: value for key, value in body.items() if key not in ("etag", "forget_kept_rules")}
            self.shared, self.etag = {**self.shared, **settings}, "e2"  # a key the write leaves out keeps its value
            return {"settings": self.shared, "etag": self.etag, "kept_rules": self.kept}
        if (method, path) == ("GET", "log/verify"):
            return {"ok": True, "checked": 12, "broken_at": None}
        if path.startswith("log"):
            entry = {"id": 1, "ts": 1_793_000_000, "actor": "admin:a_aaaaaaaaaa", "action": "person.added",
                     "target": "u_emilycarte", "details": {"name": "Emily Carter"}, "address": "10.0.0.5",
                     "prev": "", "hash": "ab" * 32}  # fmt: skip
            return {"entries": [entry]}
        if (method, path) == ("GET", "admin-tokens"):
            first = {"id": "a_aaaaaaaaaa", "label": "the first, by latet server init", "created": 1_790_000_000,
                     "last_used": 1_793_000_000, "expires": 1_797_776_000}  # fmt: skip
            backup = {
                "id": "a_aaaaaaaaba",
                "label": "the backup",
                "created": 1_793_000_000,
                "last_used": None,
                "expires": None,
            }
            return {"tokens": [first, backup]}
        if (method, path) == ("POST", "admin-tokens"):
            expires = None if body.get("never") else 1_800_776_000
            return {"id": "a_aaaaaaaaba", "token": "latet_adm_" + "z" * 43, "expires": expires}
        if method == "DELETE" and path.startswith("admin-tokens/"):
            return {"revoked": True}
        if (method, path) == ("GET", "status"):
            return {
                "url": URL, "version": "1.0.0", "identities": {"people": 1, "keys": 1, "enabled": 2},
                "vault": {"limit": 200000, "near": [{"id": "u_billingci2", "values": 181000, "limit": 200000}]},
                "audit": {"chain": "ok"}, "edition": [],
            }  # fmt: skip
        return {"ok": True}


@pytest.fixture
def admin(computer, monkeypatch):
    fake = Admin()
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "r" * 43)
    monkeypatch.setattr("latet.cli.server.admin_call", fake)
    return fake


def test_a_person_is_added_with_the_command_they_run(admin):
    done = run_cli("server", "people", "add", "Daniel Okafor", "--email", "daniel.okafor@acme.com")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == (
        "POST",
        "identities",
        {"kind": "person", "name": "Daniel Okafor", "email": "daniel.okafor@acme.com"},
    )
    assert f"latet connect {URL}" in done.output


def test_a_person_is_found_by_id_name_or_email_and_disabled(admin):
    for who in ("u_emilycarte", "emily carter", "EMILY.CARTER@acme.com"):
        assert run_cli("server", "people", "disable", who).exit_code == 0
    assert admin.calls[-1] == ("PATCH", "identities/u_emilycarte", {"disabled": True})
    assert run_cli("server", "people", "disable", "Nobody").exit_code == 1


def test_removing_asks_first(admin):
    assert run_cli("server", "people", "remove", "Emily Carter").exit_code == 2  # no terminal of yours here
    assert all(call[0] != "DELETE" for call in admin.calls)
    assert run_cli("server", "people", "remove", "Emily Carter", "--yes").exit_code == 0
    assert admin.calls[-1][:2] == ("DELETE", "identities/u_emilycarte")


def test_an_api_key_is_shown_once_with_the_programs_variables(admin):
    done = run_cli("server", "keys", "add", "Billing CI")
    assert done.exit_code == 0 and KEY in done.output
    assert f"{URL}/u/{route_key(KEY)}/openai/v1" in done.output and "--key-file" in done.output
    assert "latet server config --key u_billingci2" in done.output
    rotated = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert rotated.exit_code == 0 and "within seconds" in rotated.output


BILLING_TOKENS = "identities/u_billingci2/tokens"
BILLING_ROTATE = "identities/u_billingci2/rotate"
BILLING_FORGET = "identities/u_billingci2/forget"


def test_a_mask_only_token_is_shown_once_and_has_no_address(admin):
    done = run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_TOKENS, {"name": "logs", "permissions": ["mask"]})
    [secret] = admin.secrets
    assert done.output.count(secret) == 1  # once
    assert "permissions: mask, scan" in done.output  # what the server granted: mask brings scan
    assert "/u/" not in done.output and "--key-file" not in done.output  # no address, no variables
    assert "no address" in done.output and "applications API" in done.output


def test_a_token_added_with_mask_and_unmask_has_no_address_either(admin):
    """Ruling T13b: a token added to a key, whatever it may do, calls the applications API alone; agents use the key's
    first token, whose address its rotation prints."""
    done = run_cli("server", "keys", "add-token", "Billing CI", "assistant", "--mask", "--unmask")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_TOKENS, {"name": "assistant", "permissions": ["mask", "unmask"]})
    [secret] = admin.secrets
    assert secret in done.output and "permissions: mask, scan, unmask" in done.output
    assert "/u/" not in done.output and "--key-file" not in done.output
    assert "no address" in done.output and "first token" in done.output
    assistant = admin.tokens[1]["id"]
    rotated = run_cli("server", "keys", "rotate", "Billing CI", "--token", assistant, "--yes")
    assert rotated.exit_code == 0 and "/u/" not in rotated.output and "no address" in rotated.output
    first = run_cli("server", "keys", "rotate", "Billing CI", "--token", "t_aaaaaaaaaa", "--yes")
    assert first.exit_code == 0 and f"{URL}/u/{route_key(admin.secrets[-1])}/openai/v1" in first.output


def test_a_token_asks_for_the_permissions_in_the_order_latet_names_them(admin):
    assert run_cli("server", "keys", "add-token", "Billing CI", "audit", "--unmask", "--scan").exit_code == 0
    assert admin.calls[-1][2] == {"name": "audit", "permissions": ["scan", "unmask"]}


def test_a_token_with_no_permission_is_refused_before_anything_is_sent(admin):
    done = run_cli("server", "keys", "add-token", "Billing CI", "logs")
    assert done.exit_code == 2
    assert "give at least one of --scan, --mask, --unmask and --forget" in done.output
    assert not admin.calls  # not even the key looked up


def test_a_token_may_be_given_forget_alone_or_with_others(admin):
    """Task 11b: forget is a permission of its own, given by name: it brings nothing else, and nothing brings it."""
    done = run_cli("server", "keys", "add-token", "Billing CI", "erasure", "--forget")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_TOKENS, {"name": "erasure", "permissions": ["forget"]})
    assert "permissions: forget." in done.output and "/u/" not in done.output and "no address" in done.output
    both = run_cli("server", "keys", "add-token", "Billing CI", "all", "--mask", "--unmask", "--forget")
    assert both.exit_code == 0 and admin.calls[-1][2]["permissions"] == ["mask", "unmask", "forget"]
    assert "permissions: forget, mask, scan, unmask." in both.output and "/u/" not in both.output
    listed = run_cli("server", "keys", "tokens", "Billing CI")
    assert "erasure forget last used" in listed.output and "all forget,mask,scan,unmask last used" in listed.output


VALUES = [f"customer.{n}@acme.it" for n in range(2500)]


def _values_file(folder, lines, *, end="\n", bom=False):
    path = folder / "values.txt"
    path.write_bytes(("﻿" if bom else "").encode() + end.join(lines).encode("utf-8") + end.encode())
    return path


def test_values_are_forgotten_from_a_file_a_thousand_at_a_time(admin, computer):
    """One value a line, each once (a line again, or an empty one, is no value more), in calls of a thousand at most:
    the counts are summed, and no value is printed."""
    path = _values_file(computer, [*VALUES[:1500], "", VALUES[0], *VALUES[1500:]], end="\r\n", bom=True)
    admin.vault = set(VALUES[::2])  # what the key's vault holds
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 0, done.output
    sent = [body["values"] for method, path_, body in admin.calls if (method, path_) == ("POST", BILLING_FORGET)]
    assert [len(values) for values in sent] == [1000, 1000, 500]
    assert [value for values in sent for value in values] == VALUES  # in order, each once, no line ending
    assert "1250 values forgotten from the vault of Billing CI (u_billingci2); 1250 were not in it." in done.output
    assert not any(value in done.output for value in VALUES)


def test_values_are_never_taken_from_the_command_line(admin):
    done = run_cli("server", "keys", "forget", "Billing CI", VALUES[0])
    assert done.exit_code == 2 and not admin.calls


def test_forget_asks_first_and_your_terminal_asks_even_with_yes(admin, computer, monkeypatch):
    path = _values_file(computer, VALUES[:3])
    assert run_cli("server", "keys", "forget", "Billing CI", "--file", str(path)).exit_code == 2  # no terminal here
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 1 and not any(method == "POST" for method, _, _ in admin.calls)
    assert "Forget 3 values" in questions[0] and "Billing CI" in questions[0] and URL in questions[0]


def test_forget_reads_the_values_you_type_at_your_terminal(admin, monkeypatch):
    """At your terminal, without a file: a value a line, an empty line ending them, then the question."""
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: True)
    admin.vault = {VALUES[1]}
    done = run_cli("server", "keys", "forget", "Billing CI", input=f"{VALUES[0]}\n{VALUES[1]}\n\n")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_FORGET, {"values": VALUES[:2]})
    assert "one a line" in done.output and "1 value forgotten" in done.output and "1 was not in it" in done.output


def test_forget_without_a_terminal_or_a_file_is_refused(admin):
    done = run_cli("server", "keys", "forget", "Billing CI", "--yes", input=f"{VALUES[0]}\n")
    assert done.exit_code == 2 and "--file" in done.output and not admin.calls


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_UNRECOGNIZED", "CODEX_HOME"])
def test_an_agent_cannot_forget(admin, computer, monkeypatch, marker):
    """The values are a person's data: an agent is never handed them, whichever of its variables marks it."""
    monkeypatch.setenv(marker, str(computer / "home" / ".codex") if marker == "CODEX_HOME" else "1")
    path = _values_file(computer, VALUES[:3])
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 2 and not admin.calls
    assert not any(value in done.output for value in VALUES[:3])


@pytest.mark.parametrize("given", ["missing", "empty", "not-utf8"])
def test_a_file_that_names_no_value_is_refused_before_anything_is_sent(admin, computer, given):
    path = computer / "values.txt"
    if given == "empty":
        path.write_text("\n\n", "utf-8")
    elif given == "not-utf8":
        path.write_bytes(b"caf\xe9@acme.it\n")
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 2 and not admin.calls and "caf" not in done.output


def test_a_forget_that_fails_half_way_says_how_many_were_forgotten(admin, computer, monkeypatch):
    path = _values_file(computer, VALUES)
    admin.vault = set(VALUES)
    original = admin.__call__
    calls = []

    def failing(method, path_, body=None, **kwargs):
        if (method, path_) == ("POST", BILLING_FORGET):
            calls.append(len(body["values"]))
            if len(calls) == 2:
                raise ServerError(503, "the key's vault could not be changed now, and nothing was forgotten: try again")
        return original(method, path_, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", failing)
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 1 and calls == [1000, 1000]
    assert "1000 values were forgotten before this" in done.output
    assert not any(value in done.output for value in VALUES)


def test_a_name_that_is_a_secret_is_refused_before_anything_is_sent(admin):
    secret = "latet_key_" + "z" * 43
    done = run_cli("server", "keys", "add-token", "Billing CI", secret, "--scan")
    assert done.exit_code == 2 and secret not in done.output and not admin.calls


@pytest.mark.parametrize(
    "name", ["logs latet_key_" + "z" * 43, "LOGS LATET_BRG_" + "Z" * 43, "ci latet_code_" + "q" * 32]
)
def test_a_name_that_holds_a_secret_anywhere_is_refused_before_anything_is_sent(admin, name):
    """The final review's M3: a name is kept in clear and listed to every administrator; one that holds a secret of
    Latet's anywhere, whatever its case, is refused as the server refuses it, not only one that begins with it."""
    done = run_cli("server", "keys", "add-token", "Billing CI", name, "--scan")
    assert done.exit_code == 2 and name.split()[1] not in done.output and not admin.calls


def test_the_server_refusing_a_name_is_said_in_its_words(admin, monkeypatch):
    said = "name is a text of 100 characters at most, with something left once its invisible characters are taken out"
    original = admin.__call__

    def refused(method, path, body=None, **kwargs):
        if method == "POST" and path == "identities/u_billingci2/tokens":
            raise ServerError(400, said)
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", refused)
    done = run_cli("server", "keys", "add-token", "Billing CI", "n" * 101, "--scan")
    assert done.exit_code == 1 and said in done.output and not admin.secrets


def test_a_keys_tokens_are_listed_with_their_names_and_permissions(admin):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    done = run_cli("server", "keys", "tokens", "Billing CI")
    assert done.exit_code == 0, done.output
    first, logs = (token["id"] for token in admin.tokens)
    # The final review's M2: the token that serves the key's agents (its address) is marked, not to be revoked by
    # mistake for one added with the same permissions.
    assert f"{first}  key mask,scan,unmask, serves agents (has an address) last used never" in done.output
    assert f"{logs}  logs mask,scan last used never" in done.output
    assert all(secret not in done.output for secret in admin.secrets)
    assert admin.calls[-1] == ("GET", BILLING_TOKENS, None)


def test_a_token_that_was_used_says_when_and_one_that_does_not_hold_says_so(admin):
    admin.tokens = [
        _a_token("t_aaaaaaaaaa", "key", EVERY, used=1_793_000_000),
        {**_a_token("t_bbbbbbbbbb", "audit", []), "tampered": True},
    ]
    done = run_cli("server", "keys", "tokens", "Billing CI")
    assert done.exit_code == 0, done.output
    assert "t_aaaaaaaaaa  key mask,scan,unmask last used 2026-" in done.output
    assert "t_bbbbbbbbbb  audit none last used never" in done.output
    assert "its row does not hold" in done.output


def test_a_key_with_no_token_says_how_to_give_it_one(admin):
    admin.tokens = []
    done = run_cli("server", "keys", "tokens", "Billing CI")
    assert done.exit_code == 0 and "no token" in done.output and "keys rotate" in done.output


def test_a_token_is_revoked_by_its_id_once_you_agree(admin):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    first, logs = (token["id"] for token in admin.tokens)
    done = run_cli("server", "keys", "tokens", "Billing CI", "--revoke", logs)
    assert done.exit_code == 2 and "Nothing changed" in done.output  # no terminal of yours here
    assert all(call[0] != "DELETE" for call in admin.calls)
    done = run_cli("server", "keys", "tokens", "Billing CI", "--revoke", logs, "--yes")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("DELETE", f"{BILLING_TOKENS}?token={logs}", None)
    assert [token["id"] for token in admin.tokens] == [first] and logs in done.output


def test_revoking_asks_the_question_with_the_tokens_name(admin, monkeypatch):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    logs = admin.tokens[1]["id"]
    done = run_cli("server", "keys", "tokens", "Billing CI", "--revoke", logs, "--yes")
    assert done.exit_code == 1 and not any(call[0] == "DELETE" for call in admin.calls)
    assert logs in questions[0] and "logs" in questions[0] and URL in questions[0] and "Billing CI" in questions[0]


def test_a_token_that_is_not_the_keys_is_not_revoked(admin):
    done = run_cli("server", "keys", "tokens", "Billing CI", "--revoke", "t_zzzzzzzzzz", "--yes")
    assert done.exit_code == 1 and "No token of" in done.output and "nothing was revoked" in done.output
    assert all(call[0] == "GET" for call in admin.calls)


def test_a_token_revoked_by_another_administrator_meanwhile_is_not_a_success(admin, monkeypatch):
    original = admin.__call__
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda method, path, body=None, **kwargs: (
            {"revoked": 0} if method == "DELETE" else original(method, path, body, **kwargs)
        ),
    )
    done = run_cli("server", "keys", "tokens", "Billing CI", "--revoke", "t_aaaaaaaaaa", "--yes")
    assert done.exit_code == 1 and "nothing was revoked" in done.output


def test_a_rotation_of_a_key_with_several_tokens_says_the_servers_words(admin):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    before = list(admin.tokens)
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 1
    assert "this key has 2 tokens: name the one to rotate" in done.output
    assert admin.tokens == before and len(admin.secrets) == 1  # nothing was rotated


def test_a_rotation_names_the_token_with_token(admin):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    first, logs = (token["id"] for token in admin.tokens)
    done = run_cli("server", "keys", "rotate", "Billing CI", "--token", logs, "--yes")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_ROTATE, {"token": logs})
    assert admin.tokens[0]["id"] == first and logs not in [t["id"] for t in admin.tokens]
    assert [(t["name"], t["permissions"]) for t in admin.tokens] == [("key", EVERY), ("logs", ["mask", "scan"])]
    assert admin.secrets[-1] in done.output and "within seconds" in done.output
    assert "logs" in done.output and "permissions: mask, scan" in done.output  # what the new secret may do
    assert "/u/" not in done.output  # a token with no address is given none


def test_a_rotation_of_a_full_token_prints_its_permissions_and_the_programs_variables(admin):
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 0, done.output
    assert "permissions: mask, scan, unmask" in done.output
    assert f"{URL}/u/{route_key(admin.secrets[-1])}/openai/v1" in done.output


def test_a_rotation_names_the_new_token_by_its_id_as_adding_one_does(admin):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    logs = admin.tokens[1]["id"]
    added = run_cli("server", "keys", "add-token", "Billing CI", "audit", "--scan")
    assert f"Token audit ({admin.tokens[2]['id']}), permissions: scan." in added.output
    done = run_cli("server", "keys", "rotate", "Billing CI", "--token", logs, "--yes")
    assert done.exit_code == 0, done.output
    new = next(t["id"] for t in admin.tokens if t["name"] == "logs")
    assert new != logs and f"Token logs ({new}), permissions: mask, scan." in done.output


@pytest.mark.parametrize(
    "command", [("rotate", "Billing CI", "--yes"), ("add-token", "Billing CI", "gw", "--mask", "--unmask")]
)
def test_an_answer_with_no_permissions_gets_no_address(admin, monkeypatch, command):
    """Fail closed: the address of a token is printed when the server says it holds mask and unmask, and for nothing
    less (a server that says nothing of permissions included)."""
    original = admin.__call__

    def without(method, path, body=None, **kwargs):
        answer = original(method, path, body, **kwargs)
        if method != "POST":
            return answer
        answer = {key: value for key, value in answer.items() if key != "permissions"}
        if isinstance(answer.get("token"), dict):  # an added token says it inside its own object
            answer["token"] = {key: value for key, value in answer["token"].items() if key != "permissions"}
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", without)
    done = run_cli("server", "keys", *command)
    assert done.exit_code == 0, done.output
    secret = admin.secrets[-1]
    assert secret in done.output  # the secret is still shown, once
    assert "/u/" not in done.output and "--key-file" not in done.output
    assert "permissions: ?" in done.output and "no address" in done.output


def test_a_rotation_without_token_names_the_only_token_it_listed(admin):
    """The key's tokens are listed first, and the one there is named: a token revoked meanwhile is not replaced by a
    new full one (the server would make one for a key with none, were none named)."""
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 0, done.output
    assert admin.calls[-2:] == [("GET", BILLING_TOKENS, None), ("POST", BILLING_ROTATE, {"token": "t_aaaaaaaaaa"})]


def test_a_token_revoked_between_the_listing_and_the_rotation_is_not_replaced(admin, monkeypatch):
    original = admin.__call__

    def meanwhile(method, path, body=None, **kwargs):
        answer = original(method, path, body, **kwargs)
        if (method, path) == ("GET", BILLING_TOKENS):
            admin.tokens.clear()  # another administrator revokes it after it was listed
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", meanwhile)
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 1 and "no such token" in done.output
    assert admin.tokens == [] and not admin.secrets  # no full token was made in its place


def test_a_key_with_no_token_is_rotated_into_a_new_one_with_none_named(admin):
    admin.tokens = []
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("POST", BILLING_ROTATE, None)
    assert [t["name"] for t in admin.tokens] == ["key"] and "permissions: mask, scan, unmask" in done.output


def test_a_token_that_is_not_in_the_list_is_not_rotated(admin):
    done = run_cli("server", "keys", "rotate", "Billing CI", "--token", "t_zzzzzzzzzz", "--yes")
    assert done.exit_code == 1 and "No token of" in done.output and "nothing was rotated" in done.output
    assert all(call[0] == "GET" for call in admin.calls)


def test_the_rotation_asks_which_token_in_its_question(admin, monkeypatch):
    run_cli("server", "keys", "add-token", "Billing CI", "logs", "--mask")
    logs = admin.tokens[1]["id"]
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    done = run_cli("server", "keys", "rotate", "Billing CI", "--token", logs)
    assert done.exit_code == 1 and not any(call[0] == "POST" and call[1] == BILLING_ROTATE for call in admin.calls)
    assert logs in questions[0] and "logs" in questions[0] and URL in questions[0]


@pytest.mark.parametrize(
    "command",
    [
        ("keys", "tokens", "Billing CI"),
        ("keys", "tokens", "Billing CI", "--revoke", "t_aaaaaaaaaa", "--yes"),
        ("keys", "rotate", "Billing CI", "--token", "t_aaaaaaaaaa", "--yes"),
    ],
)
def test_a_token_name_from_the_server_is_shown_without_its_terminal_controls(admin, command):
    danger = "name" + "".join(chr(n) for n in (27, 155, 8238, 8232, 133))
    admin.tokens = [_a_token("t_aaaaaaaaaa", danger, EVERY), _a_token("t_bbbbbbbbbb", "ok", ["scan"])]
    done = run_cli("server", *command)
    assert done.exit_code == 0, done.output
    assert all(chr(n) not in done.output for n in (27, 155, 8238, 8232, 133))


@pytest.mark.parametrize("command", [("keys", "tokens", "Billing CI"), ("keys", "rotate", "Billing CI", "--yes")])
@pytest.mark.parametrize(
    "answer",
    [
        {"tokens": "t_aaaaaaaaaa"},
        {"tokens": [1]},
        {"tokens": {"id": "t_aaaaaaaaaa"}},
    ],
)
def test_a_list_of_tokens_that_is_none_is_said_and_nothing_is_rotated(admin, monkeypatch, command, answer):
    original = admin.__call__
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda method, path, body=None, **kwargs: (
            answer if (method, path) == ("GET", BILLING_TOKENS) else original(method, path, body, **kwargs)
        ),
    )
    done = run_cli("server", *command)
    assert done.exit_code == 1 and "tokens is not a list of objects" in done.output
    assert all(call[0] == "GET" for call in admin.calls) and not admin.secrets


def test_what_the_server_says_of_a_tokens_permissions_is_shown_only_when_it_is_latets(admin):
    admin.tokens = [
        _a_token("t_aaaaaaaaaa", "odd", "mask"),
        _a_token("t_bbbbbbbbbb", "unknown", ["scan", "delete"]),
        _a_token("t_cccccccccc", "fine", ["scan", "mask", "scan"]),
    ]
    done = run_cli("server", "keys", "tokens", "Billing CI")
    assert done.exit_code == 0, done.output
    assert "t_aaaaaaaaaa  odd ? last used never" in done.output
    assert "t_bbbbbbbbbb  unknown ? last used never" in done.output
    assert "t_cccccccccc  fine mask,scan last used never" in done.output


def test_the_only_token_the_server_lists_with_an_id_that_is_none_is_never_named_or_rotated(admin):
    admin.tokens = [_a_token("../rotate?x=", "odd", EVERY)]
    listed = run_cli("server", "keys", "tokens", "Billing CI")
    assert listed.exit_code == 0 and "?  odd mask,scan,unmask last used never" in listed.output
    done = run_cli("server", "keys", "rotate", "Billing CI", "--yes")
    assert done.exit_code == 1 and "not one" in done.output and "nothing was rotated" in done.output
    assert all(call[0] == "GET" for call in admin.calls) and not admin.secrets


def test_a_keys_vault_limit_is_set(admin):
    done = run_cli("server", "keys", "limit", "Billing CI", "5000")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("PATCH", "identities/u_billingci2", {"vault_limit": 5000})
    assert "Billing CI" in done.output and "5000 values" in done.output and "within seconds" in done.output
    assert "holds" not in done.output  # nothing to warn of: its vault is empty
    assert admin.limit == 5000


def test_a_keys_vault_limit_goes_back_to_the_servers(admin):
    admin.limit = 5000
    done = run_cli("server", "keys", "limit", "Billing CI", "default")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1] == ("PATCH", "identities/u_billingci2", {"vault_limit": None})
    assert "the server's" in done.output and "within seconds" in done.output
    assert admin.limit is None


def test_a_limit_below_the_values_a_vault_holds_says_it_takes_no_new_one(admin):
    admin.values = 1500
    done = run_cli("server", "keys", "limit", "Billing CI", "1000")
    assert done.exit_code == 0, done.output
    assert admin.limit == 1000  # accepted, not refused
    assert "1500 values" in done.output and "no new value" in done.output
    again = run_cli("server", "keys", "limit", "Billing CI", "2000")  # room again: nothing to warn of
    assert again.exit_code == 0 and "no new value" not in again.output


@pytest.mark.parametrize(
    "given", ["10", "999", "0", "100000001", "5k", "5000.5", "1_000", "1,000", "", " ", "\u2464\u24ea\u24ea\u24ea", "x"]
)
def test_a_vault_limit_outside_its_range_is_refused_before_anything_is_sent(admin, given):
    done = run_cli("server", "keys", "limit", "Billing CI", given)
    assert done.exit_code == 2, done.output
    assert "1000" in done.output and "100000000" in done.output and "default" in done.output  # the range, in words
    assert not admin.calls and admin.limit is None


def test_a_vault_limit_the_server_refuses_is_said_in_the_servers_words(admin, monkeypatch):
    said = "vault_limit is a whole number from 2000 to 100000000 values, or null for the server's limit"
    original = admin.__call__

    def refused(method, path, body=None, **kwargs):
        if method == "PATCH":
            raise ServerError(400, said)
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", refused)
    done = run_cli("server", "keys", "limit", "Billing CI", "1000")
    assert done.exit_code == 1 and said in done.output


def test_a_vault_limit_the_server_does_not_keep_is_not_a_success(admin, monkeypatch):
    original = admin.__call__

    def other(method, path, body=None, **kwargs):
        answer = original(method, path, body, **kwargs)
        if method == "PATCH":
            answer["identity"]["vault_limit"] = 7000  # not what was asked
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", other)
    done = run_cli("server", "keys", "limit", "Billing CI", "5000")
    assert done.exit_code == 1 and "5000" in done.output and "keys list" in done.output
    assert "within seconds" not in done.output


def test_the_key_list_says_the_limit_of_a_key_that_has_one(admin, monkeypatch):
    plain = run_cli("server", "keys", "list")
    assert plain.exit_code == 0 and "vault limit" not in plain.output
    original = admin.__call__

    def limited(method, path, body=None, **kwargs):
        answer = original(method, path, body, **kwargs)
        if method == "GET" and answer.get("identities"):
            answer["identities"] = [{**i, "vault_limit": 5000} for i in answer["identities"]]
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", limited)
    done = run_cli("server", "keys", "list")
    assert done.exit_code == 0 and "u_billingci2  Billing CI  enabled  last used never  vault limit 5000" in done.output


def test_the_vault_limits_here_are_the_servers():
    from latet.cli import server
    from latet.server import identities

    assert server.VAULT_LIMITS == (identities.VAULT_LIMIT_LEAST, identities.VAULT_LIMIT_MOST)


def test_the_shared_settings_change_with_the_same_keys_as_latet_config(admin):
    assert run_cli("server", "config", "set", "history", "metadata").exit_code == 0
    assert admin.shared["history"] == "metadata"
    assert run_cli("server", "config", "set", "ocr", "off").exit_code == 0  # a bare off is the word, not false
    assert admin.shared["ocr"] == "off"
    shown = run_cli("server", "config", "get", "history", "--json")
    assert json.loads(shown.stdout) == "metadata"
    assert run_cli("server", "config", "set", "retention_days", "nonsense").exit_code == 2  # checked before it is sent


def test_unset_puts_a_key_back_to_its_default_though_the_server_keeps_what_a_write_leaves_out(admin):
    """The server keeps every key a write leaves out: `--unset` sends the key's default, on the shared settings and on
    an API key's, a key inside another too."""
    from latet.config import Config

    default = Config()
    assert run_cli("server", "config", "set", "retention_days", "3").exit_code == 0
    assert run_cli("server", "config", "set", "retention_days", "--unset").exit_code == 0
    assert admin.shared["retention_days"] == default.retention_days
    assert run_cli("server", "config", "--key", "Billing CI", "set", "profile", "max").exit_code == 0
    assert run_cli("server", "config", "--key", "Billing CI", "set", "profile", "--unset").exit_code == 0
    assert admin.own["profile"] == default.profile.value
    assert run_cli("server", "config", "set", "llm_review.model", "qwen").exit_code == 0
    assert run_cli("server", "config", "set", "llm_review.model", "--unset").exit_code == 0
    assert admin.shared["llm_review"].get("model", default.llm_review.model) == default.llm_review.model


def test_rules_for_everyone_are_refused_before_anything_is_sent_and_an_api_keys_are_its_own(admin, tmp_path):
    """Rules for everyone are the Enterprise Edition's: a command that would set, add, remove, print or check one
    among the shared settings says so, with the ways to share rules by hand, and asks the server nothing; the same
    commands with --key change an API key's own rules."""
    rule = tmp_path / "badge.yaml"
    rule.write_text("name: badge\ntype: BADGE\npattern: 'AB-[0-9]{6}'\n")
    rules_file = tmp_path / "team.yaml"
    rules_file.write_text("terms:\n- value: Project Falcon\n")
    for args in (
        ("config", "add", "terms", "{value: Project Falcon}"),
        ("config", "set", "profile", "max"),
        ("config", "set", "live_review.enabled", "true"),
        ("config", "remove", "allow_values", "support@acme.com"),
        ("config", "get", "terms"),
        ("rules", "add", str(rule)),
        ("rules", "list"),
        ("rules", "remove", "badge"),
        ("rules", "import", str(rules_file)),
    ):
        done = run_cli("server", *args)
        assert done.exit_code == 2, (args, done.output)
        assert "Enterprise Edition" in done.output and "latet rules export" in done.output, args
    assert admin.calls == []
    assert run_cli("server", "rules", "add", "--help").exit_code == 0
    assert run_cli("server", "config", "--key", "Billing CI", "add", "terms", "{value: Project Falcon}").exit_code == 0
    assert [t["value"] for t in admin.own["terms"]] == ["Project Falcon"]


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (("config", "set", "histroy", "off"), "not a setting the server shares"),
        (("config", "get", "retention"), "not a setting the server shares"),
        (("config", "get", "identity"), "each identity's own"),
        (("config", "set", "projects.acme", "x"), "each identity's own"),
    ],
)
def test_a_key_that_is_no_rule_is_never_said_to_be_the_enterprise_editions(admin, args, said):
    done = run_cli("server", *args)
    assert done.exit_code == 2, done.output
    assert said in done.output and "Enterprise" not in done.output and "retention_days" in done.output
    assert admin.calls == []


def test_a_bare_no_added_to_an_api_keys_list_is_the_word(admin):
    done = run_cli("server", "config", "--key", "Billing CI", "add", "allow_values", "no")
    assert done.exit_code == 0, done.output
    assert "no" in admin.own["allow_values"] and False not in admin.own["allow_values"]


def test_rules_export_without_a_key_and_nothing_kept_is_an_api_keys_command(admin, tmp_path):
    """On a server that keeps no rules from an earlier version (every server a person installs), `rules export` without
    --key says it exports an API key's rules, as the other rules commands do; `rules forget` is not in the help."""
    done = run_cli("server", "rules", "export", str(tmp_path / "none.yaml"))
    assert done.exit_code == 2, done.output
    assert "--key" in done.output and "Enterprise Edition" in done.output
    assert not (tmp_path / "none.yaml").exists()
    shown = run_cli("server", "rules", "--help")
    assert shown.exit_code == 0 and "forget" not in shown.output and "earlier version" not in shown.output


def test_a_kept_rule_a_rules_file_cannot_carry_is_named_at_export_and_at_forget(admin, monkeypatch, tmp_path):
    """The Live Reviewer and auto mode's review, kept from an earlier version, are not in a rules file: the export says
    so by their names, and so does the question that deletes them."""
    from latet.config import TermConfig

    admin.kept = {
        "terms": [TermConfig(value="Project Falcon").model_dump(mode="json")],
        "live_review": {"enabled": True},
        "auto_mode_review": {"user_name": "allow"},
    }
    out = tmp_path / "kept.yaml"
    exported = run_cli("server", "rules", "export", str(out))
    assert exported.exit_code == 0, exported.output
    assert "Project Falcon" in out.read_text("utf-8") and "live_review" not in out.read_text("utf-8")
    assert "live_review" in exported.stderr and "auto_mode_review" in exported.stderr
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda *args, **kw: True)
    monkeypatch.setattr("typer.confirm", lambda prompt, **kw: questions.append(prompt) or False)
    assert run_cli("server", "rules", "forget").exit_code == 1
    assert "live_review" in questions[0] and "auto_mode_review" in questions[0] and "rules file" in questions[0]


def test_rules_kept_from_an_earlier_version_are_counted_exported_and_forgotten_when_asked(admin, tmp_path):
    from latet.config import TermConfig

    admin.kept = {"terms": [TermConfig(value="Project Falcon").model_dump(mode="json")], "profile": "max"}
    shown = run_cli("server", "config", "get")
    assert shown.exit_code == 0 and "history" in shown.stdout and "Project Falcon" not in shown.stdout
    assert "1 term" in shown.stderr and "profile" in shown.stderr and "Project Falcon" not in shown.stderr
    out = tmp_path / "kept.yaml"
    assert run_cli("server", "rules", "export", str(out)).exit_code == 0
    assert "Project Falcon" in out.read_text("utf-8") and "max" in out.read_text("utf-8")
    assert run_cli("server", "rules", "forget").exit_code == 2  # no terminal of yours here: asked first
    assert admin.kept
    assert run_cli("server", "rules", "forget", "--yes").exit_code == 0
    assert admin.kept == {} and admin.calls[-1][2]["forget_kept_rules"] is True
    assert run_cli("server", "rules", "export", str(tmp_path / "none.yaml")).exit_code == 2  # nothing kept: --key


@pytest.mark.parametrize(
    ("key", "given"),
    [
        ("terms", "{value: Project Falcon, type: PROJECT}"),  # without the defaults the server lists it with
        ("terms", "{value: Project Falcon, type: PROJECT, whole_word: true}"),
        ("allow_values", "support@acme.com"),
    ],
)
def test_an_item_an_api_keys_list_has_already_is_said_to_be_there_and_nothing_is_written(admin, key, given):
    """The server lists an API key's term with its defaults: the same term given without them is the same item, there
    already, never added a second time (known item 1)."""
    from latet.config import TermConfig

    admin.own["terms"] = [TermConfig(value="Project Falcon", type="PROJECT").model_dump(mode="json")]
    admin.own["allow_values"] = ["support@acme.com"]
    done = run_cli("server", "config", "--key", "Billing CI", "add", key, given)
    assert done.exit_code == 0, done.output
    assert f"{key}: that item is there already, so nothing changed." in done.output
    assert all(method == "GET" for method, _, _ in admin.calls)
    assert len(admin.own["terms"]) == 1 and admin.own["allow_values"] == ["support@acme.com"]


def test_a_term_that_differs_from_a_listed_one_is_added(admin):
    from latet.config import TermConfig

    admin.own["terms"] = [TermConfig(value="Project Falcon", type="PROJECT").model_dump(mode="json")]
    added = run_cli("server", "config", "--key", "Billing CI", "add", "terms", "{value: Project Falcon, type: CLIENT}")
    assert added.exit_code == 0, added.output
    assert [t["type"] for t in admin.own["terms"]] == ["PROJECT", "CLIENT"]


def test_a_change_made_meanwhile_by_another_administrator_is_said_and_nothing_lost(admin, monkeypatch):
    original = admin.__call__

    def meanwhile(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        if (method, path) == ("PUT", "shared"):
            admin.etag = "changed-by-someone-else"
        return original(method, path, body)

    monkeypatch.setattr("latet.cli.server.admin_call", meanwhile)
    done = run_cli("server", "config", "add", "upstreams", "{name: llm-acme-com, url: 'https://llm.acme.com'}")
    assert done.exit_code == 1 and admin.shared["upstreams"] == []
    assert "The server refused: these settings changed since you read them" in done.output


def test_an_api_keys_own_settings_are_changed_with_key(admin, tmp_path):
    done = run_cli("server", "config", "--key", "Billing CI", "set", "profile", "max")
    assert done.exit_code == 0, done.output
    assert admin.calls[-1][:2] == ("PUT", "identities/u_billingci2/settings") and admin.own["profile"] == "max"
    assert "Billing CI's settings (u_billingci2) changed" in done.output and "profile" not in admin.shared
    rule = tmp_path / "invoice.yaml"
    rule.write_text("name: invoice\ntype: INVOICE\npattern: 'INV-[0-9]{6}'\n")
    assert run_cli("server", "rules", "--key", "u_billingci2", "add", str(rule)).exit_code == 0
    assert [r["name"] for r in admin.own["recognizers"]] == ["invoice"] and "recognizers" not in admin.shared
    assert run_cli("server", "config", "--key", "Nobody", "get").exit_code == 1


def test_an_api_keys_recognizers_are_checked_here_then_added(admin, tmp_path):
    good = tmp_path / "badge.yaml"
    good.write_text("name: badge\ntype: BADGE\npattern: 'AB-[0-9]{6}'\n")
    assert run_cli("server", "rules", "--key", "Billing CI", "add", str(good)).exit_code == 0
    assert [r["name"] for r in admin.own["recognizers"]] == ["badge"]
    written = len(admin.calls)
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: broken\ntype: BROKEN\npattern: '(['\n")
    assert run_cli("server", "rules", "--key", "Billing CI", "add", str(broken)).exit_code == 2
    assert [r["name"] for r in admin.own["recognizers"]] == ["badge"]
    assert admin.calls[written:] == []  # checked here: the server was asked nothing


def test_the_administration_log_is_read_with_its_filters(admin):
    done = run_cli("server", "log", "--since", "7d", "--action", "person.added")
    assert done.exit_code == 0 and "person.added" in done.output and "u_emilycarte" in done.output
    method, path, _ = admin.calls[-1]
    assert method == "GET" and path.startswith("log?") and "action=person.added" in path and "since=" in path


@pytest.mark.parametrize(
    "actor",
    ["../shared", "u_emilycarte&token=t_aaaaaaaaaa", "admin:../shared", "latet_adm_" + "r" * 43],
)
def test_log_actor_is_checked_before_it_reaches_a_query(admin, actor):
    done = run_cli("server", "log", "--actor", actor)
    assert done.exit_code == 2, done.output
    assert not admin.calls
    assert actor not in done.output


@pytest.mark.parametrize("actor", ["u_emilycarte", "admin:a_aaaaaaaaaa"])
def test_log_actor_accepts_checked_identity_and_administration_ids(admin, actor):
    from urllib.parse import parse_qs, urlsplit

    done = run_cli("server", "log", "--actor", actor)
    assert done.exit_code == 0, done.output
    assert parse_qs(urlsplit(admin.calls[-1][1]).query)["actor"] == [actor]


OUTLIVES = (
    "an administration token issues tokens that end no later than itself, and the one this call is made with ends on "
    "2026-11-01 09:00 UTC"
)  # the server's own words (``latet.server.admin``)


def test_refused_lasting_token_names_the_recovery_command(admin, monkeypatch):
    def refused(method, path, body=None, **kwargs):
        assert (method, path) == ("POST", "admin-tokens")
        raise ServerError(403, OUTLIVES)

    monkeypatch.setattr("latet.cli.server.admin_call", refused)
    done = run_cli("server", "admin-token", "issue", "--never", "--label", "offline backup")
    assert done.exit_code == 1
    assert f"The server refused: {OUTLIVES}" in done.output
    assert "latet server init --new-admin-token --never" in done.output
    assert "server's machine" in done.output


@pytest.mark.parametrize("said", ["administration calls are made over HTTPS", "browser requests are not accepted here"])
def test_another_refusal_of_a_new_token_gives_no_hint_about_lasting_tokens(admin, monkeypatch, said):
    """The administration answers 403 for a call made in clear, or from a browser, too: the hint about a token that
    never expires follows the server's words about outliving the issuer alone."""

    def refused(method, path, body=None, **kwargs):
        raise ServerError(403, said)

    monkeypatch.setattr("latet.cli.server.admin_call", refused)
    done = run_cli("server", "admin-token", "issue", "--never", "--label", "offline backup")
    assert done.exit_code == 1 and f"The server refused: {said}" in done.output
    assert "--new-admin-token" not in done.output and "never expires" not in done.output


@pytest.mark.parametrize("kind", ["people", "keys"])
def test_disable_help_says_propagation_takes_seconds(admin, kind):
    done = run_cli("server", kind, "disable", "--help")
    assert done.exit_code == 0
    words = " ".join(done.output.split())
    assert "within seconds" in words and "at once" not in words


def test_an_administration_token_is_issued_listed_and_revoked(admin, monkeypatch):
    issued = run_cli("server", "admin-token", "issue", "--label", "Daniel's laptop")
    assert issued.exit_code == 0 and "latet_adm_" in issued.output and "a_aaaaaaaaba" in issued.output
    assert "It expires on" in issued.output and admin.calls[-1] == (
        "POST",
        "admin-tokens",
        {"label": "Daniel's laptop"},
    )
    forever = run_cli("server", "admin-token", "issue", "--label", "the backup", "--never")
    assert forever.exit_code == 0 and "never expires" in forever.output
    assert admin.calls[-1] == ("POST", "admin-tokens", {"label": "the backup", "never": True})
    assert run_cli("server", "admin-token", "issue", "--days", "30", "--never").exit_code == 2  # one or the other
    listed = run_cli("server", "admin-token", "list")
    assert (
        listed.exit_code == 0
        and "a_aaaaaaaaaa" in listed.output
        and "the backup" in listed.output
        and "never" in listed.output
    )
    assert "latet_adm_" not in listed.output  # never a secret
    assert run_cli("server", "admin-token", "revoke", "a_aaaaaaaaba").exit_code == 0

    def unknown(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        raise ServerError(404, "no administration token has this id")

    monkeypatch.setattr("latet.cli.server.admin_call", unknown)
    gone = run_cli("server", "admin-token", "revoke", "a_aaaaaaaabb")
    assert gone.exit_code == 1 and "The server refused: no administration token has this id." in gone.output


def test_without_an_administration_token_the_command_says_where_it_goes(computer, monkeypatch):
    monkeypatch.setenv("LATET_SERVER_URL", URL)
    done = run_cli("server", "people", "list")
    assert done.exit_code == 2 and "LATET_SERVER_ADMIN_TOKEN" in done.output


def test_a_refused_token_is_said_plainly(admin, monkeypatch):
    def refused(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        raise ServerError(401, "this administration token is not accepted")

    monkeypatch.setattr("latet.cli.server.admin_call", refused)
    done = run_cli("server", "status")
    assert done.exit_code == 1 and "does not accept this administration token" in done.output


def test_the_logs_chain_is_verified(admin, monkeypatch):
    done = run_cli("server", "log", "verify")
    assert done.exit_code == 0 and "12 entries" in done.output and admin.calls[-1][:2] == ("GET", "log/verify")

    def broken(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        return {"ok": False, "checked": 7, "broken_at": 8}

    monkeypatch.setattr("latet.cli.server.admin_call", broken)
    failed = run_cli("server", "log", "verify")
    assert failed.exit_code == 1 and "broken at entry 8" in failed.output


def test_status_says_the_vaults_near_their_limit_and_the_logs_chain(admin):
    done = run_cli("server", "status")
    assert done.exit_code == 0
    assert "vault: 1 identity near its limit (u_billingci2: 181000 of 200000)" in done.output
    assert "the server's limit is 200000 values" in done.output
    assert "administration log: chain ok" in done.output


def test_status_says_each_identitys_own_limit_and_reads_a_server_that_does_not(admin, monkeypatch):
    original = admin.__call__

    def with_limits(method, path, body=None, **kwargs):
        answer = original(method, path, body, **kwargs)
        if (method, path) == ("GET", "status"):
            answer["vault"]["near"] = [
                {"id": "u_billingci2", "values": 950, "retired": 0, "limit": 1000},
                {"id": "k_logsmasker", "values": 100, "retired": 850, "limit": 1000},  # masks and forgets in a loop
                {"id": "u_aaaaaaaaaa", "values": 190000},  # an earlier server's: no limit said
            ]
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", with_limits)
    done = run_cli("server", "status")
    assert done.exit_code == 0
    assert (
        "vault: 3 identities near their limit (u_billingci2: 950 of 1000, k_logsmasker: 100 (and 850 retired fakes) "
        "of 1000, u_aaaaaaaaaa: 190000)"
    ) in done.output


def test_status_prints_the_editions_lines(admin, monkeypatch):
    original = admin.__call__

    def with_edition(method, path, body=None, **kwargs):
        path = _path(path, kwargs)
        answer = original(method, path, body)
        if (method, path) == ("GET", "status"):
            answer["edition"] = [{"status": "failed", "title": "License", "detail": "Acme S.p.A., 300 users"}]
        return answer

    monkeypatch.setattr("latet.cli.server.admin_call", with_edition)
    done = run_cli("server", "status")
    assert done.exit_code == 0 and "License: Acme S.p.A., 300 users" in done.output and "edition" not in done.output
    assert "failed" in done.output


def test_the_administration_is_asked_over_https_with_the_certificate_pinned(latet_server, monkeypatch):
    from latet.cli import server as cli_server

    from .fake_latet_server import ADMIN_TOKEN

    monkeypatch.setattr(cli_server, "_target", {"server": None, "fingerprint": None, "token_file": None})
    monkeypatch.setenv("LATET_SERVER_URL", latet_server.url)
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", latet_server.fingerprint)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", ADMIN_TOKEN)
    assert cli_server.admin_call("GET", "status")["url"] == latet_server.url
    assert cli_server.admin_call("GET", "/_latet/server/v1/admin/status")["version"] == "test"  # the whole path too
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", "latet_adm_" + "w" * 43)
    with pytest.raises(ServerError) as refused:
        cli_server.admin_call("GET", "status")
    assert refused.value.status == 401


@pytest.mark.parametrize("default_days", [7, 365])
def test_expiry_contract_does_not_guess_a_ninety_day_default(admin, monkeypatch, default_days):
    from latet.cli import server

    now = 1_793_000_000.0
    expires = now + default_days * 86400
    original = admin.__call__

    def answer(method, path, body=None, **kwargs):
        found = original(method, path, body, **kwargs)
        assert (method, path, body) == ("POST", "admin-tokens", {"label": "administration"})
        return {**found, "expires": expires}

    monkeypatch.setattr(server.time, "time", lambda: now)
    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "admin-token", "issue")
    assert done.exit_code == 0, done.output
    assert f"It expires on {server._seen(expires)}." in done.output
    assert "cannot outlast its issuer" in done.output
    assert "may have cut" not in done.output
    assert len(admin.calls) == 1  # no extra privileged read to discover the server's default


def test_expiry_contract_a_never_token_still_explains_the_issuer_limit(admin):
    done = run_cli("server", "admin-token", "issue", "--never", "--label", "offline backup")
    assert done.exit_code == 0, done.output
    assert "It never expires." in done.output
    assert "cannot outlast its issuer" in done.output


@pytest.mark.parametrize("returned_days", [7, 30])
def test_expiry_contract_only_an_explicit_request_supports_a_shorter_lifetime_hint(admin, monkeypatch, returned_days):
    from latet.cli import server

    now = 1_793_000_000.0
    original = admin.__call__

    def answer(method, path, body=None, **kwargs):
        found = original(method, path, body, **kwargs)
        assert body == {"label": "administration", "days": 30}
        return {**found, "expires": now + returned_days * 86400}

    monkeypatch.setattr(server.time, "time", lambda: now)
    monkeypatch.setattr(server, "admin_call", answer)
    done = run_cli("server", "admin-token", "issue", "--days", "30")
    assert done.exit_code == 0, done.output
    assert "cannot outlast its issuer" in done.output
    assert ("may have cut" in done.output) == (returned_days < 30)
    assert len(admin.calls) == 1


def _transport_admin_target(latet_server, monkeypatch):
    from latet.cli import server

    from .fake_latet_server import ADMIN_TOKEN

    monkeypatch.setattr(server, "_target", {"server": None, "fingerprint": None, "token_file": None})
    monkeypatch.setenv("LATET_SERVER_URL", latet_server.url)
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", latet_server.fingerprint)
    monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", ADMIN_TOKEN)
    return server


def test_admin_transport_contract_reads_ten_thousand_largest_normal_settings_entries(latet_server, monkeypatch):
    """Real TLS and E0b serialization; no API stub and no large CLI output captured."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from latet.server.admin import LOG_MOST, _log_answer
    from latet.server.audit import AuditEntry
    from latet.server.client import ANSWER_BYTES, ANSWER_MOST

    from .fake_latet_server import ADMIN_TOKEN, API, REFUSED

    server = _transport_admin_target(latet_server, monkeypatch)
    # changed_keys returns Config's top-level keys; this includes every possible key plus the
    # failure metadata of an applied change whose approval could not be kept.
    details = {"keys": list(Config.model_fields), "approved": False, "status": 500}
    response = _log_answer(
        [
            AuditEntry(
                id=number,
                ts=1_793_000_000.123456,
                actor="admin:a_aaaaaaaaaa",
                action="settings.changed",
                target="u_billingci2",
                details=details,
                address="ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255",
                prev="ab" * 32,
                hash="cd" * 32,
            )
            for number in range(LOG_MOST, 0, -1)
        ]
    )
    assert ANSWER_BYTES < len(response.body) <= ANSWER_MOST
    calls = []

    async def page(request):
        calls.append(dict(request.query_params))
        if request.headers.get("x-latet-admin") != ADMIN_TOKEN:
            return JSONResponse(REFUSED, status_code=401)
        return response

    latet_server.app.router.routes.append(Route(f"{API}/admin/log", page, methods=["GET"]))
    found = server.admin_call("GET", "log", query={"limit": LOG_MOST})
    entries = found["entries"]
    assert len(entries) == LOG_MOST
    assert entries[0]["id"] == LOG_MOST and entries[-1]["id"] == 1
    assert entries[0]["details"] == details and entries[-1]["hash"] == "cd" * 32
    assert calls == [{"limit": str(LOG_MOST)}]


def test_admin_transport_contract_refuses_an_answer_beyond_the_existing_ceiling(latet_server, monkeypatch):
    from starlette.responses import Response

    from latet.server.client import ANSWER_MOST

    server = _transport_admin_target(latet_server, monkeypatch)
    latet_server.answers["admin/status"] = Response(b" " * (ANSWER_MOST + 1), media_type="application/json")
    with pytest.raises(ServerError, match=f"more than {ANSWER_MOST} bytes") as stopped:
        server.admin_call("GET", "status")
    assert stopped.value.status == 0


@pytest.mark.parametrize("content", [b"[]", b"[" * 10000 + b"]" * 10000, b"not JSON", b""])
def test_admin_transport_contract_rejects_every_non_object_answer(latet_server, monkeypatch, content):
    from starlette.responses import Response

    server = _transport_admin_target(latet_server, monkeypatch)
    latet_server.answers["admin/status"] = Response(content, media_type="application/json")
    with pytest.raises(ServerError, match="JSON object") as stopped:
        server.admin_call("GET", "status")
    assert stopped.value.status == 0


def test_remaining_contract_says_when_the_administration_chain_is_unknown(admin, monkeypatch):
    original = admin.__call__

    def answer(method, path, body=None, **kwargs):
        return {**original(method, path, body, **kwargs), "audit": {"chain": "unknown"}}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    monkeypatch.setattr("latet.engine.reviewer.certificate.fetch", lambda url: (None, None))
    done = run_cli("server", "status")
    assert done.exit_code == 0, done.output
    assert "chain unknown" in done.output and "no check in the last two hours" in done.output


def test_remaining_contract_marks_tampered_person_and_computer_rows(admin, monkeypatch):
    original = admin.__call__

    def answer(method, path, body=None, **kwargs):
        if path == "identities" and kwargs.get("query") == {"kind": "person"}:
            return {"identities": [{**EMILY, "tampered_tokens": 2}]}
        if path == f"identities/{EMILY['id']}/tokens":
            return {"tokens": [{"id": "t_aaaaaaaaaa", "label": "laptop", "last_used": None, "tampered": True}]}
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    listed = run_cli("server", "people", "list")
    assert listed.exit_code == 0 and "2 tampered token rows: they open nothing" in listed.output
    tokens = run_cli("server", "people", "tokens", EMILY["id"])
    assert tokens.exit_code == 0 and "made or changed without the server's key, it opens nothing" in tokens.output


def test_remaining_contract_lists_current_issuer_and_tampered_administration_rows(admin, monkeypatch):
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda *args, **kwargs: {
            "tokens": [
                {"id": "a_aaaaaaaaaa", "label": "backup", "this": True, "issued_by": "a_aaaaaaaaba"},
                {"id": "a_aaaaaaaabb", "label": "planted", "tampered": True},
            ]
        },
    )
    done = run_cli("server", "admin-token", "list")
    assert done.exit_code == 0, done.output
    assert "a_aaaaaaaaaa this" in done.output and "issued by a_aaaaaaaaba" in done.output
    assert "written without the server's key: it opens nothing" in done.output


@pytest.mark.parametrize("head", [{"id": 123, "hash": "ab" * 32}, [123, "ab" * 32]])
def test_remaining_contract_reports_chain_head_and_truncation_limits(admin, monkeypatch, head):
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda *args, **kwargs: {"ok": True, "checked": 120, "broken_at": None, "head": head},
    )
    done = run_cli("server", "log", "verify")
    assert done.exit_code == 0, done.output
    assert "123" in done.output and "ab" * 32 in done.output
    assert "Entries taken off the log's end leave its chain whole" in done.output
    assert "one noted last time" in done.output


def test_remaining_contract_an_unwalkable_chain_is_not_reported_whole(admin, monkeypatch):
    monkeypatch.setattr(
        "latet.cli.server.admin_call", lambda *args, **kwargs: {"ok": False, "checked": 0, "broken_at": None}
    )
    done = run_cli("server", "log", "verify")
    assert done.exit_code == 1 and "chain could not be walked" in done.output


def test_remaining_contract_reset_only_replaces_a_row_that_does_not_open(admin, monkeypatch):
    """Replaced with the default settings the server shares, and no rule for everyone among them."""
    from latet.server.admin import UNREADABLE_SHARED
    from latet.server.shared import SETTING_KEYS

    defaults = Config().model_dump(mode="json")
    calls = []

    def answer(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            raise ServerError(500, UNREADABLE_SHARED)
        return {"settings": body, "etag": "new"}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "reset", "--yes")
    assert done.exit_code == 0, done.output
    assert calls == [("GET", "shared", None), ("PUT", "shared", {key: defaults[key] for key in SETTING_KEYS})]


@pytest.mark.parametrize("status", [403, 500])
def test_review_fix_reset_refuses_unrelated_open_errors(admin, monkeypatch, status):
    calls = []

    def answer(method, path, body=None, **kwargs):
        calls.append(method)
        if method == "GET":
            raise ServerError(status, "permission denied: cannot open the database")
        return {"settings": body, "etag": "new"}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "reset", "--yes")
    assert done.exit_code == 1 and isinstance(done.exception, SystemExit), done.output
    assert calls == ["GET"]


def test_review_fix_reset_refuses_readable_invalid_settings(admin, monkeypatch):
    calls = []

    def answer(method, path, body=None, **kwargs):
        calls.append(method)
        return {"settings": {"terms": "invalid but readable"}, "etag": "e1"}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "reset", "--yes")
    assert done.exit_code == 1 and isinstance(done.exception, SystemExit), done.output
    assert "These settings open" in done.output and calls == ["GET"]


@pytest.mark.parametrize("marker", ["CLAUDE_CODE_UNRECOGNIZED", "CODEX_HOME"])
@pytest.mark.parametrize("diagnostic", ["nonmatching-example", "matched-value", "invalid-pattern"])
def test_review_fix_agent_rules_json_omits_real_value_diagnostics(admin, computer, monkeypatch, marker, diagnostic):
    from latet.engine.build import reserved_types
    from latet.engine.detectors.recognizers import compile_all

    planted = "planted_sensitive_value_97"
    entry: dict[str, object] = {"name": "invoice", "type": "INVOICE", "pattern": "INV-[0-9]{6}"}
    if diagnostic == "nonmatching-example":
        entry["examples"] = {"match": [planted]}
    elif diagnostic == "matched-value":
        entry["pattern"] = planted
        entry["examples"] = {"no_match": [planted]}
    else:
        entry["pattern"] = "(" + planted
    _, statuses = compile_all([entry], reserved_types())
    assert len(statuses) == 1
    assert planted in " ".join(statuses[0].errors + statuses[0].warnings)
    admin.own["recognizers"] = [entry]
    monkeypatch.setenv(marker, str(computer / "home" / ".codex") if marker == "CODEX_HOME" else "1")
    done = run_cli("server", "rules", "--key", "Billing CI", "list", "--json")
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout) == [{"name": statuses[0].name, "state": statuses[0].state}]
    assert planted not in done.output
    assert admin.calls == [("GET", "identities?kind=key", None), ("GET", "identities/u_billingci2/settings", None)]


def test_review_fix_rules_json_retains_safe_active_listing_for_agents(admin, monkeypatch):
    admin.own["recognizers"] = [{"name": "invoice", "type": "INVOICE", "pattern": "INV-[0-9]{6}"}]
    monkeypatch.setenv("CLAUDECODE", "1")
    done = run_cli("server", "rules", "--key", "Billing CI", "list", "--json")
    assert done.exit_code == 0, done.output
    assert [(row["name"], row["state"]) for row in json.loads(done.stdout)] == [("invoice", "active")]
    assert all(method == "GET" for method, _, _ in admin.calls)


@pytest.mark.parametrize("name", [["invoice"], {"value": "invoice"}], ids=["list-name", "object-name"])
def test_review_fix_rules_add_rejects_unhashable_stored_names_without_writing(admin, tmp_path, name):
    source = tmp_path / "invoice.yaml"
    source.write_text("name: invoice\ntype: INVOICE\npattern: 'INV-[0-9]{6}'\n")
    stored = {"name": name, "type": "BADGE", "pattern": "AB-[0-9]{6}"}
    admin.own["recognizers"] = [stored]
    done = run_cli("server", "rules", "--key", "Billing CI", "add", str(source))
    assert done.exit_code == 1 and isinstance(done.exception, SystemExit), done.output
    assert "recognizer" in done.output.casefold() and "name" in done.output.casefold()
    assert admin.own["recognizers"] == [stored]
    assert admin.calls == [("GET", "identities?kind=key", None), ("GET", "identities/u_billingci2/settings", None)]


def test_review_fix_rules_add_updates_valid_names_and_retains_other_rules(admin, tmp_path):
    source = tmp_path / "invoice.yaml"
    source.write_text("name: invoice\ntype: INVOICE\npattern: 'INV-[0-9]{6}'\n")
    badge = {"name": "badge", "type": "BADGE", "pattern": "AB-[0-9]{6}"}
    admin.own["recognizers"] = [badge, {"name": "invoice", "type": "INVOICE", "pattern": "OLD-[0-9]{6}"}]
    done = run_cli("server", "rules", "--key", "Billing CI", "add", str(source))
    assert done.exit_code == 0, done.output
    assert admin.own["recognizers"] == [
        badge,
        {"name": "invoice", "type": "INVOICE", "pattern": "INV-[0-9]{6}"},
    ]
    assert [(method, path) for method, path, _ in admin.calls] == [
        ("GET", "identities?kind=key"),
        ("GET", "identities/u_billingci2/settings"),
        ("PUT", "identities/u_billingci2/settings"),
    ]


@pytest.mark.parametrize("status", [0, 500])
@pytest.mark.parametrize(
    ("command", "recovery"),
    [
        (("admin-token", "issue", "--label", "backup"), "admin-token list"),
        (("admin-token", "revoke", "a_aaaaaaaaba", "--yes"), "admin-token list"),
        (("keys", "add", "Billing CI"), "keys list"),
        (("keys", "disable", "Billing CI"), "keys list"),
        (("keys", "remove", "Billing CI", "--yes"), "keys list"),
        (("config", "--key", "Billing CI", "set", "profile", "max"), "config --key u_billingci2 get"),
        (("people", "tokens", "Emily Carter", "--revoke", "t_aaaaaaaaaa"), "people tokens u_emilycarte"),
        (("keys", "tokens", "Billing CI", "--revoke", "t_aaaaaaaaaa", "--yes"), "keys tokens u_billingci2"),
        (("keys", "add-token", "Billing CI", "logs", "--mask"), "keys tokens u_billingci2"),
        (("keys", "rotate", "Billing CI", "--yes"), "keys tokens u_billingci2"),
        (("keys", "rotate", "Billing CI", "--token", "t_aaaaaaaaaa", "--yes"), "keys tokens u_billingci2"),
        (("config", "set", "history", "metadata"), "config get"),
        (("people", "add", "Daniel Okafor"), "people list"),
    ],
    ids=[
        "token-issue",
        "token-revoke",
        "key-add",
        "key-disable",
        "key-remove",
        "key-settings",
        "person-tokens",
        "key-token-revoke",
        "key-token-add",
        "rotation-control",
        "rotation-of-one",
        "shared-control",
        "person-control",
    ],
)
def test_review_fix_uncertain_writes_name_the_relevant_inventory_once(admin, monkeypatch, status, command, recovery):
    original = admin.__call__
    writes = []

    def answer(method, path, body=None, **kwargs):
        if method != "GET":
            writes.append((method, path, body))
            raise ServerError(status, "the write's outcome is unknown")
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", *command)
    assert done.exit_code == 1 and isinstance(done.exception, SystemExit), done.output
    assert "may have made this change" in done.output
    assert f"Check `latet server {recovery}` before trying again." in done.output
    assert len(writes) == 1


@pytest.mark.parametrize(
    ("status", "message", "details", "words"),
    [
        (400, "these are not settings", ("upstreams.0.url: not an address",), "upstreams.0.url: not an address"),
        (409, "these settings changed since you read them", (), "these settings changed since you read them"),
        (413, "settings are larger than this server accepts", (), "settings are larger than this server accepts"),
    ],
)
def test_remaining_contract_settings_refusals_preserve_message_and_details(
    admin, monkeypatch, status, message, details, words
):
    original = admin.__call__
    writes = []

    def answer(method, path, body=None, **kwargs):
        if method == "PUT":
            writes.append((path, body))
            raise ServerError(status, message, details)
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "set", "history", "metadata")
    assert done.exit_code == 1 and "The server refused:" in done.output and words in done.output
    assert len(writes) == 1


def test_remaining_contract_key_settings_refusal_details_are_escaped(admin, monkeypatch):
    original = admin.__call__
    danger = "llm_review.api_key_env: forbidden" + chr(0x202E)

    def answer(method, path, body=None, **kwargs):
        if method == "PUT":
            raise ServerError(400, "these settings name a file or a variable of the server's", (danger,))
        return original(method, path, body, **kwargs)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "--key", "Billing CI", "set", "profile", "max")
    assert done.exit_code == 1 and "llm_review.api_key_env: forbidden" in done.output
    assert chr(0x202E) not in done.output


@pytest.mark.parametrize("reason", ["unavailable", "uncounted", "ceiling", "busy"])
def test_remaining_contract_explains_paced_log_entries(admin, monkeypatch, reason):
    monkeypatch.setattr(
        "latet.cli.server.admin_call",
        lambda *args, **kwargs: {
            "entries": [{"id": 1, "ts": 1, "action": "calls.refused", "details": {"count": 456, "reason": reason}}]
        },
    )
    done = run_cli("server", "log")
    assert done.exit_code == 0 and f"456 calls refused: {reason}" in done.output


@pytest.mark.parametrize("current", [True, False], ids=["current", "last"])
def test_remaining_contract_revoking_current_or_last_token_requires_confirmation(admin, monkeypatch, current):
    calls = []

    def answer(method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"tokens": [{"id": "a_aaaaaaaaaa", "this": current}]} if method == "GET" else {"revoked": True}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    refused = run_cli("server", "admin-token", "revoke", "a_aaaaaaaaaa")
    assert refused.exit_code == 2 and all(method == "GET" for method, _ in calls)
    questions = []
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda question, **kwargs: questions.append(question) or False)
    still_refused = run_cli("server", "admin-token", "revoke", "a_aaaaaaaaaa", "--yes")
    assert still_refused.exit_code == 1 and all(method == "GET" for method, _ in calls)
    assert URL in questions[0] and "a_aaaaaaaaaa" in questions[0]
    assert "latet server init --new-admin-token" in questions[0]


def test_the_token_files_help_says_the_variable_holds_the_token_itself():
    """LATET_SERVER_ADMIN_TOKEN holds the token, not a file's name, and is taken for LATET_SERVER_URL alone."""
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    group = typer.main.get_command(app).commands["server"]  # type: ignore[attr-defined]
    [option] = [param for param in group.params if param.name == "admin_token_file"]
    assert "(or the token in LATET_SERVER_ADMIN_TOKEN, bound to LATET_SERVER_URL)" in option.help
    assert "default: LATET_SERVER_ADMIN_TOKEN" not in option.help


def test_the_last_token_is_counted_without_the_tampered_rows(admin, monkeypatch):
    """One real token and one row written without the server's key, which opens nothing: revoking the real one leaves
    no token, and asks first as revoking the last one does."""
    calls = []
    tokens = [{"id": "a_aaaaaaaaaa"}, {"id": "a_aaaaaaaaba", "tampered": True}]

    def answer(method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"tokens": tokens} if method == "GET" else {"revoked": True}

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    refused = run_cli("server", "admin-token", "revoke", "a_aaaaaaaaaa")
    assert refused.exit_code == 2 and all(method == "GET" for method, _ in calls)  # asked, and no terminal here
    tokens.append({"id": "a_aaaaaaaabb"})  # another real one: not the last any more
    assert run_cli("server", "admin-token", "revoke", "a_aaaaaaaaaa").exit_code == 0
    assert calls[-1] == ("DELETE", "admin-tokens/a_aaaaaaaaaa")


@pytest.mark.parametrize(
    "relative", [pytest.param("9" * 1000 + "d", id="overflow"), pytest.param("9" * 5000 + "d", id="digits")]
)
def test_remaining_contract_log_relative_times_fail_with_words_instead_of_an_exception(admin, relative):
    done = run_cli("server", "log", "--since", relative)
    assert done.exit_code == 2
    assert isinstance(done.exception, SystemExit)
    assert "7d, 12h, 30m" in done.output and not admin.calls


PLANTED_TERM = {"value": {"planted_sensitive_field_97": "planted_sensitive_value_97"}}  # not a term: never shown


@pytest.mark.parametrize(
    ("command", "own", "kept"),
    [
        (("rules", "--key", "Billing CI", "list"), {"recognizers": 42}, {}),
        (("rules", "--key", "Billing CI", "export"), {"terms": [PLANTED_TERM]}, {}),
        (("rules", "export"), {}, {"terms": [PLANTED_TERM]}),
        (("config", "get"), {}, {"terms": 5}),
        (("rules", "forget", "--yes"), {}, {"terms": 5}),
        (("config", "get"), {}, ["terms"]),
        (("rules", "forget", "--yes"), {}, {"upstreams": []}),
    ],
    ids=[
        "recognizers-not-list",
        "invalid-term-shape",
        "invalid-kept-term-shape",
        "kept-list-not-list-get",
        "kept-list-not-list-forget",
        "kept-not-a-mapping",
        "kept-not-a-rule",
    ],
)
def test_remaining_contract_malformed_settings_fields_are_reported_without_a_traceback(admin, command, own, kept):
    """An API key's settings, or the rules for everyone an earlier version kept among the shared ones, that do not
    read as settings: said, and never shown."""
    admin.own, admin.kept = own, kept
    done = run_cli("server", *command)
    assert not any(method == "PUT" for method, _, _ in admin.calls)
    assert done.exit_code == 1
    assert isinstance(done.exception, SystemExit)
    assert "server" in done.output.casefold() and "Traceback" not in done.output
    assert "planted_sensitive_field_97" not in done.output and "planted_sensitive_value_97" not in done.output


@pytest.mark.parametrize(
    ("status", "message"),
    [(400, "not a settings row"), (500, "another internal error"), (503, "the database is unavailable")],
)
def test_remaining_contract_reset_refuses_every_other_failure(admin, monkeypatch, status, message):
    calls = []

    def answer(method, path, body=None, **kwargs):
        calls.append(method)
        raise ServerError(status, message)

    monkeypatch.setattr("latet.cli.server.admin_call", answer)
    done = run_cli("server", "config", "reset", "--yes")
    assert done.exit_code == 1 and "The server refused:" in done.output
    assert calls == ["GET"]


@pytest.mark.parametrize("operation", ["add", "remove", "import"])
def test_remaining_contract_malformed_rule_mutations_never_write(admin, tmp_path, operation):
    source = tmp_path / "rules.yaml"
    source.write_text(
        "terms:\n  - value: Project Falcon\n"
        if operation == "import"
        else "name: invoice\ntype: INVOICE\npattern: 'INV-[0-9]{6}'\n"
    )
    admin.own = {"terms": [PLANTED_TERM]} if operation == "import" else {"recognizers": 42}
    target = "invoice" if operation == "remove" else str(source)
    done = run_cli("server", "rules", "--key", "Billing CI", operation, target)
    assert done.exit_code == 1 and isinstance(done.exception, SystemExit)
    assert "server" in done.output.casefold()
    assert "planted_sensitive_field_97" not in done.output and "planted_sensitive_value_97" not in done.output
    assert [(method, path) for method, path, _ in admin.calls] == [
        ("GET", "identities?kind=key"),
        ("GET", "identities/u_billingci2/settings"),
    ]


def test_a_forget_waits_for_the_server_as_long_as_a_keys_first_forget_takes(admin, computer):
    """Review 11b's N5: a key's first forget after an upgrade gives its older answers and history their keyed hashes
    first, which can take minutes on a large key: each call of ``keys forget`` waits for the server's answer up to
    ``FORGET_SECONDS``, never the 10 s every other call waits."""
    from latet.cli.server import FORGET_SECONDS

    path = _values_file(computer, VALUES)
    done = run_cli("server", "keys", "forget", "Billing CI", "--file", str(path), "--yes")
    assert done.exit_code == 0, done.output
    assert FORGET_SECONDS >= 120 and admin.forget_waits == [(FORGET_SECONDS, FORGET_SECONDS)] * 3
