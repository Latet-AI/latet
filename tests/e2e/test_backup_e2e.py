"""Backup and restore end to end: an installation moved to another machine keeps its fakes, and
nothing of the machine it lands on is lost. The daemon is never the real one: its calls are faked."""

import base64
import json
import logging
import os
import socket
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from latet.config import Config, IdentityConfig, ProjectEntry, TermConfig, load_config, save_config
from latet.engine.types import EntityClass
from latet.engine.vault import Vault
from latet.runtime import REVIEWED_FILE, make_runtime
from latet.service import daemon
from latet.storage import backup as backup_module
from latet.storage.backup import read_backup
from latet.storage.db import Store
from latet.storage.keys import load_root_key

pytestmark = pytest.mark.e2e
PASS = "correct horse battery staple"
EMAIL = "mario.rossi@acme.it"
FREE_PORT = 6799  # never the port of a daemon that may run on this machine


@pytest.fixture
def machines(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_ROOT_KEY", raising=False)
    monkeypatch.delenv("LATET_ROOT_KEY_FILE", raising=False)
    monkeypatch.setenv("LATET_KEYRING", "off")
    monkeypatch.setattr(backup_module, "KDF", {"memory": 8 * 1024, "iterations": 1, "lanes": 1})
    from latet.cli import admin, agent

    monkeypatch.setattr(agent, "agent_name", lambda strict=False: None)
    monkeypatch.setattr(agent, "at_your_terminal", lambda strict=False: True)
    monkeypatch.setattr(admin, "_reload_answer", lambda: None)
    calls: list[str] = []
    monkeypatch.setattr(daemon, "status", lambda port=None: daemon.Status(False, port=FREE_PORT))
    monkeypatch.setattr(daemon, "stop", lambda timeout=10.0: calls.append("stop") or True)
    monkeypatch.setattr(daemon, "start", lambda *a, **k: calls.append("start") or daemon.Status(False))

    def use(name):
        home = tmp_path / name
        monkeypatch.setenv("LATET_DATA_DIR", str(home / "data"))
        monkeypatch.setenv("LATET_CONFIG", str(home / "config.yaml"))
        return home

    return use, calls


def _cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def _vault_on(home, value=EMAIL, **settings):
    save_config(Config(identity=IdentityConfig(enabled=False), port=FREE_PORT, **settings))
    runtime = make_runtime(data=home / "data")
    surrogate = runtime.built.vault.intern("EMAIL", EntityClass.PII, value).surrogate
    runtime.close()
    return surrogate


def _vault_of(home):
    data = home / "data"
    key = load_root_key(data)
    store = Store(data / "latet.db", key)
    try:
        return Vault(key, store.load_entities()), store.unreadable
    finally:
        store._db.close()


def test_the_vault_moved_keeps_its_fakes(machines, tmp_path):
    use, calls = machines
    a = use("a")
    projects = [ProjectEntry(path="C:/Users/alice/dev/shop", allow_values=["shop-dev"])]
    surrogate = _vault_on(a, profile="max", projects=projects)
    file = tmp_path / "a.latet"
    made = _cli("backup", str(file), "--vault", input=f"{PASS}\n")
    assert made.exit_code == 0, made.output
    key = load_root_key(a / "data")
    b = use("b")
    done = _cli("restore", str(file), "--paths", "C:/Users/alice/dev=D:/work", input=f"{PASS}\ny\n")
    assert done.exit_code == 0, done.output
    assert load_root_key(b / "data") == key
    vault, unreadable = _vault_of(b)
    assert unreadable == 0 and vault.by_original(EMAIL).surrogate == surrogate
    config = load_config()
    assert config.profile == "max" and config.port == 5283  # this machine's port, not the backup's
    assert config.identity.hostname == socket.gethostname()  # this machine's identity, added
    assert [e.path for e in config.projects] == ["D:/work/shop"]
    reviewed = json.loads((b / "data" / REVIEWED_FILE).read_text("utf-8"))
    assert reviewed["profile"] == "max"  # confirmed at your terminal: approved
    assert calls == [] and not (b / "data" / "restore.json").exists()
    assert "Not moved" in done.output


def test_this_machine_s_own_vault_is_replaced_only_when_asked_and_kept(machines, tmp_path):
    use, _ = machines
    a = use("a")
    surrogate = _vault_on(a)
    file = tmp_path / "a.latet"
    assert _cli("backup", str(file), "--vault", "--history", input=f"{PASS}\n").exit_code == 0
    b = use("b")
    _vault_on(b, "luigi.verdi@acme.it")
    refused = _cli("restore", str(file), input=f"{PASS}\ny\n")
    assert refused.exit_code == 2 and "--replace" in refused.output
    done = _cli("restore", str(file), "--replace", input=f"{PASS}\ny\n")
    assert done.exit_code == 0, done.output
    vault, unreadable = _vault_of(b)
    assert unreadable == 0 and vault.by_original(EMAIL).surrogate == surrogate
    assert vault.by_original("luigi.verdi@acme.it") is None
    [before] = (b / "data").glob("before-restore-*.latet")
    kept = read_backup(before, PASS, b / "scratch")
    assert kept.contents.values == 2  # the address and its domain
    assert kept.root_key != load_root_key(b / "data")  # B's own key, saved
    kept.discard()
    [aside] = [p for p in (b / "data").glob("before-restore-*") if p.is_dir()]
    assert (aside / "latet.db").exists()


def test_settings_alone_leave_the_key_alone(machines, tmp_path):
    use, _ = machines
    a = use("a")
    save_config(Config(identity=IdentityConfig(enabled=False), profile="max", port=FREE_PORT))
    file = tmp_path / "settings.latet"
    assert _cli("backup", str(file), input=f"{PASS}\n").exit_code == 0
    assert not (a / "data" / "root.key").exists()  # no key was made for a backup without the vault
    b = use("b")
    done = _cli("restore", str(file), input=f"{PASS}\ny\n")
    assert done.exit_code == 0, done.output
    assert load_config().profile == "max" and not (b / "data" / "root.key").exists()
    wrong = _cli("restore", str(file), input="not the passphrase\n")
    assert wrong.exit_code == 1 and "passphrase is wrong" in wrong.output


def _a_backup_of_settings_alone(use, tmp_path):
    """A backup of the settings of machine a (profile max), and the data folder of machine b, which restores it."""
    use("a")
    terms = [TermConfig(value="Mercury Labs", type="CLIENT")]
    save_config(Config(identity=IdentityConfig(enabled=False), profile="max", port=FREE_PORT, terms=terms))
    file = tmp_path / "settings.latet"
    assert _cli("backup", str(file), input=f"{PASS}\n").exit_code == 0
    return file, use("b") / "data"


def test_the_settings_a_restore_approves_are_written_for_you_alone(machines, tmp_path, monkeypatch):
    """config.reviewed.json holds your terms, your identity and the values you let through: a restore writes it as the
    daemon does, for you alone where the system allows it (on Windows a file's access follows its folder's, and the
    write asks for the same)."""
    use, _ = machines
    file, data = _a_backup_of_settings_alone(use, tmp_path)
    asked, chmod = [], os.chmod

    def recording(path, mode, *args, **kwargs):
        asked.append((Path(path).name, mode))
        chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recording)
    done = _cli("restore", str(file), input=f"{PASS}\ny\n")
    assert done.exit_code == 0, done.output
    assert [mode for name, mode in asked if name.startswith(f".{REVIEWED_FILE}")] == [0o600]  # the copy being written
    assert json.loads((data / REVIEWED_FILE).read_text("utf-8"))["profile"] == "max"
    if os.name == "posix":
        assert stat.S_IMODE((data / REVIEWED_FILE).stat().st_mode) == 0o600


def test_a_restore_where_chmod_is_refused_still_approves_the_settings_it_restored(machines, tmp_path, request, caplog):
    """On a mount that shows files as another owner's chmod is refused: the restore goes on, and the copy is written
    with a warning that names it. Without it the restored settings would not count as approved."""
    use, _ = machines
    file, data = _a_backup_of_settings_alone(use, tmp_path)
    request.getfixturevalue("chmod_refused")  # after the backup, whose file is made private too
    done = _cli("restore", str(file), input=f"{PASS}\ny\n")
    assert done.exit_code == 0, done.output
    assert json.loads((data / REVIEWED_FILE).read_text("utf-8"))["profile"] == "max"
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert str(data / REVIEWED_FILE) in warning.getMessage() and "Mercury" not in caplog.text


def test_a_restore_cut_off_keeps_the_daemon_down_until_undone(machines, monkeypatch):
    use, _ = machines
    home = use("c")
    _vault_on(home)
    data = home / "data"
    aside = data / "before-restore-x"
    aside.mkdir()
    os.replace(data / "latet.db", aside / "latet.db")  # as a restore that had begun
    (data / "latet.db").write_bytes(b"half of something")
    (data / "restore.json").write_text(json.dumps({"aside": str(aside), "key": False}), "utf-8")
    with pytest.raises(RuntimeError, match="restore"):
        make_runtime(data=data)
    monkeypatch.undo()  # the real daemon functions, on a free port
    monkeypatch.delenv("LATET_ROOT_KEY", raising=False)
    monkeypatch.setenv("LATET_DATA_DIR", str(data))
    monkeypatch.setenv("LATET_CONFIG", str(home / "config.yaml"))
    monkeypatch.setenv("LATET_KEYRING", "off")
    assert "restore" in (daemon.start(wait=0).reason or "")
    from latet.cli.main import run_server

    with pytest.raises(SystemExit) as stopped:
        run_server(port=FREE_PORT)
    assert stopped.value.code == 0  # a service manager does not start it again in a loop
    from latet.cli import agent

    monkeypatch.setattr(agent, "agent_name", lambda strict=False: None)
    undone = _cli("restore", "--abort")
    assert undone.exit_code == 0, undone.output
    assert not (data / "restore.json").exists()
    store = Store(data / "latet.db", load_root_key(data))
    assert len(store.load_entities()) == 2  # the address and its domain, readable again
    store._db.close()


def test_refused_in_an_agent_or_with_a_key_from_the_environment(machines, monkeypatch, tmp_path):
    use, _ = machines
    use("d")
    from latet.cli import agent

    monkeypatch.setattr(agent, "agent_name", lambda strict=False: "claude-code")
    assert _cli("backup", str(tmp_path / "x.latet"), input=f"{PASS}\n").exit_code == 2
    assert _cli("restore", str(tmp_path / "x.latet"), input=f"{PASS}\n").exit_code == 2
    monkeypatch.setattr(agent, "agent_name", lambda strict=False: None)
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(b"k" * 32).decode())
    keyed = _cli("restore", str(tmp_path / "x.latet"), input=f"{PASS}\n")
    assert keyed.exit_code == 2 and "LATET_ROOT_KEY" in keyed.output
    monkeypatch.delenv("LATET_ROOT_KEY")
    short = _cli("backup", str(tmp_path / "y.latet"), input="short\n")
    assert short.exit_code == 2 and not (tmp_path / "y.latet").exists()
    monkeypatch.setattr(agent, "at_your_terminal", lambda strict=False: False)
    blind = _cli("restore", str(tmp_path / "x.latet"), input=f"{PASS}\n")
    assert blind.exit_code == 2 and "terminal" in blind.output


def test_values_sealed_with_another_key_are_counted(machines):
    from starlette.testclient import TestClient

    from latet.gateway.app import create_app

    use, _ = machines
    home = use("e")
    _vault_on(home)
    (home / "data" / "root.key").write_text(base64.b64encode(b"x" * 32).decode(), "utf-8")  # another key
    runtime = make_runtime(data=home / "data")
    try:
        assert runtime.gateway.unreadable == 2
        with TestClient(create_app(runtime.gateway), base_url=f"http://127.0.0.1:{FREE_PORT}") as client:
            health = client.get("/_latet/health")
            assert health.json().get("vault") == {"unreadable": 2}, health.text
    finally:
        runtime.close()
