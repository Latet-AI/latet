"""`latet connect` swaps a one-time code for this computer's token, pins the server's certificate by its fingerprint
and sends this computer's identity; `latet disconnect` revokes the token and forgets the connection. Both are the
person's own: an agent is refused, and each says what it changes and asks at the person's terminal first."""

import errno
import socket
from types import SimpleNamespace

import keyring.core
import pytest
from keyring.backends import fail, null
from starlette.responses import JSONResponse
from typer.main import get_command

from latet.config import data_dir
from latet.engine.reviewer import certificate as certificates
from latet.integrations.base import Record, save_records
from latet.server import connection as connection_module
from latet.server.connection import (
    Connection,
    connection_path,
    kept_tokens,
    load_connection,
    load_server_token,
    save_server_token,
)

from .conftest import LAPTOP, connect_to, run_cli

_connect = connect_to
TOKEN = "latet_pat_" + "t" * 43


def _yours(monkeypatch):
    """You, at a terminal of your own: what a command shows is asked there."""
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda strict=False: True)


def _token():
    """This computer's token for its connection, as it keeps it (None when there is none)."""
    connection = load_connection()
    return None if connection is None else load_server_token(data_dir(), connection)


def _run_connect(server, *args, code=None):
    """`latet connect` to ``server`` as a script runs it (--yes, the code on standard input), with ``args``."""
    given = server.code if code is None else code
    return run_cli("connect", server.url, *args, "--code", "-", "--yes", input=f"{given}\n")


def _disconnect():
    return run_cli("disconnect", "--yes")


def _pinned(server):
    return certificates.load(data_dir(), server.fingerprint)


def _command(name):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return get_command(app).commands[name]  # type: ignore[attr-defined]


# --- connecting -------------------------------------------------------------------------------------------------


def test_connect_pins_the_certificate_keeps_the_token_and_sends_the_identity(latet_server, quiet, monkeypatch):
    _yours(monkeypatch)
    code = latet_server.code
    printed = certificates.readable(latet_server.fingerprint)  # AB:CD:…, as `latet server certificate` prints it
    done = run_cli("connect", latet_server.url, "--fingerprint", printed, input=f"y\n{code}\n")
    assert done.exit_code == 0, done.output
    connection = load_connection()
    assert (connection.url, connection.fingerprint, connection.user, connection.user_name) == (
        latet_server.url, latet_server.fingerprint, "u_emilycarte", "Emily Carter",
    )  # fmt: skip
    assert load_server_token(data_dir(), connection) in latet_server.tokens
    sent = latet_server.identities[0]
    assert (sent["usernames"], sent["hostname"], sent["emails"]) == (
        ["emily"],
        "emily-laptop",
        ["emily.carter@acme.com"],
    )
    assert "Connected to" in done.output and "latet dashboard" in done.output
    assert code not in done.output  # asked for at the terminal, hidden
    assert latet_server.labels == [socket.gethostname()]  # the computer's label: its host name


def test_a_script_gives_the_code_on_standard_input(latet_server, quiet):
    done = _connect(latet_server)
    assert done.exit_code == 0, done.output
    assert _token() in latet_server.tokens and latet_server.code is None


def test_a_code_written_on_the_command_line_is_still_taken_and_its_help_says_who_can_read_it(latet_server, quiet):
    given = ("--fingerprint", latet_server.fingerprint, "--code", latet_server.code, "--yes")
    done = run_cli("connect", latet_server.url, *given)
    assert done.exit_code == 0, done.output
    helps = {param.name: param.help or "" for param in _command("connect").params}
    assert "other users of this computer can read" in helps["code"]


def test_a_fingerprint_that_does_not_match_saves_nothing(latet_server, quiet):
    done = _run_connect(latet_server, "--fingerprint", "sha256:" + "0" * 64)
    assert done.exit_code == 1 and "another certificate" in done.output
    assert load_connection() is None and kept_tokens(data_dir()) == [] and latet_server.code is not None


def test_a_self_signed_server_without_its_fingerprint_says_how(latet_server, quiet):
    done = _run_connect(latet_server)
    assert done.exit_code == 1 and "--fingerprint" in done.output and load_connection() is None
    assert latet_server.code is not None  # nothing was sent


def test_a_code_that_does_not_work_saves_nothing_and_says_why(latet_server, quiet):
    done = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint, code="used-already")
    assert done.exit_code == 1 and "used or late" in done.output and load_connection() is None
    assert kept_tokens(data_dir()) == [] and _pinned(latet_server) is None  # nor the certificate pinned for it


def test_a_certificate_trusted_before_stays_when_a_code_does_not_work(latet_server, quiet):
    certificates.save(data_dir(), latet_server.pem)  # trusted before (`latet llm trust`, say)
    done = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint, code="used-already")
    assert done.exit_code == 1 and _pinned(latet_server) is not None


def test_an_address_that_is_not_https_is_refused(computer, quiet):
    done = run_cli("connect", "http://latet.acme.lan", "--code", "x", "--yes")
    assert done.exit_code == 2 and "https" in done.output and load_connection() is None


def test_a_code_that_is_not_one_is_refused_before_anything_is_sent_and_never_repeated(latet_server, quiet):
    for given in ("two words", "x" * 129, "code;rm"):
        done = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint, code=given)
        assert done.exit_code == 2 and given not in done.output, done.output
        written = run_cli(
            "connect", latet_server.url, "--fingerprint", latet_server.fingerprint, "--code", given, "--yes"
        )
        assert written.exit_code == 2 and given not in written.output, written.output
    assert latet_server.labels == [] and _pinned(latet_server) is None


def test_connecting_twice_asks_to_disconnect_first(latet_server, quiet):
    assert _connect(latet_server).exit_code == 0
    again = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint, code="another")
    assert again.exit_code == 2 and "latet disconnect" in again.output


def test_a_token_still_kept_for_a_server_asks_to_disconnect_first(computer, quiet):
    other = Connection(url="https://latet.other.lan", user="u_emilycarte", user_name="Emily Carter")
    save_server_token(data_dir(), other, TOKEN)  # its connection file gone, its token kept
    done = run_cli("connect", "https://latet.acme.lan", "--code", "-", "--yes", input="a-code\n")
    assert done.exit_code == 2 and "https://latet.other.lan" in done.output and "latet disconnect" in done.output
    assert [kept.token for kept in kept_tokens(data_dir())] == [TOKEN]  # never overwritten unrevoked


def test_a_token_kept_in_the_keyring_asks_to_disconnect_first_while_latet_keyring_is_off(
    computer, quiet, keyring_on, monkeypatch
):
    other = Connection(url="https://latet.other.lan", user="u_emilycarte", user_name="Emily Carter")
    save_server_token(data_dir(), other, TOKEN)  # in the keyring, saved while it was on
    monkeypatch.setenv("LATET_KEYRING", "off")
    done = run_cli("connect", "https://latet.acme.lan", "--code", "-", "--yes", input="a-code\n")
    assert done.exit_code == 2 and "https://latet.other.lan" in done.output and "latet disconnect" in done.output
    assert keyring_on.kept  # never overwritten unrevoked


def test_a_connection_file_that_is_not_one_is_said_with_exit_2_never_a_traceback(computer, quiet):
    connection_path().parent.mkdir(parents=True, exist_ok=True)
    connection_path().write_text("url: [not, an, address]\n", "utf-8")
    done = run_cli("connect", "https://latet.acme.lan", "--code", "-", "--yes", input="a-code\n")
    assert done.exit_code == 2 and "Not connected:" in done.output and "does not hold a connection" in done.output
    assert not isinstance(done.exception, connection_module.NotConnected)


def test_a_token_file_in_a_folder_not_yours_alone_is_said_with_exit_2_never_a_traceback(
    latet_server, quiet, keyring_on, monkeypatch
):
    data_dir().mkdir(parents=True, exist_ok=True)
    token_file = data_dir() / "server.token"
    token_file.write_text("a copy left in a folder others may change", "utf-8")
    before, credentials, code = token_file.read_bytes(), dict(keyring_on.kept), latet_server.code
    monkeypatch.setattr(connection_module, "private_folder_problem", lambda folder: f"{folder} is open to others")

    def unread(data):
        pytest.fail("credential copies were inspected before connection-state admission")

    monkeypatch.setattr(connection_module, "token_copies", unread)
    done = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint)  # a keyring would keep a new token
    assert done.exit_code == 2 and "Not connected:" in done.output and "open to others" in done.output
    assert "Traceback" not in done.output
    assert latet_server.labels == [] and not isinstance(done.exception, connection_module.NotConnected)
    assert latet_server.code == code and keyring_on.kept == credentials and token_file.read_bytes() == before


def test_the_identity_shown_before_the_question_is_escaped(latet_server, quiet, monkeypatch):
    planted = LAPTOP.model_copy(update={"full_name": "Emily" + chr(0x1B) + "[1A" + chr(0x1B) + "[2KServer x"})
    monkeypatch.setattr("latet.config.detect_identity", lambda: planted)  # a git author's name, an agent's to write
    _yours(monkeypatch)
    shown = run_cli("connect", latet_server.url, "--fingerprint", latet_server.fingerprint, input="n\n")
    assert shown.exit_code == 1 and chr(0x1B) not in shown.output
    assert "Emily" + chr(0x5C) + "x1b[1A" in shown.output


def test_a_running_daemon_here_is_stopped_since_nothing_uses_it(latet_server, quiet, monkeypatch):
    stopped = []
    monkeypatch.setattr("latet.service.daemon.status", lambda port=None: SimpleNamespace(running=True))
    monkeypatch.setattr("latet.service.daemon.stop", lambda: stopped.append(True) or True)
    assert _connect(latet_server).exit_code == 0 and stopped == [True]


# --- the person's own: an agent refused, what changes shown and asked at the person's terminal (T4.5, T4.6) -------


def test_at_your_terminal_it_shows_the_server_the_fingerprint_and_each_agent_and_asks(latet_server, quiet, monkeypatch):
    _yours(monkeypatch)
    # An installation Claude Code's helper can be bound to, on Windows (R5, R8): not this computer's own, whatever it is.
    monkeypatch.setattr("latet.claude_bridge.platform_problem", lambda: None)
    monkeypatch.setattr("latet.claude_bridge.installation_problem", lambda: None)
    save_records({"claude": Record("claude", 0.0), "codex": Record("codex", 0.0)})
    declined = run_cli("connect", latet_server.url, "--fingerprint", latet_server.fingerprint, input="n\n")
    assert declined.exit_code == 1 and "Nothing changed" in declined.output
    shown = " ".join(declined.output.split())
    for what in (latet_server.url, latet_server.fingerprint, "Claude Code", "Codex", "daemon", "config.yaml"):
        assert what in shown, what
    assert "will hold this computer's token" in shown  # Claude Code's settings
    assert latet_server.labels == [] and latet_server.code is not None  # nothing was sent: the code is unspent
    assert _pinned(latet_server) is None and load_connection() is None
    yes_too = run_cli("connect", latet_server.url, "--fingerprint", latet_server.fingerprint, "--yes", input="n\n")
    assert yes_too.exit_code == 1 and latet_server.labels == []  # at your terminal it is asked all the same


def test_an_agent_can_neither_connect_this_computer_nor_disconnect_it(latet_server, quiet, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    refused = _connect(latet_server)
    assert refused.exit_code == 2 and "yourself" in refused.output
    assert latet_server.url not in refused.output  # the address an agent was given is not handed on to run
    assert latet_server.labels == [] and latet_server.code is not None and _pinned(latet_server) is None
    assert load_connection() is None
    monkeypatch.delenv("CLAUDECODE")
    assert _connect(latet_server).exit_code == 0
    token = _token()
    monkeypatch.setenv("CLAUDECODE", "1")
    kept = _disconnect()
    assert kept.exit_code == 2 and "yourself" in kept.output
    assert load_connection() is not None and _token() == token and token in latet_server.tokens


def test_without_a_terminal_of_yours_nothing_is_sent_unless_yes(latet_server, quiet):
    given = ("--fingerprint", latet_server.fingerprint, "--code", "-")
    blind = run_cli("connect", latet_server.url, *given, input=f"{latet_server.code}\n")
    assert blind.exit_code == 2 and "--yes" in blind.output
    nothing_to_ask_at = run_cli("connect", latet_server.url, "--fingerprint", latet_server.fingerprint, "--yes")
    assert nothing_to_ask_at.exit_code == 2 and "--code -" in nothing_to_ask_at.output
    assert latet_server.labels == [] and latet_server.code is not None and _pinned(latet_server) is None
    assert load_connection() is None


def test_disconnect_says_what_changes_back_and_asks_at_your_terminal(latet_server, quiet, monkeypatch):
    assert _connect(latet_server).exit_code == 0
    token = _token()
    blind = run_cli("disconnect")  # no terminal of yours, no --yes
    assert blind.exit_code == 2 and "--yes" in blind.output and load_connection() is not None
    _yours(monkeypatch)
    declined = run_cli("disconnect", input="n\n")
    assert declined.exit_code == 1 and load_connection() is not None and token in latet_server.tokens
    shown = " ".join(declined.output.split())
    for what in (latet_server.url, "your rules there", "config.yaml"):
        assert what in shown, what
    done = run_cli("disconnect", input="y\n")
    assert done.exit_code == 0, done.output
    assert load_connection() is None and token not in latet_server.tokens


# --- what the server answers (T4.1, T4.3, E.9) ----------------------------------------------------------------


def test_too_many_codes_tried_says_how_long_to_wait_in_minutes(latet_server, quiet):
    said = {"error": "too many codes tried from this address: wait a few minutes"}
    latet_server.answers["enrol"] = JSONResponse(said, status_code=429, headers={"retry-after": "600"})
    done = _run_connect(latet_server, "--fingerprint", latet_server.fingerprint)
    assert done.exit_code == 1 and "10 minutes" in done.output and load_connection() is None


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (409, "this person's identity holds 32 user names, emails or host names already"),
        (413, "an identity takes 262144 bytes at most"),
    ],
)
def test_an_identity_the_server_refuses_is_said_in_its_own_words(latet_server, quiet, status, said):
    latet_server.answers["identity"] = JSONResponse({"error": said}, status_code=status)
    done = _connect(latet_server)
    assert done.exit_code == 0 and said in done.output and load_connection() is not None


def test_an_identity_being_applied_is_sent_again_once_after_its_retry_after(latet_server, quiet, monkeypatch):
    waited = []
    monkeypatch.setattr("latet.cli.connect._wait", waited.append)
    applying = {"error": "this person's identity is being applied: try again in a moment"}
    latet_server.answers["identity"] = [JSONResponse(applying, status_code=429, headers={"retry-after": "1"})]
    assert _connect(latet_server).exit_code == 0
    assert waited == [1] and len(latet_server.identities) == 1


def test_an_identity_still_being_applied_after_a_second_is_said(latet_server, quiet, monkeypatch):
    waited = []
    monkeypatch.setattr("latet.cli.connect._wait", waited.append)
    applying = {"error": "this person's identity is being applied: try again in a moment"}
    latet_server.answers["identity"] = JSONResponse(applying, status_code=429, headers={"retry-after": "1"})
    done = _connect(latet_server)
    assert done.exit_code == 0 and waited == [1] and "being applied" in done.output
    assert latet_server.identities == [] and load_connection() is not None


def test_an_identity_a_busy_server_did_not_take_says_so_and_to_send_again_later(latet_server, quiet):
    busy = {"error": "the server cannot answer now: try again in a moment"}
    latet_server.answers["identity"] = JSONResponse(busy, status_code=503)
    done = _connect(latet_server)
    [said] = [line for line in done.output.splitlines() if line.startswith("Your identity")]
    assert done.exit_code == 0 and "did not reach the server" in said and "cannot answer now" in said
    assert "latet identity send" in said and load_connection() is not None
    assert "disconnect" not in said and "new code" not in said
    assert "dashboard" not in said  # a server's dashboard shows the identity's names, and cannot edit them


def test_what_the_server_answers_is_printed_with_its_control_characters_escaped(latet_server, quiet):
    token = "latet_pat_" + "e" * 43
    latet_server.tokens.add(token)
    named = {"id": "u_emilycarte", "name": "Emily" + chr(0x1B) + "[2JCarter"}
    key = "latet_ck_" + "e" * 43
    latet_server.keys[token] = key
    latet_server.answers["enrol"] = JSONResponse({"token": token, "computer_key": key, "identity": named})
    escaped = "Emily" + chr(0x5C) + "x1b[2JCarter"
    done = _connect(latet_server)
    assert done.exit_code == 0, done.output
    assert chr(0x1B) not in done.output and escaped in done.output
    gone = _disconnect()  # the name kept with the connection, as the server gave it
    assert gone.exit_code == 0, gone.output
    assert chr(0x1B) not in gone.output and escaped in gone.output


# --- nothing kept of a connection that failed (T4.4) ------------------------------------------------------------


def test_nothing_is_kept_when_the_connection_cannot_be_written_and_its_token_is_revoked(
    latet_server, quiet, monkeypatch
):
    write = connection_module.write_atomic

    def refused(path, *args, **kwargs):
        if path == connection_path():
            raise OSError(errno.EACCES, "Permission denied", str(path))
        return write(path, *args, **kwargs)

    monkeypatch.setattr(connection_module, "write_atomic", refused)
    done = _connect(latet_server)
    assert done.exit_code == 1 and "revoked" in done.output
    assert latet_server.code is None and latet_server.tokens == set()  # the code was spent, the token it gave revoked
    assert load_connection() is None and kept_tokens(data_dir()) == [] and _pinned(latet_server) is None


@pytest.mark.parametrize("keeper", ["off", "null", "fail"], ids=["LATET_KEYRING off", "null backend", "no keyring"])
def test_a_token_only_a_file_could_keep_where_it_cannot_is_refused_before_the_code_is_spent(
    latet_server, quiet, keyring_on, monkeypatch, keeper
):
    if keeper == "off":
        monkeypatch.setenv("LATET_KEYRING", "off")
    else:
        monkeypatch.setattr(keyring.core, "_keyring_backend", null.Keyring() if keeper == "null" else fail.Keyring())
    monkeypatch.setattr(connection_module, "private_folder_problem", lambda folder: f"{folder} is open to others")
    done = _connect(latet_server)
    assert done.exit_code == 2 and "open to others" in done.output
    assert latet_server.code is not None and latet_server.labels == [] and _pinned(latet_server) is None


def test_a_token_a_keyring_failing_at_the_save_sends_to_a_folder_not_yours_alone_is_revoked_at_once(
    latet_server, quiet, keyring_on, monkeypatch
):
    def refused(service, username, password):
        raise RuntimeError("the keychain takes nothing now")

    monkeypatch.setattr(keyring_on, "set_password", refused)  # known only when the token comes

    def opened(folder):  # the folder others may change once the server issued the token: before, it is checked first
        return f"{folder} is open to others" if latet_server.tokens else None

    monkeypatch.setattr(connection_module, "private_folder_problem", opened)
    done = _connect(latet_server)
    assert done.exit_code == 1 and "open to others" in done.output and "revoked it again" in done.output
    assert latet_server.code is None and latet_server.tokens == set()
    assert load_connection() is None and keyring_on.kept == {}


def test_a_copy_of_the_token_that_could_not_be_removed_is_said_where_it_remains(
    latet_server, quiet, keyring_on, monkeypatch
):
    def locked(service, username):
        raise RuntimeError("the keychain is locked")

    write = connection_module.write_atomic

    def refused(path, *args, **kwargs):
        if path == connection_path():
            raise OSError(errno.EACCES, "Permission denied", str(path))
        return write(path, *args, **kwargs)

    monkeypatch.setattr(keyring_on, "delete_password", locked)  # the record stays in the keyring
    monkeypatch.setattr(connection_module, "write_atomic", refused)
    done = _connect(latet_server)
    words = " ".join(done.output.split())
    assert done.exit_code == 1 and "remains in the system's keyring" in words
    assert "nothing of this connection is kept" not in words and latet_server.tokens == set()  # revoked all the same


# --- disconnecting ----------------------------------------------------------------------------------------------


def test_disconnect_revokes_the_token_and_forgets_the_connection(latet_server, quiet):
    _connect(latet_server)
    token = _token()
    done = _disconnect()
    assert done.exit_code == 0 and "Disconnected" in done.output
    assert load_connection() is None and kept_tokens(data_dir()) == [] and token not in latet_server.tokens


def test_disconnect_from_a_server_that_does_not_answer_forgets_it_here_and_says_who_revokes(latet_server, quiet):
    _connect(latet_server)
    latet_server.stop()
    done = _disconnect()
    assert done.exit_code == 0 and "administrator" in done.output and load_connection() is None
    words = " ".join(done.output.split())
    assert "latet server people tokens u_emilycarte --revoke" in words and "--revoke-all" in words
    assert kept_tokens(data_dir()) == []


def test_disconnect_when_not_connected_says_so(computer, quiet):
    done = _disconnect()
    assert done.exit_code == 0 and "not connected" in done.output


def test_disconnect_revokes_a_token_kept_without_its_connection_file(latet_server, quiet):
    _connect(latet_server)
    token = _token()
    connection_path().unlink()  # the connection file gone, its token kept
    done = _disconnect()
    assert done.exit_code == 0, done.output
    assert token not in latet_server.tokens and kept_tokens(data_dir()) == []


def test_disconnect_revokes_and_forgets_a_connection_file_it_cannot_read(latet_server, quiet):
    _connect(latet_server)
    token = _token()
    connection_path().write_text("url: [not, an, address]\n", "utf-8")
    done = _disconnect()
    assert done.exit_code == 0, done.output
    assert token not in latet_server.tokens and kept_tokens(data_dir()) == [] and not connection_path().exists()


def test_disconnect_revokes_with_the_keyrings_copy_when_the_tokens_folder_is_not_yours_alone(
    latet_server, quiet, keyring_on, monkeypatch
):
    _connect(latet_server)  # its token in the keyring
    token = _token()
    assert keyring_on.kept  # in the keyring, not in a file
    (data_dir() / "server.token").write_text("a copy left in a folder others may change", "utf-8")
    monkeypatch.setattr(connection_module, "private_folder_problem", lambda folder: f"{folder} is open to others")
    done = _disconnect()
    assert done.exit_code == 0, done.output
    assert token not in latet_server.tokens and keyring_on.kept == {} and not (data_dir() / "server.token").exists()
    assert not connection_path().exists() and load_connection(unchecked=True) is None


def test_disconnect_revokes_the_keyrings_copy_while_latet_keyring_is_off(latet_server, quiet, keyring_on, monkeypatch):
    assert _connect(latet_server).exit_code == 0  # its token in the keyring
    token = _token()
    monkeypatch.setenv("LATET_KEYRING", "off")  # then turned off, in one shell say
    done = _disconnect()
    assert done.exit_code == 0, done.output
    assert token not in latet_server.tokens and keyring_on.kept == {} and load_connection() is None


def test_disconnect_forgets_the_token_and_the_connection_before_the_agents_are_applied_again(
    latet_server, quiet, monkeypatch
):
    _connect(latet_server)
    token = _token()
    save_records({"opencode": Record("opencode", 0.0, [], {"providers": []})})  # an agent applied for good
    seen = []

    def half_way(self, config=None):  # its own tool fails while it is applied again
        seen.append((load_connection(), kept_tokens(data_dir())))
        raise FileNotFoundError("opencode")

    monkeypatch.setattr("latet.integrations.agents.OpenCode.apply", half_way)
    done = _disconnect()
    words = " ".join(done.output.split())
    assert done.exit_code == 1 and "Disconnected" in words and "latet agents apply opencode" in words
    assert seen == [(None, [])] and token not in latet_server.tokens  # nothing of the server left for it
