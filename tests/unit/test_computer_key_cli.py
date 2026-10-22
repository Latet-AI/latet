"""A computer's key on the computer (R16, X-I2): `latet connect` keeps it in the token's own record (the keyring, or the
private token file), the CLI sends it on the two calls that ask for it alone (a sign-in link, the identity), no agent
is ever given it, `latet disconnect` forgets it with the token, a record without one is said, and `latet dashboard`
on a connected computer is no agent's to run."""

import json

import pytest
from starlette.responses import JSONResponse

from latet import wrappers
from latet.config import data_dir
from latet.doctor import checks
from latet.server import connection as connections
from latet.server.client import ServerClient, ServerError, pin_certificate
from latet.server.connection import Connection, kept_tokens, load_connection, save_connection, save_server_token

from .conftest import connect_to, run_cli
from .fake_latet_server import EMILY

KEY_SHAPE = r"latet_ck_[A-Za-z0-9_-]{43}"


@pytest.fixture
def connected(latet_server, quiet):
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    return latet_server


def _issued(server):
    [(token, key)] = server.keys.items()
    return token, key


def _keyless(server):
    """This computer connected as a Latet made before computer keys left it: a record of its token alone."""
    [kept] = kept_tokens(data_dir())
    connection = load_connection()
    save_server_token(data_dir(), connection, kept.token)
    server.keys.pop(kept.token)
    return kept.token


def test_connect_keeps_the_key_in_the_tokens_own_file(connected):
    token, key = _issued(connected)
    record = json.loads((data_dir() / "server.token").read_text("utf-8"))
    assert record["token"] == token and record["key"] == key
    [kept] = kept_tokens(data_dir())
    assert (kept.token, kept.key) == (token, key)
    assert key not in repr(kept)
    assert connected.keyed == [("PUT", "identity")]  # the identity, sent once connected, with the key


def test_connect_keeps_the_key_in_the_tokens_own_keyring_entry(latet_server, quiet, keyring_on):
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    token, key = _issued(latet_server)
    [(entry, record)] = keyring_on.kept.items()
    assert entry == ("latet-server", "server-token")
    assert json.loads(record)["token"] == token and json.loads(record)["key"] == key
    assert not (data_dir() / "server.token").exists()


def test_the_cli_sends_the_key_on_a_sign_in_link_and_the_identity_alone(connected, monkeypatch, safe_doctor):
    connected.keyed.clear()
    connected.asked.clear()
    assert run_cli("status").exit_code == 0
    assert run_cli("dashboard", "--print-url").exit_code == 0
    assert run_cli("identity", "send").exit_code == 0
    wrappers.destination_for("https://llm.acme.com/v1", "claude")
    assert wrappers.user_name_surrogate() == "usr_4f2a"
    assert connected.keyed == [("POST", "login-code"), ("PUT", "identity")]
    assert ("GET", "whoami") in connected.asked
    assert [call for call in connected.calls if call[1].startswith("_latet/admin/")] == [
        ("POST", "_latet/admin/destinations"),
        ("POST", "_latet/admin/user-name"),
    ]  # made with the token alone
    assert run_cli("disconnect", "--yes").exit_code == 0
    assert ("DELETE", "token") in connected.asked
    assert connected.keyed == [("POST", "login-code"), ("PUT", "identity")]  # the revocation sent none


def test_no_agent_is_given_the_key(connected):
    _, key = _issued(connected)
    env = wrappers.claude_env()
    assert env["LATET_BRIDGE_TOKEN"] in connected.tokens and key not in json.dumps(env)
    assert key not in json.dumps(wrappers.generic_env())


def test_disconnect_forgets_the_key_with_the_token(connected):
    token, key = _issued(connected)
    done = run_cli("disconnect", "--yes")
    assert done.exit_code == 0, done.output
    assert kept_tokens(data_dir()) == [] and not (data_dir() / "server.token").exists()
    assert key not in done.output and token not in connected.tokens


def test_disconnect_keeps_the_connection_while_an_agents_file_holds_the_key(connected):
    from latet.integrations.agents import ClaudeCode

    _, key = _issued(connected)
    settings = ClaudeCode().settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"env": {"COPIED_BY_HAND": key}}), "utf-8")
    done = run_cli("disconnect", "--yes")
    words = " ".join(done.output.split())
    assert done.exit_code == 1 and "still holds this computer's key" in words and key not in done.output
    assert load_connection() is not None  # kept until no copy is left, so that it can be run again


def test_a_file_of_latets_that_holds_a_computer_key_keeps_the_connection(connected):
    from latet.cli import connect as command

    _, key = _issued(connected)
    left: list = []
    assert command._holding(f'{{"copied": "{key}"}}'.encode(), [], [], [], left, data_dir() / "x.json")
    assert left == [(data_dir() / "x.json", "this computer's key")]
    assert command._holds(f"prefix {key} suffix", [])


@pytest.fixture
def safe_doctor(monkeypatch):
    for name in (
        "check_daemon",
        "check_port",
        "check_engine",
        "check_recognizers",
        "check_ner",
        "check_ocr",
        "check_reviewer",
        "check_live_review",
        "check_agents",
        "check_instructions",
    ):
        monkeypatch.setattr(checks, name, lambda: [])


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_status_and_doctor_say_a_connection_without_a_key_connects_again(connected, safe_doctor, command):
    _keyless(connected)
    done = run_cli(command)
    words = " ".join(done.output.split())
    assert "Computer key" in words and "has no computer key" in words
    assert "`latet disconnect`, then `latet connect" in words and connected.url in words
    assert done.exit_code == 0, done.output  # a warning: the agents still work


def test_status_says_nothing_of_a_key_that_is_there(connected, safe_doctor):
    done = run_cli("status")
    assert done.exit_code == 0 and "Computer key" not in done.output


@pytest.mark.parametrize("command", [("dashboard", "--print-url"), ("identity", "send")])
def test_a_record_without_a_key_asks_nothing_and_says_to_connect_again(connected, command):
    _keyless(connected)
    connected.asked.clear()
    done = run_cli(*command)
    words = " ".join(done.output.split())
    assert done.exit_code != 0 and "has no computer key" in words and "`latet disconnect`, then `latet connect" in words
    assert connected.asked == []  # nothing sent: neither the token nor anything else


def test_a_key_the_server_refuses_is_said_in_its_words(connected):
    token, _ = _issued(connected)
    connected.keys[token] = "latet_ck_" + "x" * 43  # another key than this computer's, as the server now holds it
    done = run_cli("dashboard", "--print-url")
    assert done.exit_code == 1 and "computer key" in done.output and "login?code=" not in done.output


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX", "OPENCODE"])
def test_dashboard_on_a_connected_computer_refuses_an_agent(connected, monkeypatch, marker):
    monkeypatch.setenv(marker, "1")
    connected.asked.clear()
    done = run_cli("dashboard", "--print-url")
    assert done.exit_code == 2 and "latet dashboard" in done.output and "terminal of your own" in done.output
    assert connected.asked == [] and "login?code=" not in done.output


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CODEX_SANDBOX", "OPENCODE"])
def test_identity_send_on_a_connected_computer_refuses_an_agent(connected, monkeypatch, marker):
    """It is the CLI's other call with the computer key: an agent could fill the identity's lists with junk."""
    monkeypatch.setenv(marker, "1")
    connected.asked.clear()
    connected.identities.clear()
    done = run_cli("identity", "send")
    words = " ".join(done.output.split())
    assert done.exit_code == 2 and "latet identity send" in words and "terminal of your own" in words
    assert connected.asked == [] and connected.identities == []


def test_dashboard_on_a_computer_not_connected_is_as_before_with_an_agent(computer, quiet, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda **_kwargs: None)

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"url": "http://127.0.0.1:5283/_latet/ui/login?code=abc"}'

    monkeypatch.setattr("latet.service.daemon.local_urlopen", lambda request, timeout: Answer())
    monkeypatch.setattr("latet.storage.keys.load_bridge_token", lambda data: "local-bridge-token")
    done = run_cli("dashboard", "--print-url")
    assert done.exit_code == 0, done.output
    assert "login?code=abc" in done.output


def test_the_client_refuses_a_call_that_asks_for_a_key_it_does_not_have(latet_server):
    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    with ServerClient(latet_server.url, None, pinned) as client:
        enrolled = client.enrol(latet_server.code, "laptop")
    token, key = enrolled["token"], enrolled["computer_key"]
    assert __import__("re").fullmatch(KEY_SHAPE, key) and latet_server.keys == {token: key}
    with ServerClient(latet_server.url, token, pinned) as client:
        with pytest.raises(ServerError, match="no computer key") as refused:
            client.login_code()
        assert refused.value.status == 0
        assert client.whoami()["id"] == EMILY["id"]
    assert latet_server.keyed == []
    with ServerClient(latet_server.url, token, pinned, key=key) as client:
        assert client.login_code().startswith(f"{latet_server.url}/_latet/ui/login?code=")
    assert latet_server.keyed == [("POST", "login-code")]


def test_an_enrolment_answered_without_a_key_is_revoked_again(latet_server):
    pinned = pin_certificate(latet_server.url, latet_server.fingerprint)
    token = "latet_pat_" + "o" * 43
    latet_server.tokens.add(token)
    answer = {"token": token, "identity": EMILY, "url": latet_server.url}  # an earlier server's enrolment
    latet_server.answers["enrol"] = JSONResponse(answer)
    with (
        ServerClient(latet_server.url, None, pinned) as client,
        pytest.raises(ServerError, match="no computer key") as refused,
    ):
        client.enrol(latet_server.code, "laptop")
    assert "upgrade" in refused.value.message and token not in refused.value.message
    assert token not in latet_server.tokens  # revoked again: nothing of it is left on the server


def test_connect_to_a_server_that_gives_no_key_keeps_nothing(latet_server, quiet):
    token = "latet_pat_" + "p" * 43
    latet_server.tokens.add(token)
    latet_server.answers["enrol"] = JSONResponse({"token": token, "identity": EMILY, "url": latet_server.url})
    done = connect_to(latet_server)
    assert done.exit_code == 1 and "no computer key" in " ".join(done.output.split())
    assert load_connection() is None and kept_tokens(data_dir()) == [] and token not in latet_server.tokens


def test_a_record_whose_key_is_not_one_holds_none(computer):
    connection = Connection(url="https://latet.acme.lan", user="u_emilycarte", user_name="Emily Carter")
    save_connection(connection)
    token = "latet_pat_" + "q" * 43
    record = {"url": connection.url, "user": connection.user, "fingerprint": None, "token": token, "key": "nope"}
    (data_dir() / "server.token").write_text(json.dumps(record), "utf-8")
    (data_dir() / "server.token").chmod(0o600)  # as Latet writes it: one others can read is refused on Linux and macOS
    [kept] = kept_tokens(data_dir())
    assert kept.token == token and kept.key is None
    assert connections.load_server_key(data_dir(), connection, token) is None
    with pytest.raises(ValueError, match="computer key"):
        save_server_token(data_dir(), connection, token, "latet_ck_short")
