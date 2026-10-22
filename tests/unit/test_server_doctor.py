"""Server-machine doctor uses selected settings and secret metadata, never secret bytes."""

import builtins
import io
import json
import os
import stat
from pathlib import Path

import pytest

from latet.doctor import checks
from latet.server import settings

from .conftest import connect_to, run_cli

_LOCAL_GROUPS = (
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
)
_FLAGS = ("database_private_network", "valkey_private_network", "valkey_unauthenticated")
_SECRETS = (
    "master_key_file",
    "database_password_file",
    "valkey_password_file",
    "metrics_token_file",
    "reviewer_key_file",
    "key",
)


@pytest.fixture
def machine(computer, monkeypatch):
    from latet.server import service

    # These tests own every selected path. Local probes are unrelated external operations.
    for variable in (*settings._VARIABLES.values(), "LATET_SERVER_CONFIG", "SSLKEYLOGFILE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ProgramData", str(computer / "program-data"))
    monkeypatch.setattr(service, "windows_folder", lambda: computer / "program-data" / "Latet")
    local = []
    for name in _LOCAL_GROUPS:

        def probe(name=name):
            local.append(name)
            return [checks.Check(checks.OK, name)]

        monkeypatch.setattr(checks, name, probe)
    return computer, local


def _select(machine, monkeypatch, data):
    folder, _ = machine
    path = folder / "server.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("LATET_SERVER_CONFIG", str(path))
    return path


def _named(found, name):
    return [check for check in found if name in f"{check.title} {check.detail}"]


def test_selected_server_config_reports_each_network_exception_and_key_logging(machine, monkeypatch):
    _select(machine, monkeypatch, {name: True for name in _FLAGS})
    monkeypatch.setenv("SSLKEYLOGFILE", "private-keylog-path-sentinel")

    found = checks.run_all()

    for name in (*_FLAGS, "SSLKEYLOGFILE"):
        assert any(check.status == checks.WARN for check in _named(found, name)), (name, found)
    assert "private-keylog-path-sentinel" not in repr(found)


def test_server_machine_skips_personal_daemon_engine_and_agent_probes(machine, monkeypatch):
    _select(machine, monkeypatch, {"database_private_network": True})

    found = checks.run_all()

    assert machine[1] == [], found
    assert any("server" in check.title.lower() for check in found), found


@pytest.mark.parametrize("field", _SECRETS)
def test_selected_server_secret_missing_metadata_fails_without_showing_path(machine, monkeypatch, field):
    secret = machine[0] / f"private-path-sentinel-{field}"
    _select(machine, monkeypatch, {field: str(secret)})

    found = checks.run_all()

    assert any(check.status == checks.FAIL for check in _named(found, field)), found
    assert secret.name not in repr(found)


def test_nonempty_environment_overrides_config_network_flags(machine, monkeypatch):
    _select(machine, monkeypatch, {name: True for name in _FLAGS})
    for name in _FLAGS:
        monkeypatch.setenv(settings._VARIABLES[name], "false")

    found = checks.run_all()

    assert any("server" in check.title.lower() for check in found), found
    for name in _FLAGS:
        assert not any(check.status == checks.WARN for check in _named(found, name)), found


def test_environment_server_selection_reports_flags_without_config_file(machine, monkeypatch):
    monkeypatch.setenv("LATET_SERVER_DATABASE_PRIVATE_NETWORK", "1")

    found = checks.run_all()

    assert any(check.status == checks.WARN for check in _named(found, "database_private_network")), found
    assert machine[1] == [], found


def test_server_url_alone_keeps_the_disconnected_personal_doctor(machine, monkeypatch):
    # LATET_SERVER_URL also names a remote administration target; it is not machine configuration alone.
    monkeypatch.setenv("LATET_SERVER_URL", "https://remote.example.invalid")

    found = checks.run_all()

    assert machine[1] == list(_LOCAL_GROUPS), found


@pytest.fixture
def metadata(machine, monkeypatch):
    from latet.server import service

    base = machine[0] / "program-data" / "Latet"
    for name in ("data", "logs"):
        (base / name).mkdir(parents=True, exist_ok=True)
    overrides = {}

    def descriptor(path):
        path = Path(path)
        if path in overrides:
            value = overrides[path]
            if isinstance(value, Exception):
                raise value
            return value
        directory = stat.S_ISDIR(path.lstat().st_mode)
        flags = 3 if directory else 0
        mask = 0x1301BF if path in (base / "data", base / "logs") else 0x1200A9
        return service.WindowsSecurity(
            "S-1-5-18",
            True,
            (
                service.WindowsACE("S-1-5-18", 0x1F01FF, flags),
                service.WindowsACE("S-1-5-32-544", 0x1F01FF, flags),
                service.WindowsACE(service.SERVICE_SID, mask, flags),
            ),
            directory,
        )

    monkeypatch.setattr(service, "platform", lambda: "windows")
    monkeypatch.setattr(service, "windows_security", descriptor)
    return service, overrides, descriptor, base


def _forbid_secret_reads(monkeypatch, paths):
    from latet.server import instance

    attempts = []
    paths = {os.path.normcase(os.path.abspath(path)) for path in paths}

    def guarded(reader):
        def read(path, *args, **kwargs):
            if (
                isinstance(path, (str, bytes, os.PathLike))
                and os.path.normcase(os.path.abspath(os.fsdecode(path))) in paths
            ):
                attempts.append("secret contents")
                raise AssertionError("doctor attempted a secret-content read")
            return reader(path, *args, **kwargs)

        return read

    def settings_loader(*args, **kwargs):
        attempts.append("validated server settings")
        raise AssertionError("doctor invoked secret-reading settings validation")

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
    monkeypatch.setattr(os, "open", guarded(os.open))
    monkeypatch.setattr(settings, "load_server_settings", settings_loader)
    monkeypatch.setattr(instance, "settings", settings_loader)
    return attempts


@pytest.mark.parametrize("field", _SECRETS)
def test_doctor_cli_checks_private_secret_metadata_without_reading_contents(machine, metadata, monkeypatch, field):
    secret = machine[0] / f"private-file-sentinel-{field}"
    secret.write_text("secret-content-sentinel", encoding="utf-8")
    _select(machine, monkeypatch, {field: str(secret)})
    attempts = _forbid_secret_reads(monkeypatch, [secret])

    done = run_cli("doctor")

    assert done.exit_code == 0, done.output
    assert field in done.output
    assert attempts == []
    for private in (secret.name, "secret-content-sentinel", "Traceback"):
        assert private not in done.output


@pytest.mark.parametrize("field", _SECRETS)
@pytest.mark.parametrize("problem", ["owner", "reader", "unprotected", "unavailable", "directory", "unknown-ace"])
def test_server_secret_unknown_or_unsafe_metadata_is_a_failure(machine, metadata, monkeypatch, field, problem):
    from dataclasses import replace

    service, overrides, descriptor, _ = metadata
    secret = machine[0] / f"secret-path-sentinel-{field}"
    secret.write_text("secret-content-sentinel", encoding="utf-8")
    safe = descriptor(secret)
    if problem == "owner":
        bad = replace(safe, owner="S-1-5-21-901-902-903-904")
    elif problem == "reader":
        bad = replace(safe, entries=(*safe.entries, service.WindowsACE("S-1-1-0", 0x120089, 0)))
    elif problem == "unprotected":
        bad = replace(safe, protected=False)
    elif problem == "unavailable":
        bad = OSError("metadata-error-private-sentinel")
    elif problem == "directory":
        bad = replace(safe, directory=True)
    else:
        bad = replace(safe, entries=(*safe.entries, service.WindowsACE("S-1-5-18", 0x120089, 0, 99)))
    overrides[secret] = bad
    _select(machine, monkeypatch, {field: str(secret)})
    attempts = _forbid_secret_reads(monkeypatch, [secret])

    found = checks.run_all()

    assert any(check.status == checks.FAIL for check in _named(found, field)), found
    assert attempts == []
    for private in (secret.name, "secret-content-sentinel", "metadata-error-private-sentinel"):
        assert private not in repr(found)


@pytest.mark.parametrize("field", _SECRETS)
@pytest.mark.parametrize("environment", ["selected", "empty"])
def test_secret_metadata_uses_nonempty_environment_precedence(machine, metadata, monkeypatch, field, environment):
    missing = machine[0] / f"missing-secret-sentinel-{field}"
    private = machine[0] / f"private-secret-sentinel-{field}"
    private.write_text("private-content-sentinel", encoding="utf-8")
    _select(machine, monkeypatch, {field: str(missing)})
    monkeypatch.setenv(settings._VARIABLES[field], str(private) if environment == "selected" else "")
    attempts = _forbid_secret_reads(monkeypatch, [missing, private])

    found = checks.run_all()

    expected = checks.OK if environment == "selected" else checks.FAIL
    assert any(check.status == expected for check in _named(found, field)), found
    assert attempts == []
    assert "secret-sentinel" not in repr(found)


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_network_flags_follow_the_settings_boolean_and_environment_rules(machine, metadata, monkeypatch, value):
    _select(machine, monkeypatch, {name: True for name in _FLAGS})
    for name in _FLAGS:
        monkeypatch.setenv(settings._VARIABLES[name], value)

    found = checks.run_all()

    for name in _FLAGS:
        assert any(check.status == checks.WARN for check in _named(found, name)) is (value == ""), found


@pytest.mark.parametrize(
    "payload",
    [
        b"password: [private-yaml-sentinel\n",
        b"[private-yaml-sentinel]",
        b"private-yaml-sentinel\xff",
        b"key: &secret private-yaml-sentinel\nmaster_key_file: *secret\n",
        b"private-yaml-sentinel" + b" " * (1024 * 1024),
    ],
    ids=["malformed", "list", "encoding", "alias", "oversized"],
)
def test_server_config_failures_have_controlled_cli_output(machine, metadata, monkeypatch, payload):
    path = _select(machine, monkeypatch, {})
    path.write_bytes(payload)

    done = run_cli("doctor")

    assert done.exit_code == 1, done.output
    assert "Server settings" in done.output
    assert "private-yaml-sentinel" not in done.output
    assert str(path) not in done.output
    assert "Traceback" not in done.output


@pytest.mark.parametrize("field", [*_FLAGS, *_SECRETS])
def test_invalid_diagnostic_setting_does_not_print_its_value(machine, metadata, monkeypatch, field):
    _select(machine, monkeypatch, {field: {"private-setting-sentinel": "secret-value"}})

    found = checks.run_all()

    assert any(check.status == checks.FAIL for check in _named(found, field)), found
    assert "private-setting-sentinel" not in repr(found)
    assert "secret-value" not in repr(found)


@pytest.mark.parametrize("target", ["root", "data", "logs"])
def test_windows_server_folder_requires_private_root_and_writable_data_and_logs(machine, metadata, monkeypatch, target):
    from dataclasses import replace

    service, overrides, descriptor, base = metadata
    _select(machine, monkeypatch, {})
    path = base if target == "root" else base / target
    safe = descriptor(path)
    if target == "logs":
        overrides[path] = replace(safe, protected=False)
    else:
        mask = 0x1301BF if target == "root" else 0x1200A9
        overrides[path] = replace(safe, entries=(*safe.entries[:-1], service.WindowsACE(service.SERVICE_SID, mask, 3)))

    found = checks.run_all()

    title = "Server folder" if target == "root" else f"Server {target} folder"
    assert any(check.title == title and check.status == checks.FAIL for check in found), found


def test_connected_computer_keeps_authenticated_route_even_with_server_environment(
    latet_server, machine, quiet, monkeypatch
):
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    latet_server.asked.clear()
    _select(machine, monkeypatch, {"database_private_network": True})

    found = checks.run_all()

    assert any(check.title == "Server token" and check.status == checks.OK for check in found), found
    assert latet_server.asked == [("GET", "whoami")]
    assert machine[1] == ["check_agents", "check_instructions"], found
    assert not _named(found, "database_private_network")


class TestDefaultMachine:
    @pytest.mark.parametrize("has_config", [True, False])
    def test_existing_default_windows_root_with_unsafe_acl_fails_without_environment(
        self, machine, metadata, has_config
    ):
        from dataclasses import replace

        _, overrides, descriptor, base = metadata
        if has_config:
            (base / "server.yaml").write_text("{}", encoding="utf-8")
        overrides[base] = replace(descriptor(base), protected=False)

        found = checks.run_all()

        assert any(check.title == "Server folder" and check.status == checks.FAIL for check in found), found
        assert machine[1] == [], found

    def test_default_windows_config_reports_flags_and_secret_metadata_without_environment(
        self, machine, metadata, monkeypatch
    ):
        _, _, _, base = metadata
        secret = base / "private-default-secret-sentinel"
        secret.write_text("private-default-content-sentinel", encoding="utf-8")
        (base / "server.yaml").write_text(
            json.dumps({"database_private_network": True, "master_key_file": str(secret)}), encoding="utf-8"
        )
        attempts = _forbid_secret_reads(monkeypatch, [secret])

        found = checks.run_all()

        assert any(check.status == checks.WARN for check in _named(found, "database_private_network")), found
        assert any(check.status == checks.OK for check in _named(found, "master_key_file")), found
        assert machine[1] == [], found
        assert attempts == []
        assert "private-default" not in repr(found)

    @pytest.mark.parametrize("target", ["root", "config"])
    def test_unreadable_default_windows_artifact_is_not_absence(self, machine, metadata, monkeypatch, target):
        _, _, _, base = metadata
        config = base / "server.yaml"
        config.write_text("{}", encoding="utf-8")
        unreadable = base if target == "root" else config
        actual = Path.lstat

        def inspect(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError("private-default-error-sentinel")
            return actual(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", inspect)
        attempts = _forbid_secret_reads(monkeypatch, [config])

        found = checks.run_all()

        assert any(check.status == checks.FAIL and "Server" in check.title for check in found), found
        assert machine[1] == [], found
        assert attempts == []
        assert "private-default-error-sentinel" not in repr(found)

    @pytest.mark.parametrize("target", ["root", "config"])
    def test_default_windows_reparse_artifact_is_refused_before_config_read(
        self, machine, metadata, monkeypatch, target
    ):
        from types import SimpleNamespace

        _, _, _, base = metadata
        config = base / "server.yaml"
        config.write_text("{}", encoding="utf-8")
        linked = base if target == "root" else config
        actual = Path.lstat

        def inspect(path, *args, **kwargs):
            found = actual(path, *args, **kwargs)
            if path == linked:
                return SimpleNamespace(st_mode=found.st_mode, st_file_attributes=0x400)
            return found

        monkeypatch.setattr(Path, "lstat", inspect)
        attempts = _forbid_secret_reads(monkeypatch, [config])

        found = checks.run_all()

        assert any(check.status == checks.FAIL and "Server" in check.title for check in found), found
        assert machine[1] == [], found
        assert attempts == []

    @pytest.mark.parametrize("platform", ["windows", "linux"])
    def test_absent_default_artifact_keeps_personal_doctor(self, machine, monkeypatch, platform):
        from latet.server import service

        monkeypatch.setattr(service, "platform", lambda: platform)

        found = checks.run_all()

        assert machine[1] == list(_LOCAL_GROUPS), found

    def test_existing_linux_default_config_selects_actual_settings(self, machine, monkeypatch):
        from latet.server import service

        monkeypatch.setattr(service, "platform", lambda: "linux")
        service.LINUX_CONFIG.parent.mkdir(parents=True)
        service.LINUX_CONFIG.write_text("valkey_unauthenticated: true\n", encoding="utf-8")

        found = checks.run_all()

        assert any(check.status == checks.WARN for check in _named(found, "valkey_unauthenticated")), found
        assert machine[1] == [], found

    def test_unreadable_linux_default_config_is_not_absence(self, machine, monkeypatch):
        from latet.server import service

        monkeypatch.setattr(service, "platform", lambda: "linux")
        actual = Path.lstat

        def inspect(path, *args, **kwargs):
            if path == service.LINUX_CONFIG:
                raise PermissionError("private-linux-default-sentinel")
            return actual(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", inspect)

        found = checks.run_all()

        assert any(check.status == checks.FAIL and "Server" in check.title for check in found), found
        assert machine[1] == [], found
        assert "private-linux-default-sentinel" not in repr(found)

    def test_explicit_config_environment_wins_over_default_config(self, machine, metadata, monkeypatch):
        _, _, _, base = metadata
        (base / "server.yaml").write_text(
            json.dumps({"database_private_network": True, "master_key_file": str(base / "missing")}), encoding="utf-8"
        )
        _select(machine, monkeypatch, {"database_private_network": False})

        found = checks.run_all()

        assert any(check.title == "Server settings" and check.status == checks.OK for check in found), found
        assert not _named(found, "database_private_network")
        assert not _named(found, "master_key_file")
        assert machine[1] == [], found

    def test_connected_computer_has_priority_over_existing_default_service(
        self, latet_server, machine, metadata, quiet
    ):
        from dataclasses import replace

        _, overrides, descriptor, base = metadata
        (base / "server.yaml").write_text("database_private_network: true\n", encoding="utf-8")
        overrides[base] = replace(descriptor(base), protected=False)
        done = connect_to(latet_server)
        assert done.exit_code == 0, done.output
        latet_server.asked.clear()

        found = checks.run_all()

        assert any(check.title == "Server token" and check.status == checks.OK for check in found), found
        assert latet_server.asked == [("GET", "whoami")]
        assert machine[1] == ["check_agents", "check_instructions"], found
        assert not _named(found, "database_private_network")
        assert not any(check.title == "Server folder" for check in found), found

    def test_existing_default_windows_root_with_missing_config_stays_in_server_group(self, machine, metadata):
        found = checks.run_all()

        assert any(check.title == "Server settings" and check.status == checks.FAIL for check in found), found
        assert machine[1] == [], found
