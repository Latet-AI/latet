"""EC4 exercises real local persistence with synthetic enrolment and injected interruptions."""

import importlib

import pytest

from latet.config import data_dir
from latet.server import client, connection

URL = "https://eop-computer.example.test"
TOKEN = "latet_pat_" + "i" * 43
KEY = "latet_ck_" + "i" * 43


@pytest.mark.parametrize("stage", ["save_server_token", "save_connection"])
@pytest.mark.parametrize("after_write", [False, True], ids=["before-write", "after-write"])
@pytest.mark.parametrize("interruption", ["keyboard", "exit"])
def test_issued_token_interruption_cleans_once_and_preserves_original(
    computer, monkeypatch, capsys, caplog, stage, after_write, interruption
):
    command = importlib.import_module("latet.cli.connect")
    original = KeyboardInterrupt("injected interruption") if interruption == "keyboard" else SystemExit(73)
    writer = "_" + stage
    saved = getattr(connection, writer)
    reached = []
    revocations, removals = [], []

    class Enrolment:
        def __init__(self, url, token, fingerprint):
            assert (url, token, fingerprint) == (URL, None, None)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def enrol(self, code, label):
            assert code == "synthetic-one-time-code"
            return {"token": TOKEN, "computer_key": KEY, "identity": {"id": "u_eopcomputr", "name": "Synthetic Person"}}

    def interrupted(*args, **kwargs):
        reached.append(stage)
        if after_write:
            saved(*args, **kwargs)
        raise original

    def revoke(url, token, fingerprint):
        revocations.append((url, token, fingerprint))
        return None

    remove = connection.remove_server_token

    def remove_once(folder):
        removals.append(folder)
        return remove(folder)

    def forbidden(*args, **kwargs):
        pytest.fail("post-failure identity, daemon or agent side effect")

    monkeypatch.setattr(client, "ServerClient", Enrolment)
    monkeypatch.setattr(connection, writer, interrupted)
    monkeypatch.setattr(connection, "remove_server_token", remove_once)
    monkeypatch.setattr(command, "_revoke", revoke)
    monkeypatch.setattr(command, "_send_identity", forbidden)
    monkeypatch.setattr(command, "_refresh", forbidden)
    monkeypatch.setattr("latet.service.daemon.status", forbidden)
    with pytest.raises(type(original)) as caught:
        command._connected(URL, None, "synthetic-one-time-code")
    assert caught.value is original
    assert reached == [stage]
    assert revocations == [(URL, TOKEN, None)]
    assert removals == [data_dir()]
    assert not (data_dir() / "server.token").exists()
    # A connection written by this failed attempt must be removed, or its retained state reported truthfully.
    output = capsys.readouterr()
    said = output.out + output.err
    if connection.connection_path().exists():
        assert "remain" in said.lower() or "disconnect" in said.lower()
        assert "nothing" not in said.lower()
    assert "Connected to" not in said
    assert TOKEN not in said + caplog.text


def test_local_cleanup_error_cannot_replace_the_original_interruption(computer, monkeypatch, capsys):
    interrupted = KeyboardInterrupt("original interruption")
    cleanup = OSError("planted_cleanup_error_9717")
    saved = connection._save_connection
    reached, cleanups = [], []

    def save_then_interrupt(selected, value):
        reached.append(value)
        saved(selected, value)
        raise interrupted

    def cleanup_fails(folder):
        cleanups.append(folder)
        raise cleanup

    monkeypatch.setattr(connection, "_save_connection", save_then_interrupt)
    monkeypatch.setattr(connection, "remove_server_token", cleanup_fails)
    value = connection.Connection(url=URL, user="u_eopcomputr", user_name="Synthetic Person")
    with pytest.raises(KeyboardInterrupt) as caught:
        connection.keep_connection(value, TOKEN)
    assert caught.value is interrupted
    assert reached == [value]
    assert cleanups == [data_dir()]
    output = capsys.readouterr()
    assert "planted_cleanup_error_9717" not in output.out + output.err
