"""Finite Task7 doctor exposure and real consumer ordering witnesses.

Native ACL/mode/link results apply only to their marked platform. Roaming is a metadata simulation. No installed
agent is launched: only detection is supplied, while the doctor's status/route/record consumers remain real.
"""

import builtins
import io
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from latet.config import data_dir
from latet.doctor import checks
from latet.engine.reviewer import certificate
from latet.integrations import agents, base
from latet.server import connection
from latet.server.client import ServerClient
from latet.server.tokens import route_key
from latet.storage import folders

from .conftest import connect_to, run_cli

TOKEN = "latet_pat_" + "T" * 43
ROUTE = route_key(TOKEN)
TITLE = "Claude Code credentials"


def _controlled(done):
    assert "Traceback" not in done.output
    assert done.exception is None or isinstance(done.exception, SystemExit), type(done.exception).__name__
    assert TOKEN not in done.output and ROUTE not in done.output


def _exposure(done, state):
    _controlled(done)
    lines = [line for line in done.output.splitlines() if TITLE in line]
    assert lines, done.output
    assert any(state in line.lower() for line in lines), done.output
    if state in {"unknown", "exposed"}:
        assert done.exit_code == 1, done.output
        assert not any("absent" in line.lower() for line in lines), done.output


@pytest.fixture
def doctor_here(computer, quiet, monkeypatch):
    for name in (
        "check_daemon",
        "check_port",
        "check_engine",
        "check_recognizers",
        "check_ner",
        "check_ocr",
        "check_reviewer",
        "check_live_review",
        "check_instructions",
    ):
        monkeypatch.setattr(checks, name, lambda: [])
    agent = agents.ClaudeCode()
    monkeypatch.setattr(agents, "ALL", {agent.id: agent})
    monkeypatch.setattr(agent, "detect", lambda: base.AgentInfo(agent.id, agent.name, True, "owned-test"))
    monkeypatch.setattr(agents, "_roaming", lambda folder: None)
    settings = agent.settings_path()
    settings.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    (data_dir() / "integrations.json").write_text("{}", encoding="utf-8")
    return agent, settings


def _settings(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@contextmanager
def _content_guard(monkeypatch, paths, *, deny=False, unreadable=False):
    """Observe real file opens without ever blocking on a special file or following an unsafe fixture link."""
    selected = set(paths)
    opened, reads, writes = [], [], []
    descriptors = set()

    def chosen(value):
        return not isinstance(value, int) and Path(value) in selected

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            reads.append(size)
            return self.stream.read(size)

        def readline(self, size=-1):
            reads.append(size)
            return self.stream.readline(size)

        def readinto(self, value):
            reads.append(len(value))
            return self.stream.readinto(value)

    def intercept(original):
        def call(path, mode="r", *args, **kwargs):
            if chosen(path):
                if any(flag in mode for flag in "wax+"):
                    writes.append(str(path))
                    raise AssertionError("doctor attempted a settings write")
                opened.append(str(path))
                if deny or unreadable:
                    raise PermissionError("owned settings read refused")
                return Stream(original(path, mode, *args, **kwargs))
            return original(path, mode, *args, **kwargs)

        return call

    original_open, original_fdopen = os.open, os.fdopen

    def fd_open(path, flags, *args, **kwargs):
        if chosen(path):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                writes.append(str(path))
                raise AssertionError("doctor attempted a settings write")
            opened.append(str(path))
            if deny or unreadable:
                raise PermissionError("owned settings read refused")
            fd = original_open(path, flags, *args, **kwargs)
            descriptors.add(fd)
            return fd
        return original_open(path, flags, *args, **kwargs)

    def fd_stream(fd, *args, **kwargs):
        stream = original_fdopen(fd, *args, **kwargs)
        return Stream(stream) if fd in descriptors else stream

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", intercept(Path.open))
        patch.setattr(builtins, "open", intercept(builtins.open))
        patch.setattr(io, "open", intercept(io.open))
        patch.setattr(os, "open", fd_open)
        patch.setattr(os, "fdopen", fd_stream)
        yield opened, reads, writes


def _link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("owned native symlink creation unavailable; no link evidence")


def _junction(link, target):
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, "owned junction creation failed"
    assert link.is_junction()


@pytest.mark.parametrize(
    ("value", "state"),
    [
        ({"env": {"LATET_BRIDGE_TOKEN": TOKEN}}, "present"),
        ({"env": {"ANTHROPIC_BASE_URL": "https://latet.acme.lan/u/" + ROUTE}}, "present"),
        ({"env": {"HARMLESS": "owned"}}, "absent"),
        ({"env": "owned-invalid-env-shape"}, "unknown"),
    ],
    ids=["token", "route", "neither", "scalar-env"],
)
def test_doctor_classifies_owned_settings_without_repair(doctor_here, monkeypatch, value, state):
    _, path = doctor_here
    _settings(path, value)
    before = path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
    with _content_guard(monkeypatch, [path]) as (_, _, writes):
        done = run_cli("doctor")
    assert writes == []
    _exposure(done, state)
    assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("kind", ["leaf-link", "parent-link", "directory", "fifo"])
def test_doctor_refuses_unsafe_settings_before_content_or_route(doctor_here, computer, monkeypatch, kind):
    _, path = doctor_here
    target = computer / "exposure-target.json"
    _settings(target, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    if kind == "leaf-link":
        _link(path, target)
    elif kind == "parent-link":
        path.parent.rmdir()
        folder = computer / "exposure-target"
        folder.mkdir(mode=0o700)
        target.rename(folder / path.name)
        target = folder / path.name
        _link(path.parent, folder, directory=True)
    elif kind == "directory":
        path.mkdir()
    else:
        if os.name != "posix":
            pytest.skip("native POSIX FIFO witness; no Windows FIFO claim")
        os.mkfifo(path, mode=0o600)
    routed = []
    monkeypatch.setattr(checks, "route_checks", lambda agent: routed.append("route") or [])
    with _content_guard(monkeypatch, [path, target], deny=True) as (opened, _, writes):
        done = run_cli("doctor")
    _exposure(done, "unknown")
    assert opened == [] and routed == [] and writes == []


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows settings-parent junction")
def test_doctor_refuses_native_settings_junction_parent(doctor_here, computer, monkeypatch):
    _, path = doctor_here
    path.parent.rmdir()
    target = computer / "junction-settings"
    target.mkdir()
    _settings(target / path.name, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _junction(path.parent, target)
    try:
        with _content_guard(monkeypatch, [path, target / path.name], deny=True) as (opened, _, _):
            done = run_cli("doctor")
        _exposure(done, "unknown")
        assert opened == []
    finally:
        assert path.parent.is_junction()
        path.parent.rmdir()  # Exact owned junction only; the target is not traversed or deleted.


@pytest.mark.parametrize("kind", ["malformed", "nonobject", "deep", "unreadable"])
def test_doctor_reports_unavailable_settings_as_unknown(doctor_here, monkeypatch, kind):
    _, path = doctor_here
    raw = {
        "malformed": b'{"env":',
        "nonobject": b"[]",
        "deep": b'{"env":' + b"[" * 128 + b"0" + b"]" * 128 + b"}",
        "unreadable": json.dumps({"env": {"LATET_BRIDGE_TOKEN": TOKEN}}).encode(),
    }[kind]
    path.write_bytes(raw)
    path.chmod(0o600)
    with _content_guard(monkeypatch, [path], unreadable=kind == "unreadable") as (_, _, writes):
        done = run_cli("doctor")
    _exposure(done, "unknown")
    assert writes == [] and path.read_bytes() == raw


@pytest.mark.parametrize("oversized", [False, True], ids=["bounded-positive", "oversized"])
def test_doctor_settings_reads_are_finite(doctor_here, monkeypatch, oversized):
    _, path = doctor_here
    # The already-reviewed record reader's ceiling is an upper bound here, not a guessed future doctor API/constant.
    ceiling = base.RECORD_BYTES + 1
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    if oversized:
        path.write_bytes(b'{"env":{}}' + b" " * ceiling)
    with _content_guard(monkeypatch, [path]) as (_, reads, writes):
        done = run_cli("doctor")
    _exposure(done, "unknown" if oversized else "present")
    assert reads and all(type(size) is int and 0 <= size <= ceiling for size in reads), reads
    assert writes == []


def _applied_claude():
    (data_dir() / "integrations.json").write_text(
        json.dumps({"claude": {"integration": "claude", "applied_at": 1.0}}), encoding="utf-8"
    )


@pytest.mark.skipif(os.name != "posix", reason="native POSIX0600/0644 settings permissions")
def test_doctor_preserves_native_settings_modes_and_reports_exposure(doctor_here):
    """A 0644 settings.json in a 0700 folder is read by nobody else (nobody else goes in): what `chmod 700 ~/.claude`
    makes of the file Claude Code writes 0644. In a folder others go into, it is exposed once Latet put its token there,
    and a warning that names the fix before."""
    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _exposure(run_cli("doctor"), "present")
    path.chmod(0o644)
    try:
        _exposure(run_cli("doctor"), "present")  # its folder is 0700
        path.parent.chmod(0o755)  # others go in (macOS makes ~/.claude so): the file's 0644 lets them read it
        done = run_cli("doctor")
        warned = [line for line in done.output.splitlines() if TITLE in line]
        assert warned and warned[0].startswith("[WARN]") and done.exit_code == 0, done.output
        assert "chmod" in warned[0], done.output
        _applied_claude()
        done = run_cli("doctor")
        _exposure(done, "exposed")
        assert "chmod" in done.output
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    finally:
        path.parent.chmod(0o700)
        path.chmod(0o600)


@pytest.mark.skipif(os.name != "posix", reason="native POSIX folder and file modes")
def test_doctor_reads_settings_yours_alone_in_a_folder_others_only_go_into(doctor_here):
    """macOS makes ~/.claude 0755, and `latet agents apply claude` writes settings.json 0600: others may list the
    folder but read no file in it, so Latet's token there is present, never exposed."""
    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _applied_claude()
    path.parent.chmod(0o755)
    try:
        done = run_cli("doctor")
        _exposure(done, "present")
        assert done.exit_code == 0, done.output
    finally:
        path.parent.chmod(0o700)


def test_doctor_reads_the_settings_its_own_folder_check_admits(doctor_here, monkeypatch):
    """The doctor's own check of Claude Code's folder decides: the stricter one for Latet's own secrets (macOS refuses
    a folder others may read, as it makes ~/.claude) is not asked again when the file is read."""
    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _applied_claude()
    strict = folders.private_folder_problem
    monkeypatch.setattr(
        folders,
        "private_folder_problem",
        lambda folder: f"{folder} can be read by other users" if folder == path.parent else strict(folder),
    )
    monkeypatch.setattr(checks, "_settings_folder_problem", lambda folder: None)
    done = run_cli("doctor")
    _exposure(done, "present")
    assert done.exit_code == 0, done.output


def _not_private(monkeypatch, path):
    """Claude Code's settings folder one that others could change, in the words that name the fix."""
    fix = f"{path.parent} can be written by its group or by other users: `chmod 700 {path.parent}`"
    real = checks._settings_folder_problem
    monkeypatch.setattr(
        checks, "_settings_folder_problem", lambda folder: fix if folder == path.parent else real(folder)
    )


def test_doctor_warns_with_the_fix_when_the_settings_folder_is_not_private_and_latet_keeps_nothing_there(
    doctor_here, monkeypatch
):
    """Claude Code made the folder, Latet never applied it: none of Latet's token or route can be there, so the folder
    is a warning that names the fix, never a failure."""
    _, path = doctor_here
    _settings(path, {"env": {"HARMLESS": "owned"}})
    _not_private(monkeypatch, path)
    with _content_guard(monkeypatch, [path], deny=True) as (opened, _, writes):
        done = run_cli("doctor")
    _controlled(done)
    lines = [line for line in done.output.splitlines() if TITLE in line]
    assert len(lines) == 1 and lines[0].startswith("[WARN]"), done.output
    assert f"chmod 700 {path.parent}" in lines[0] and "exposed" not in lines[0].lower()
    assert done.exit_code == 0, done.output
    assert opened == [] and writes == []


def test_doctor_says_exposed_with_the_fix_when_latet_applied_claude_code_there(doctor_here, monkeypatch):
    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _applied_claude()
    _not_private(monkeypatch, path)
    with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
        done = run_cli("doctor")
    _exposure(done, "exposed")
    assert f"chmod 700 {path.parent}" in done.output and opened == []


def test_doctor_warns_when_latet_applied_claude_code_in_another_settings_folder(doctor_here, computer, monkeypatch):
    """Latet's record names the settings file it wrote: one in another folder (CLAUDE_CONFIG_DIR changed since) put
    nothing of Latet's in this one."""
    _, path = doctor_here
    _settings(path, {"env": {"HARMLESS": "owned"}})
    other = {
        "path": str(computer / "elsewhere" / "settings.json"),
        "backup": None,
        "sha_before": None,
        "sha_after": None,
    }
    (data_dir() / "integrations.json").write_text(
        json.dumps({"claude": {"integration": "claude", "applied_at": 1.0, "files": [other]}}), encoding="utf-8"
    )
    _not_private(monkeypatch, path)
    done = run_cli("doctor")
    _controlled(done)
    lines = [line for line in done.output.splitlines() if TITLE in line]
    assert lines and lines[0].startswith("[WARN]") and done.exit_code == 0, done.output


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows settings DACL")
def test_doctor_preserves_native_foreign_read_acl(doctor_here, monkeypatch):
    from .test_eop_computer_namespace import _icacls

    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    _exposure(run_cli("doctor"), "present")
    parent = folders.folder_grants(path.parent)
    assert folders.private_folder_problem(path.parent) is None
    _icacls(path, "/grant", "*S-1-5-32-545:R")
    try:
        leaf = folders.folder_grants(path)
        assert any(grant.sid == "S-1-5-32-545" for grant in leaf[1])
        assert folders.folder_grants(path.parent) == parent
        assert folders.private_folder_problem(path.parent) is None
        with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
            done = run_cli("doctor")
        warned = [line for line in done.output.splitlines() if TITLE in line]  # Latet never applied Claude Code here
        assert warned and warned[0].startswith("[WARN]") and done.exit_code == 0, done.output
        assert "latet agents apply claude" in warned[0] and opened == []
        _applied_claude()
        with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
            done = run_cli("doctor")
        assert opened == []
        _exposure(done, "exposed")
        assert folders.folder_grants(path) == leaf
        assert folders.folder_grants(path.parent) == parent
    finally:
        _icacls(path, "/remove:g", "*S-1-5-32-545")
        (data_dir() / "integrations.json").write_text("{}", encoding="utf-8")
    _icacls(path.parent, "/grant", "*S-1-5-32-545:(OI)(CI)(RX)")
    try:
        before = folders.folder_grants(path.parent)
        assert any(grant.sid == "S-1-5-32-545" for grant in before[1])
        with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
            done = run_cli("doctor")
        warned = [line for line in done.output.splitlines() if TITLE in line]  # Latet never applied Claude Code here
        assert warned and warned[0].startswith("[WARN]") and done.exit_code == 0, done.output
        assert opened == []
        _applied_claude()
        with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
            done = run_cli("doctor")
        _exposure(done, "exposed")
        assert opened == [] and folders.folder_grants(path.parent) == before
    finally:
        _icacls(path.parent, "/remove:g", "*S-1-5-32-545")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows roaming metadata propagation simulation")
def test_doctor_reports_roaming_unknown_before_content(doctor_here, monkeypatch):
    _, path = doctor_here
    _settings(path, {"env": {"LATET_BRIDGE_TOKEN": TOKEN}})
    checked = []

    def roaming(folder):
        assert folder == path.parent
        checked.append(folder)
        return "owned Windows profile roams"

    monkeypatch.setattr(agents, "_roaming", roaming)
    with _content_guard(monkeypatch, [path], deny=True) as (opened, _, _):
        done = run_cli("doctor")
    _exposure(done, "unknown")
    assert checked and opened == []


@pytest.fixture
def connected_here(latet_server, quiet):
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    latet_server.asked.clear()
    return latet_server


def _owned_directory_link(link, target):
    if sys.platform == "win32":
        _junction(link, target)
    else:
        _link(link, target, directory=True)


@pytest.mark.parametrize("boundary", ["selected-connection-parent", "certificate-child"])
def test_diagnostic_real_selected_boundary_precedes_sensitive_work(
    connected_here, doctor_here, computer, monkeypatch, boundary
):
    if boundary == "selected-connection-parent":
        target = computer / "outside-connection"
        target.mkdir(mode=0o700)
        selected = target / "connection.yaml"
        selected.write_bytes(connection.connection_path().read_bytes())
        link = computer / "selected-parent"
        _owned_directory_link(link, target)
        monkeypatch.setenv("LATET_CONNECTION", str(link / selected.name))
        paths = [link / selected.name, selected]
    else:
        link = data_dir() / "certificates"
        target = computer / "outside-certificates"
        link.rename(target)
        _owned_directory_link(link, target)
        paths = [*target.iterdir(), *(link / item.name for item in target.iterdir())]
    used = []

    def forbidden(name):
        def call(*args, **kwargs):
            used.append(name)
            raise PermissionError("owned sensitive consumer refused")

        return call

    monkeypatch.setattr(connection, "_copies", forbidden("credential-store"))
    monkeypatch.setattr(ServerClient, "__init__", forbidden("client"))
    monkeypatch.setattr(checks, "check_agents", forbidden("agents"))
    try:
        with _content_guard(monkeypatch, paths, deny=True) as (opened, _, _):
            done = run_cli("doctor")
        _controlled(done)
        assert done.exit_code == 1 and "private" in done.output.lower(), done.output
        assert opened == [] and used == [] and connected_here.asked == []
    finally:
        if sys.platform == "win32":
            assert link.is_junction()
            link.rmdir()
        else:
            assert link.is_symlink()
            link.unlink()


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_absent_selected_state_is_noncreating(doctor_here, computer, monkeypatch, command):
    missing = computer / "missing-data" / "private"
    selected = computer / "missing-connection" / "private" / "connection.yaml"
    monkeypatch.setenv("LATET_DATA_DIR", str(missing))
    monkeypatch.setenv("LATET_CONNECTION", str(selected))
    created = []
    mkdir = Path.mkdir

    def made(path, *args, **kwargs):
        if path in (missing, selected.parent) or missing in path.parents or selected.parent in path.parents:
            created.append(path)
            raise AssertionError("diagnostic attempted to create selected state")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", made)
    done = run_cli(command)
    _controlled(done)
    assert created == [] and not missing.exists() and not selected.parent.exists()


def test_doctor_expiry_uses_checked_pem_snapshot(connected_here, doctor_here, monkeypatch):
    saved = connection.load_connection()
    assert saved is not None and saved.fingerprint is not None
    path = data_dir() / "certificates" / (saved.fingerprint.removeprefix("sha256:") + ".pem")
    original = path.read_bytes()
    expected = original.decode().replace("\r\n", "\n").replace("\r", "\n")
    read_pem, expires = connection._server_pem, certificate.expires
    parsed, reopened = [], []
    captured = False
    path_open = Path.open

    def snapshot(data, fingerprint, *, strict=False):
        nonlocal captured
        text = read_pem(data, fingerprint, strict=strict)
        assert text == expected
        path.write_bytes(b"owned-invalid-pem-after-snapshot")
        captured = True
        return text

    def open_after(path_, mode="r", *args, **kwargs):
        if path_ == path and captured and "r" in mode:
            reopened.append(path_)
            raise PermissionError("owned unchecked PEM reopen")
        return path_open(path_, mode, *args, **kwargs)

    def expiry(text):
        parsed.append(text)
        return expires(text)

    monkeypatch.setattr(connection, "_server_pem", snapshot)
    monkeypatch.setattr(certificate, "expires", expiry)
    monkeypatch.setattr(Path, "open", open_after)
    done = run_cli("doctor")
    _controlled(done)
    assert captured and parsed == [expected] and reopened == []


def test_missing_token_does_not_hide_safe_certificate_expiry(connected_here, doctor_here, monkeypatch):
    seen = []
    expiry = certificate.expires

    def expires(text):
        seen.append(text)
        return expiry(text)

    monkeypatch.setattr(connection, "load_server_token", lambda data, saved, *, _selected=None: None)
    monkeypatch.setattr(certificate, "expires", expires)
    done = run_cli("doctor")
    _controlled(done)
    assert done.exit_code == 1 and "token" in done.output.lower(), done.output
    assert len(seen) == 1 and "Server certificate: expires" in done.output
    assert connected_here.asked == []


def test_doctor_escapes_actual_failed_connection_path(connected_here, doctor_here, computer, monkeypatch):
    positive = run_cli("doctor")
    _controlled(positive)
    assert positive.exit_code == 0, positive.output
    connected_here.asked.clear()
    control = chr(0x202E)  # A real Windows/POSIX filename character, never an invalid Windows ESC filename.
    path = computer / f"connection-{control}.yaml"
    path.write_text("url: []\nuser: invalid\nuser_name: owned-malformed-sentinel\n", encoding="utf-8")
    monkeypatch.setenv("LATET_CONNECTION", str(path))
    done = run_cli("doctor")
    _controlled(done)
    assert done.exit_code == 1 and "connection" in done.output.lower(), done.output
    assert control not in done.output and "owned-malformed-sentinel" not in done.output
    assert connected_here.asked == []


@pytest.mark.parametrize("consumer", ["check_agents", "route_checks", "user_name_checks"])
def test_disconnected_doctor_admits_records_before_agent_consumers(doctor_here, computer, monkeypatch, consumer):
    agent, path = doctor_here
    _settings(path, {"env": {"HARMLESS": "owned"}})
    positive = run_cli("doctor")
    _controlled(positive)
    assert positive.exit_code == 0, positive.output
    assert "Claude Code" in positive.output, positive.output
    target = computer / "unsafe-record-target"
    target.mkdir(mode=0o700)
    (target / "integrations.json").write_text("{}", encoding="utf-8")
    selected = computer / "unsafe-record-link"
    _owned_directory_link(selected, target)
    monkeypatch.setenv("LATET_DATA_DIR", str(selected))
    called = []

    def forbidden(name):
        def call(*args, **kwargs):
            called.append(name)
            raise PermissionError("owned consumer before record admission")

        return call

    monkeypatch.setattr(checks, "load_config", forbidden("config"))
    actual_consumer = getattr(checks, consumer)
    if consumer == "check_agents":
        monkeypatch.setattr(agent, "detect", forbidden("detect"))
    else:
        monkeypatch.setattr(checks, "check_agents", lambda: actual_consumer(agent))
    try:
        with _content_guard(monkeypatch, [selected / "integrations.json", target / "integrations.json"], deny=True) as (
            opened,
            _,
            _,
        ):
            done = run_cli("doctor")
            direct_error, direct_checks = None, []
            try:
                direct_checks = actual_consumer() if consumer == "check_agents" else actual_consumer(agent)
            except Exception as exc:
                direct_error = type(exc)
        _controlled(done)
        assert done.exit_code == 1, done.output
        assert "record" in done.output.lower() or "private" in done.output.lower(), done.output
        assert called == [] and opened == []
        assert direct_error in (None, base.RecordsUnavailable, connection.NotConnected)
        assert direct_error is not None or any(item.status == checks.FAIL for item in direct_checks)
    finally:
        if sys.platform == "win32":
            assert selected.is_junction()
            selected.rmdir()
        else:
            assert selected.is_symlink()
            selected.unlink()
