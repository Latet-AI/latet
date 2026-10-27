"""Prospective Task 7 public capabilities; absent command/option failures are capability gaps, never security RED.

No speculative production helper imports. These tests use actual CLI registration, connection and ServerClient.
"""

import pytest
from starlette.responses import JSONResponse

from latet.config import data_dir
from latet.server import connection as connections
from latet.server.tokens import route_key

from .conftest import LAPTOP, connect_to, run_cli
from .test_connected_cli import _controlled, _private_output

KEY = "latet_key_" + "k" * 43


@pytest.fixture(name="connected")
def connected_server(latet_server, quiet):
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    latet_server.asked.clear()
    return latet_server


def test_identity_send_uses_existing_detected_schema_without_personal_output(connected):
    connection = connections.load_connection()
    assert connection is not None
    token = connections.load_server_token(data_dir(), connection)
    assert token is not None
    connected.identities.clear()
    done = run_cli("identity", "send")
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert connected.asked == [("PUT", "identity")]
    assert connected.identities == [LAPTOP.model_dump(include={"usernames", "hostname", "full_name", "emails"})]
    assert done.stdout.strip()  # fixed acknowledgement, exact wording belongs to the implementation
    _private_output(done, *LAPTOP.usernames, LAPTOP.hostname, LAPTOP.full_name, *LAPTOP.emails, token, route_key(token))


@pytest.mark.parametrize("status", [401, 403, 409, 413, 429, 503])
def test_identity_send_refusal_is_nonzero_with_retry_guidance(connected, status):
    connected.answers["identity"] = JSONResponse(
        {"error": "controlled identity refusal", "details": ["fixed detail"]},
        status_code=status,
        headers={"retry-after": "0" if status == 503 else "60"},
    )
    done = run_cli("identity", "send")
    _controlled(done)
    assert done.exit_code != 0, done.output
    if status == 401:  # R7: a refused token is not retried; it says what to do
        said = " ".join(done.output.split())
        assert "no longer accepts this computer's token" in said and "`latet disconnect`, then `latet connect" in said
    else:
        assert "controlled identity refusal" in done.output and "fixed detail" in done.output
        assert "latet identity send" in done.output
        assert "disconnect" not in done.output and "connect again" not in done.output
    assert connected.asked == [("PUT", "identity")] * (2 if status == 503 else 1)
    _private_output(done, *LAPTOP.usernames, LAPTOP.hostname, LAPTOP.full_name, *LAPTOP.emails)
    if status == 429:
        assert "60" in done.output


def test_identity_send_without_connection_is_controlled(computer, quiet):
    done = run_cli("identity", "send")
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert "server" in done.output.lower()


def test_identity_send_missing_credential_has_zero_network(connected, monkeypatch):
    monkeypatch.setattr(connections, "load_server_token", lambda data, connection, *, _selected=None: None)
    done = run_cli("identity", "send")
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert "token" in done.output.lower() and connected.asked == []


@pytest.mark.parametrize("shell", ["bash", "powershell", "cmd"])
def test_key_file_gives_only_generic_variables_and_its_own_stderr_warning(
    computer, quiet, tmp_path, monkeypatch, shell
):
    key_file = tmp_path / "synthetic-key.fixture"
    key_file.write_text(KEY, encoding="utf-8")
    monkeypatch.setenv("LATET_SERVER_URL", "https://latet.acme.lan")
    done = run_cli("env", "--key-file", str(key_file), "--shell", shell)
    _controlled(done)
    assert done.exit_code == 0, done.output
    base = f"https://latet.acme.lan/u/{route_key(KEY)}"
    assert f"{base}/openai/v1" in done.stdout and f"{base}/anthropic" in done.stdout
    assert "OPENAI_BASE_URL" in done.stdout and "ANTHROPIC_BASE_URL" in done.stdout
    assert "LATET_BRIDGE_TOKEN" not in done.stdout and "NODE_EXTRA_CA_CERTS" not in done.stdout
    _private_output(done, KEY)
    assert "secret" in done.stderr and "route" in done.stderr.lower()
    assert "secret" not in done.stdout
    expected = {
        "bash": "export OPENAI_BASE_URL=",
        "powershell": "$env:OPENAI_BASE_URL =",
        "cmd": "set OPENAI_BASE_URL=",
    }
    assert expected[shell] in done.stdout


def test_key_file_explicit_server_beats_environment_and_personal_connection(connected, tmp_path, monkeypatch):
    key_file = tmp_path / "synthetic-key.fixture"
    key_file.write_text(KEY, encoding="utf-8")
    monkeypatch.setenv("LATET_SERVER_URL", "https://other.acme.lan")
    done = run_cli("env", "--key-file", str(key_file), "--server", "https://latet.acme.lan", "--shell", "bash")
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert f"https://latet.acme.lan/u/{route_key(KEY)}" in done.stdout
    _private_output(done, KEY, connected.url, "https://other.acme.lan")
    assert connected.asked == []
    # An explicit key/server does not consume an unrelated personal connection, including at the warning stage.
    connections.connection_path().write_text(
        "url: []\nuser: invalid\nuser_name: unrelated-personal-sentinel\n", encoding="utf-8"
    )
    independent = run_cli("env", "--key-file", str(key_file), "--server", "https://latet.acme.lan", "--shell", "bash")
    _controlled(independent)
    assert independent.exit_code == 0, independent.output
    assert f"https://latet.acme.lan/u/{route_key(KEY)}" in independent.stdout
    _private_output(independent, KEY, connected.url, "https://other.acme.lan", "unrelated-personal-sentinel")
    assert "secret" in independent.stderr and connected.asked == []


@pytest.mark.parametrize(("shell", "agent"), [("fish", "generic"), ("bash", "other"), ("bash", "claude")])
def test_key_file_rejects_unsupported_shell_agent_combinations(computer, quiet, tmp_path, shell, agent):
    key_file = tmp_path / "synthetic-key.fixture"
    key_file.write_text(KEY, encoding="utf-8")
    done = run_cli(
        "env",
        "--key-file",
        str(key_file),
        "--server",
        "https://latet.acme.lan",
        "--shell",
        shell,
        "--agent",
        agent,
    )
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert "OPENAI_BASE_URL" not in done.stdout
    _private_output(done, KEY)


@pytest.mark.parametrize("content", ["", "latet_pat_" + "t" * 43, KEY + "\n" + KEY, KEY + "\x1b"])
def test_key_file_validation_never_echoes_bad_content(computer, quiet, tmp_path, content):
    key_file = tmp_path / "synthetic-key.fixture"
    key_file.write_text(content, encoding="utf-8")
    done = run_cli("env", "--key-file", str(key_file), "--server", "https://latet.acme.lan")
    _controlled(done)
    assert done.exit_code != 0, done.output
    _private_output(done, KEY, "latet_pat_" + "t" * 43)
    assert "OPENAI_BASE_URL" not in done.stdout


def _key_env(server, tmp_path, monkeypatch):
    """`latet env --key-file` for a key's token, the server named and its certificate pinned."""
    key_file = tmp_path / "synthetic-key.fixture"
    key_file.write_text(KEY, encoding="utf-8")
    monkeypatch.setenv("LATET_SERVER_FINGERPRINT", server.fingerprint)
    return run_cli("env", "--key-file", str(key_file), "--server", server.url, "--shell", "bash")


def test_key_file_of_a_token_with_no_address_prints_none_and_says_why(
    computer, quiet, latet_server, tmp_path, monkeypatch
):
    """The final review's M9: a token added with `keys add-token`, whatever its permissions, has no address for models
    (the server answers 403 there), so the server is asked who the token is: it gives it no address, and the command
    prints no variable and says why, and which token has them."""
    latet_server.answers["whoami"] = JSONResponse(
        {"id": "k_logsxxxxxx", "name": "Logs", "kind": "key", "url": latet_server.url, "version": "test"}
    )
    done = _key_env(latet_server, tmp_path, monkeypatch)
    _controlled(done)
    assert done.exit_code == 1, done.output
    assert "OPENAI_BASE_URL" not in done.stdout and "ANTHROPIC_BASE_URL" not in done.stdout
    said = " ".join(done.stderr.split())
    assert "no address" in said and "keys add-token" in said and "first token" in said and "applications API" in said
    assert latet_server.asked == [("GET", "whoami")]
    _private_output(done, KEY, route_key(KEY))


def test_key_file_of_a_token_with_addresses_is_asked_and_printed(computer, quiet, latet_server, tmp_path, monkeypatch):
    latet_server.tokens.add(KEY)  # the key's first token: the server gives it its addresses
    done = _key_env(latet_server, tmp_path, monkeypatch)
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert f"{latet_server.url}/u/{route_key(KEY)}/openai/v1" in done.stdout
    assert latet_server.asked == [("GET", "whoami")]
    _private_output(done, KEY)


def test_key_file_of_a_token_the_server_refuses_prints_none(computer, quiet, latet_server, tmp_path, monkeypatch):
    done = _key_env(latet_server, tmp_path, monkeypatch)  # a token the server does not know: 401
    _controlled(done)
    assert done.exit_code == 1, done.output
    assert "OPENAI_BASE_URL" not in done.stdout and "refuses this token" in " ".join(done.stderr.split())
    _private_output(done, KEY, route_key(KEY))
