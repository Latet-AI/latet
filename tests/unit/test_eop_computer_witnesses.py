"""Further finite computer EOP witnesses against the reviewed dependency's actual call sites."""

import builtins
import errno
import hashlib
import importlib
import io
import json
import os
from pathlib import Path

import pytest
import typer

from latet.config import Config, data_dir
from latet.integrations import agents, base
from latet.server import client, connection
from latet.storage import folders

URL = "https://eop-computer.example.test"
TOKEN = "latet_pat_" + "w" * 43
KEY = "latet_ck_" + "w" * 43
PIN = "sha256:" + "ab" * 32
MARKER = "planted_sensitive_fault_9741"


def _forbidden(*args, **kwargs):
    pytest.fail("a side effect preceded the record trust refusal")


def _unsafe(monkeypatch):
    def problem(folder):
        return "folder open to another account"

    monkeypatch.setattr(folders, "private_folder_problem", problem)
    if hasattr(base, "private_folder_problem"):
        monkeypatch.setattr(base, "private_folder_problem", problem)


def _store(raw):
    path = base.manifest_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _controlled(call):
    with pytest.raises(Exception, match=r"(?i)folder|record|integration|unavailable") as caught:
        call()
    assert not isinstance(caught.value, (AssertionError, OSError, TypeError, json.JSONDecodeError))


@pytest.mark.parametrize("failed", ["revoke", "copies", "pin"])
@pytest.mark.parametrize("trusted", [False, True], ids=["new-pin", "existing-pin"])
def test_every_cleanup_phase_is_attempted_once_after_an_independent_failure(
    computer, monkeypatch, capsys, caplog, failed, trusted
):
    command = importlib.import_module("latet.cli.connect")
    from latet.engine.reviewer import certificate

    original = KeyboardInterrupt("original interruption")
    calls = []

    class Enrolment:
        def __init__(self, address, token, fingerprint):
            assert (address, token, fingerprint) == (URL, None, PIN)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def enrol(self, code, label):
            calls.append("enrol")
            return {"token": TOKEN, "computer_key": KEY, "identity": {"id": "u_eopcomputr", "name": "Synthetic Person"}}

    def keep(value, token, key=None, *, cleanup=True):
        calls.append("keep")
        assert cleanup is False
        assert (value.url, value.user, value.fingerprint, token, key) == (URL, "u_eopcomputr", PIN, TOKEN, KEY)
        raise original

    def phase(name):
        def attempt(*args):
            calls.append(name)
            if name == failed:
                raise OSError(errno.EACCES, MARKER)
            return [] if name == "copies" else None

        return attempt

    monkeypatch.setattr(client, "ServerClient", Enrolment)
    monkeypatch.setattr(client, "pin_certificate", lambda *args: None)
    monkeypatch.setattr(certificate, "load", lambda *args: "saved synthetic pin" if trusted else None)
    monkeypatch.setattr(certificate, "forget", phase("pin"))
    monkeypatch.setattr(connection, "keep_connection", keep)
    monkeypatch.setattr(connection, "remove_server_token", phase("copies"))
    monkeypatch.setattr(command, "_revoke", phase("revoke"))
    monkeypatch.setattr(command, "_send_identity", _forbidden)
    monkeypatch.setattr(command, "_refresh", _forbidden)
    monkeypatch.setattr("latet.service.daemon.status", _forbidden)
    with pytest.raises(KeyboardInterrupt) as caught:
        command._connected(URL, PIN, "synthetic-one-time-code")
    assert caught.value is original
    assert calls == ["enrol", "keep", "revoke", "copies", *([] if trusted else ["pin"])]
    said = capsys.readouterr()
    assert MARKER not in said.out + said.err + caplog.text
    assert TOKEN not in said.out + said.err + caplog.text


@pytest.mark.parametrize("outcome", ["401", "timeout", "revoke-error", "local-left"])
def test_failed_keep_reports_uncertain_revocation_without_retry_or_removing_another_connection(
    computer, monkeypatch, capsys, caplog, outcome
):
    command = importlib.import_module("latet.cli.connect")
    old = connection.Connection(url="https://prior.example.test", user="u_priorstate", user_name="Previous Person")
    connection.save_connection(old)
    path = connection.connection_path()
    before = path.read_bytes()
    calls = []

    class Enrolment:
        def __init__(self, address, token, fingerprint):
            assert (address, fingerprint) == (URL, None)
            assert token in (None, TOKEN)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def enrol(self, code, label):
            calls.append("enrol")
            return {"token": TOKEN, "computer_key": KEY, "identity": {"id": "u_eopcomputr", "name": "Synthetic Person"}}

        def revoke(self):
            calls.append("revoke")
            if outcome in ("401", "timeout"):
                raise client.ServerError(401 if outcome == "401" else 0, "unconfirmed revocation")
            if outcome == "revoke-error":
                raise OSError(errno.EACCES, MARKER)

    def keep(*args, cleanup=True):
        calls.append("keep")
        assert cleanup is False
        raise OSError(errno.EACCES, MARKER)

    def copies(folder):
        calls.append("copies")
        return ["synthetic private token file"] if outcome == "local-left" else []

    monkeypatch.setattr(client, "ServerClient", Enrolment)
    monkeypatch.setattr(connection, "keep_connection", keep)
    monkeypatch.setattr(connection, "remove_server_token", copies)
    monkeypatch.setattr(command, "_send_identity", _forbidden)
    monkeypatch.setattr(command, "_refresh", _forbidden)
    monkeypatch.setattr("latet.service.daemon.status", _forbidden)
    with pytest.raises(typer.Exit) as caught:
        command._connected(URL, None, "synthetic-one-time-code")
    assert caught.value.exit_code == 1
    assert calls == ["enrol", "keep", "revoke", "copies"]
    assert path.read_bytes() == before
    output = capsys.readouterr()
    said = output.out + output.err
    assert TOKEN not in said + caplog.text
    assert MARKER not in said + caplog.text
    assert "nothing" not in said.lower()
    if outcome != "local-left":
        assert "server revoked" not in said.lower()
        assert "people tokens u_eopcomputr" in said
    else:
        assert "remain" in said.lower()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EROFS, errno.ENOTDIR])
def test_fallback_mkdir_failure_precedes_pin_and_enrol(computer, monkeypatch, capsys, error):
    command = importlib.import_module("latet.cli.connect")
    mkdir = Path.mkdir

    def failed(self, *args, **kwargs):
        if self == data_dir():
            raise OSError(error, MARKER)
        return mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failed)
    monkeypatch.setattr(client, "pin_certificate", _forbidden)
    monkeypatch.setattr(client, "ServerClient", _forbidden)
    with pytest.raises(typer.Exit) as caught:
        command._connected(URL, PIN, "synthetic-one-time-code")
    assert caught.value.exit_code != 0
    said = capsys.readouterr()
    assert MARKER not in said.out + said.err


@pytest.mark.parametrize("operation", ["claude", "codex", "opencode", "refresh", "save", "backup", "restore"])
def test_disconnected_mutators_refuse_before_configuration_backup_or_plugin_side_effect(
    computer, monkeypatch, operation
):
    from latet import wrappers
    from latet.cli import admin

    sentinel = computer / "outside-record-store.txt"
    sentinel.write_bytes(b"outside sentinel")
    forged = {
        "path": str(sentinel),
        "backup": str(sentinel),
        "sha_before": None,
        "sha_after": hashlib.sha256(sentinel.read_bytes()).hexdigest(),
    }
    raw = json.dumps(
        {
            name: {"integration": name, "applied_at": 1, "files": [forged], "data": {}}
            for name in ("claude", "codex", "opencode")
        }
    ).encode()
    path = _store(raw)
    _unsafe(monkeypatch)
    monkeypatch.setattr(agents.ClaudeCode, "prepare_marketplace", _forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "_claude", _forbidden)
    monkeypatch.setattr(agents, "register_destination", _forbidden)
    monkeypatch.setattr(wrappers, "user_name_surrogate", _forbidden)
    monkeypatch.setattr(wrappers, "codex_uses_chatgpt", lambda: False)
    monkeypatch.setattr(agents.Codex, "outdated", lambda self: None)
    edit = base.FileEdit(str(sentinel), str(sentinel), None, hashlib.sha256(sentinel.read_bytes()).hexdigest())
    actions = {
        "claude": lambda: agents.ClaudeCode().apply(config=Config()),
        "codex": lambda: agents.Codex().apply(config=Config(), chatgpt=False),
        "opencode": lambda: agents.OpenCode().apply(config=Config()),
        "refresh": admin.refresh_applied,
        "save": lambda: base.save_records({}),
        "backup": lambda: base.backup(sentinel),
        "restore": lambda: base.restore(edit),
    }
    opened = Path.open

    def guarded_open(self, *args, **kwargs):
        assert self not in (path, sentinel), "record or backup was opened before preflight"
        return opened(self, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "open", guarded_open)
        _controlled(actions[operation])
    assert path.read_bytes() == raw
    assert sentinel.read_bytes() == b"outside sentinel"
    assert not (data_dir() / "backups").exists()


def test_mixed_valid_and_malformed_rows_cannot_be_saved_as_a_reduced_manifest(computer, monkeypatch):
    raw = b'{"claude":{"integration":"claude","applied_at":1,"files":[],"data":{}},"codex":null}'
    path = _store(raw)
    monkeypatch.setattr(agents.ClaudeCode, "revert", _forbidden)
    monkeypatch.setattr(agents, "backup", _forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "prepare_marketplace", _forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "_claude", _forbidden)
    monkeypatch.setattr(agents, "register_destination", _forbidden)
    _controlled(lambda: agents.ClaudeCode().apply(config=Config()))
    assert path.read_bytes() == raw
    assert not (data_dir() / "backups").exists()


def test_save_rechecks_store_after_a_successful_read(computer, monkeypatch):
    raw = b"{}"
    path = _store(raw)
    records = base.load_records()
    _unsafe(monkeypatch)
    _controlled(lambda: base.save_records(records))
    assert path.read_bytes() == raw


def test_trusted_disconnect_scrub_cannot_read_an_unsafe_disconnected_manifest(computer, monkeypatch):
    command = importlib.import_module("latet.cli.connect")
    raw = b"{}"
    path = _store(raw)
    _unsafe(monkeypatch)
    read = command._read_regular

    def guarded_read(target):
        assert target != path, "direct manifest read bypassed trust"
        return read(target)

    monkeypatch.setattr(command, "_read_regular", guarded_read)
    _controlled(lambda: command._forget_files([], unsafe=False))
    assert path.read_bytes() == raw


@pytest.mark.parametrize("root", ["integrations.json", "backups"])
def test_quarantine_rename_failure_is_controlled_and_preserves_original(computer, monkeypatch, capsys, root):
    command = importlib.import_module("latet.cli.connect")
    manifest = _store(b"original manifest")
    backups = data_dir() / "backups"
    backups.mkdir()
    backup = backups / "original.txt"
    backup.write_bytes(b"original backup")
    target = data_dir() / root
    rename = Path.rename
    attempted = []

    def failed(self, destination):
        if self == target:
            attempted.append(self)
            raise OSError(errno.EACCES, MARKER)
        return rename(self, destination)

    if os.name == "nt":
        monkeypatch.setattr(Path, "rename", failed)
    else:
        native = importlib.import_module("latet.integrations.quarantine")
        quarantine = native.quarantine_record

        def failed_native(folder, name):
            if folder / name == target:
                attempted.append(target)
                raise OSError(errno.EACCES, MARKER)
            return quarantine(folder, name)

        monkeypatch.setattr(native, "quarantine_record", failed_native)
    failure = None
    result = None
    try:
        result = command._quarantine_records()
    except Exception as exc:
        failure = exc
    assert attempted == [target], "the platform's actual quarantine failure boundary was not reached once"
    assert failure is None and result is not None and target in result
    output = capsys.readouterr()
    words = str(failure).lower() if failure is not None else (output.out + output.err).lower()
    assert MARKER not in words
    assert any(word in words for word in ("record", "quarantin", "retain"))
    if root == "integrations.json":
        assert manifest.read_bytes() == b"original manifest"
    else:
        assert backup.read_bytes() == b"original backup"
    assert not target.with_name(root + ".untrusted").exists()


def test_unsafe_prior_ca_records_cannot_be_swallowed_as_no_personal_ca(computer, monkeypatch):
    from latet import wrappers

    settings = agents.ClaudeCode().settings_path()
    settings.parent.mkdir(parents=True)
    own_bundle = connection.connection_dir() / "claude.pem"
    settings.write_text(json.dumps({"env": {"NODE_EXTRA_CA_CERTS": str(own_bundle)}}), "utf-8")
    path = _store(b"{}")
    _unsafe(monkeypatch)
    _controlled(wrappers._claude_settings_certificates)
    assert path.read_bytes() == b"{}"


def test_new_record_store_is_created_privately_before_its_first_save(computer):
    assert not data_dir().exists()
    base.save_records({})
    assert folders.private_folder_problem(data_dir()) is None
    assert base.load_records() == {}


def test_nonregular_record_backup_is_refused_before_restore_copy(computer, monkeypatch):
    path = _store(b"{}")
    destination = computer / "agent-settings.txt"
    destination.write_bytes(b"current settings")
    backup = data_dir() / "backups" / "not-a-regular-file"
    backup.mkdir(parents=True)
    current_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    edit = base.FileEdit(str(destination), str(backup), None, current_hash)
    monkeypatch.setattr(base.shutil, "copy2", _forbidden)
    _controlled(lambda: base.restore(edit))
    assert path.read_bytes() == b"{}"
    assert destination.read_bytes() == b"current settings"


def test_manifest_allocation_is_bounded_at_the_actual_io_calls(computer, monkeypatch):
    limit = 16 * 1024 * 1024
    raw = b"{}" + b" " * (limit - 2)
    path = _store(raw)
    opened, file_open, native_open, native_read, native_close = builtins.open, io.open, os.open, os.read, os.close
    descriptors = set()
    requested = []
    received = 0

    def budget(size):
        assert 0 <= size <= limit + 1, "manifest read requested an unbounded allocation"
        requested.append(size)

    def charge_count(count):
        nonlocal received
        received += count
        assert received <= limit + 1, "manifest aggregate reads exceeded the byte ceiling plus sentinel"

    def charge(value):
        charge_count(len(value))
        return value

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            if name in ("buffer", "raw"):
                return Reader(getattr(self.stream, name))
            assert name not in ("readlines", "peek"), "record loader selected an unaccounted content operation"
            return getattr(self.stream, name)

        def __iter__(self):
            pytest.fail("manifest iteration bypassed the bounded read")

        def read(self, size=-1):
            budget(size)
            return charge(self.stream.read(size))

        def read1(self, size=-1):
            budget(size)
            return charge(self.stream.read1(size))

        def readline(self, size=-1):
            budget(size)
            return charge(self.stream.readline(size))

        def readinto(self, buffer):
            budget(len(buffer))
            count = self.stream.readinto(buffer)
            charge_count(count)
            return count

        def readinto1(self, buffer):
            budget(len(buffer))
            count = self.stream.readinto1(buffer)
            charge_count(count)
            return count

    def watched(open_file):
        def wrapper(target, *args, **kwargs):
            stream = open_file(target, *args, **kwargs)
            is_record = target in descriptors if isinstance(target, int) else Path(target) == path
            return Reader(stream) if is_record else stream

        return wrapper

    def fd_open(target, *args, **kwargs):
        fd = native_open(target, *args, **kwargs)
        if Path(target) == path:
            descriptors.add(fd)
        return fd

    def fd_read(fd, size):
        if fd in descriptors:
            budget(size)
            return charge(native_read(fd, size))
        return native_read(fd, size)

    def fd_close(fd):
        descriptors.discard(fd)
        return native_close(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", watched(opened))
        scoped.setattr(io, "open", watched(file_open))
        scoped.setattr(os, "open", fd_open)
        scoped.setattr(os, "read", fd_read)
        scoped.setattr(os, "close", fd_close)
        assert base.load_records() == {}
    assert requested, "no actual bounded content read was observed"
    assert path.read_bytes() == raw


@pytest.mark.parametrize("command", ["apply", "revert", "global-install", "global-uninstall"])
def test_command_mutators_refuse_unsafe_records_before_their_first_side_effect(computer, quiet, monkeypatch, command):
    from latet.cli import admin

    from .conftest import run_cli

    path = _store(b"{}")
    _unsafe(monkeypatch)
    for agent in agents.ALL.values():
        monkeypatch.setattr(type(agent), "detect", lambda self: base.AgentInfo("synthetic", "Synthetic", False))
    monkeypatch.setattr(agents.ClaudeCode, "prepare_marketplace", _forbidden)
    monkeypatch.setattr(agents.ClaudeCode, "_claude", _forbidden)
    monkeypatch.setattr(agents, "register_destination", _forbidden)
    monkeypatch.setattr(admin, "_install_port", lambda current, option: (current, None))
    monkeypatch.setattr("latet.config.save_config", _forbidden)
    monkeypatch.setattr("latet.service.autostart.uninstall", _forbidden)
    monkeypatch.setattr("latet.service.autostart.install", _forbidden)
    monkeypatch.setattr("latet.service.daemon.start", _forbidden)
    monkeypatch.setattr("latet.service.daemon.stop", _forbidden)
    args = ("agents", command, "claude") if command in ("apply", "revert") else (command,)
    if command == "global-install":
        args += (
            "--yes",
            "--agents",
            "none",
            "--no-ner",
            "--no-ocr",
            "--no-identity",
            "--no-autostart",
            "--no-dashboard",
        )
    done = run_cli(*args)
    assert done.exit_code != 0
    assert isinstance(done.exception, SystemExit), "record error escaped the CLI's controlled refusal boundary"
    assert any(word in done.output.lower() for word in ("record", "integration", "folder", "unavailable"))
    assert path.read_bytes() == b"{}"
