"""Task 14 installer boundaries; all paths and process responses belong to the test."""

import importlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from .conftest import run_cli


@pytest.fixture
def service():
    return importlib.import_module("latet.server.service")


@pytest.fixture
def descriptor(service):
    return service.WindowsSecurity(
        service.ADMINISTRATORS,
        True,
        (
            service.WindowsACE(service.SYSTEM, service.FULL, 3),
            service.WindowsACE(service.ADMINISTRATORS, service.FULL, 3),
            service.WindowsACE(service.SERVICE_SID, service.READ_EXECUTE, 3),
        ),
        True,
    )


def test_service_acl_accepts_only_its_complete_protected_policy(service, descriptor):
    assert service.acl_problem(descriptor, "unused") is None
    writable = replace(
        descriptor,
        entries=(*descriptor.entries[:2], replace(descriptor.entries[2], mask=service.MODIFY)),
    )
    assert service.acl_problem(writable, "unused", writable=True) is None
    assert service.acl_problem(writable, "unused") is not None
    assert service.acl_problem(descriptor, "unused", writable=True) is not None


@pytest.mark.parametrize(
    "changed",
    ["owner", "missing-owner", "protection", "foreign", "unknown", "missing", "inherited", "child-only", "deny"],
)
def test_service_acl_refuses_incomplete_foreign_or_ambiguous_metadata(service, descriptor, changed):
    if changed == "owner":
        descriptor = replace(descriptor, owner="S-1-5-21-1-2-3-1001")
    elif changed == "missing-owner":
        descriptor = replace(descriptor, owner=None)
    elif changed == "protection":
        descriptor = replace(descriptor, protected=False)
    elif changed == "foreign":
        descriptor = replace(descriptor, entries=(*descriptor.entries, service.WindowsACE("S-1-5-21-9", 1, 3)))
    elif changed == "unknown":
        descriptor = replace(descriptor, entries=(*descriptor.entries, service.WindowsACE("", 0, 0, 9)))
    elif changed == "missing":
        descriptor = replace(descriptor, entries=descriptor.entries[:2])
    else:
        entry = descriptor.entries[2]
        replacement = replace(
            entry, flags={"inherited": 19, "child-only": 11}.get(changed, 3), kind=1 if changed == "deny" else 0
        )
        descriptor = replace(descriptor, entries=(*descriptor.entries[:2], replacement))
    assert service.acl_problem(descriptor, "unused") is not None


def test_acl_text_localized_names_and_unreadable_metadata_never_authorize(service):
    for value in ("", "BUILTIN\\Administrators:(F)", "Administratoren:(F)", None):
        assert service.acl_problem(value, "unused") is not None


def test_secret_file_metadata_does_not_read_the_contents(service, tmp_path, monkeypatch):
    path = tmp_path / "key"
    path.write_bytes(b"synthetic-secret")
    monkeypatch.setattr(service, "platform", lambda: "windows")
    monkeypatch.setattr(service, "_namespace_problem", lambda path, **kwargs: None)
    descriptor = service.WindowsSecurity(
        service.SYSTEM,
        True,
        tuple(
            service.WindowsACE(sid, mask, 0)
            for sid, mask in (
                (service.SYSTEM, service.FULL),
                (service.ADMINISTRATORS, service.FULL),
                (service.SERVICE_SID, service.READ_EXECUTE),
            )
        ),
        False,
    )
    monkeypatch.setattr(service, "windows_security", lambda path: descriptor)

    def forbidden(*args, **kwargs):
        pytest.fail("secret metadata inspection read content")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert service.secret_file_problem(path) is None
    monkeypatch.setattr(service, "windows_security", lambda path: replace(descriptor, directory=True))
    assert service.secret_file_problem(path) is not None


def test_windows_root_is_protected_before_children_and_commands(service, tmp_path, monkeypatch):
    root = tmp_path / "Latet"
    events = []
    monkeypatch.setattr(service, "_namespace_problem", lambda path, **kwargs: None)
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: None)
    monkeypatch.setattr(service, "_tree_problem", lambda path: None)
    monkeypatch.setattr(service, "windows_path_problem", lambda path, **kwargs: None)

    def create(path, *, writable):
        events.append((path.name, writable))
        path.mkdir()

    monkeypatch.setattr(service, "_create_private_directory", create)
    monkeypatch.setattr(service, "_run", lambda command: events.append(command[1]))
    service.install_windows(service.windows_commands([r"C:\Latet\python.exe", "-m", "latet.server.winservice"]), root)
    assert events[:3] == [("Latet", False), ("data", True), ("logs", True)]
    assert events[3:] == ["create", "description", "failure", "failureflag", "config"]


@pytest.mark.parametrize("failure", ["ancestor", "root", "child", "postcondition"])
def test_unsafe_windows_targets_are_refused_without_touching_existing_contents(service, tmp_path, monkeypatch, failure):
    root = tmp_path / "Latet"
    root.mkdir()
    sentinel = root / "unrelated"
    sentinel.write_text("owned sentinel", encoding="utf-8")
    events = []
    monkeypatch.setattr(
        service, "_namespace_problem", lambda path, **kwargs: "unsafe" if failure == "ancestor" else None
    )
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: None)
    monkeypatch.setattr(service, "_tree_problem", lambda path: "unsafe" if failure == "child" else None)
    monkeypatch.setattr(
        service,
        "windows_path_problem",
        lambda path, **kwargs: (
            "unsafe" if failure == "root" or (failure == "postcondition" and path.name == "data") else None
        ),
    )

    def create(path, *, writable):
        events.append(path.name)
        path.mkdir()

    monkeypatch.setattr(service, "_create_private_directory", create)
    monkeypatch.setattr(service, "_run", lambda command: pytest.fail("unsafe installation reached the service manager"))
    with pytest.raises(service.InstallError, match=r"\S"):
        service.install_windows(service.windows_commands([r"C:\Latet\python.exe"]), root)
    assert sentinel.read_text(encoding="utf-8") == "owned sentinel"
    if failure != "postcondition":
        assert events == []


@pytest.mark.parametrize("failure", ["timeout", "exit", "os"])
def test_external_commands_are_bounded_and_errors_hide_captured_secrets(service, monkeypatch, failure):
    def failed(command, **kwargs):
        assert Path(command[0]).is_absolute()
        assert kwargs["timeout"] == 30
        assert kwargs.get("shell", False) is False
        assert kwargs["stdout"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output="synthetic-secret")
        if failure == "exit":
            raise subprocess.CalledProcessError(1, command, stderr="synthetic-secret")
        raise OSError("synthetic-secret")

    monkeypatch.setattr(service.subprocess, "run", failed)
    with pytest.raises(service.InstallError) as error:
        service._run([str(Path.cwd() / "owned-tool"), "argument"])
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["executable", "prefix", "base_prefix", "package", "configuration"])
def test_install_provenance_checks_every_privileged_input(service, tmp_path, monkeypatch, kind):
    candidates = {name: tmp_path / name for name in ("executable", "prefix", "base_prefix", "package", "configuration")}
    for path in candidates.values():
        path.mkdir()
    monkeypatch.setattr(service.sys, "executable", str(candidates["executable"]))
    monkeypatch.setattr(service.sys, "prefix", str(candidates["prefix"]))
    monkeypatch.setattr(service.sys, "base_prefix", str(candidates["base_prefix"]))
    monkeypatch.setattr(service, "_package_path", lambda: candidates["package"])
    monkeypatch.setattr(service, "in_a_home", lambda places, roots: None)
    monkeypatch.setattr(service, "_package_problem", lambda path: None)
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: "unsafe" if path == candidates[kind] else None)
    monkeypatch.setattr(service, "secret_file_problem", lambda path, **kwargs: None)
    assert service.installation_problem("linux", candidates["configuration"]) is not None


@pytest.mark.parametrize(
    ("users", "groups", "said"),
    [
        ((), (), "the latet user and group, which this machine does not have"),
        ((), ("latet",), "the latet user, which this machine does not have"),
        (("latet",), (), "the latet group, which this machine does not have"),
    ],
    ids=["neither", "no user", "no group"],
)
def test_a_missing_latet_user_or_group_is_named_before_the_configuration(
    service, tmp_path, monkeypatch, users, groups, said
):
    """The unit runs as User=latet and Group=latet: a machine without either is told which, and how to create it,
    rather than that the configuration must be readable by a group it does not have (D-I1)."""

    def lookup(names):
        def found(name):
            if name not in names:
                raise KeyError(name)
            return SimpleNamespace(pw_uid=990, gr_gid=990)

        return found

    config = tmp_path / "server.yaml"
    config.write_text("url: https://latet.acme.lan", encoding="utf-8")
    monkeypatch.setattr(service, "_account_lookups", lambda: (lookup(users), lookup(groups)))
    monkeypatch.setattr(service, "in_a_home", lambda places, roots: None)
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: None)
    monkeypatch.setattr(service, "_package_problem", lambda path: None)
    monkeypatch.setattr(service, "secret_file_problem", lambda path, **kwargs: "the configuration's problem")
    problem = service.installation_problem("linux", config) or ""
    assert said in problem and ("useradd" in problem or "groupadd" in problem), problem
    assert "readable by the latet group" not in problem
    monkeypatch.setattr(service, "_account_lookups", lambda: (lookup(("latet",)), lookup(("latet",))))
    assert "readable by the latet group" in (service.installation_problem("linux", config) or "")


def test_an_installation_in_a_home_names_where_uv_puts_latet_and_its_python(service, tmp_path, monkeypatch):
    """uv puts a tool, its command and the Python it runs on in three places of the person's home: each has its
    variable, and an installation for the machine sets all three."""
    config = tmp_path / "server.yaml"
    config.write_text("url: https://latet.acme.lan", encoding="utf-8")
    monkeypatch.setattr(service, "in_a_home", lambda places, roots: places[0])
    problem = service.installation_problem("linux", config) or ""
    assert all(name in problem for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR")), problem


@pytest.fixture
def printable(service, tmp_path, monkeypatch, quiet):
    import latet.cli.server as cli
    from latet.config import Config

    config = tmp_path / "server.yaml"
    config.write_text("url: https://latet.acme.lan\n", encoding="utf-8")
    monkeypatch.setattr(service, "platform", lambda: "linux")
    monkeypatch.setattr(service, "installation_problem", lambda kind, path: None)
    monkeypatch.setattr(service, "privileged", lambda kind: True)
    monkeypatch.setattr(cli, "_local", lambda path: Config())
    monkeypatch.setattr("latet.config.in_container", lambda: False)
    return config


def test_human_print_and_decline_never_mutate(service, printable, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("print or refusal mutated service installation")

    monkeypatch.setattr(service, "install_systemd", forbidden)
    monkeypatch.setattr(service, "install_windows", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    printed = run_cli("server", "service", "install", "--config", str(printable), "--print")
    assert printed.exit_code == 0, printed.output
    assert "ProtectSystem=strict" in printed.output
    monkeypatch.setattr("latet.cli.agent.at_your_terminal", lambda: True)
    declined = run_cli("server", "service", "install", "--config", str(printable), input="n\n")
    assert declined.exit_code != 0 and "Nothing changed" in declined.output


def test_failed_install_never_reports_success_or_start_hint(service, printable, monkeypatch):
    def failed(*args, **kwargs):
        raise service.InstallError("The service manager refused installation.")

    monkeypatch.setattr(service, "install_systemd", failed)
    result = run_cli("server", "service", "install", "--config", str(printable), "--yes")
    assert result.exit_code == 2
    assert "systemctl start" not in result.output
    assert "The service is installed and enabled at boot." not in result.output


@pytest.fixture
def linux_machine_files(service, tmp_path, monkeypatch, quiet):
    """Real CLI/config reader; only OS identity and path metadata are synthetic."""
    import latet.cli.server as cli
    from latet.storage import folders

    config, secret = tmp_path / "server.yaml", tmp_path / "selected-secret"
    secret.write_text("synthetic-secret", encoding="utf-8")
    config.write_text(json.dumps({"master_key_file": str(secret)}), encoding="utf-8")
    service_uid, service_gid = 410, 420
    metadata = {
        path: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0, st_nlink=1) for path in secret.parents
    }
    metadata[tmp_path] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o750, st_uid=0, st_gid=service_gid, st_nlink=1)
    for path in (config, secret):
        metadata[path] = SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_uid=0, st_gid=service_gid, st_nlink=1)
    original_plain = service._plain
    monkeypatch.setattr(service, "_plain", lambda path: metadata[path] if path in metadata else original_plain(path))
    monkeypatch.setattr(service, "platform", lambda: "linux")
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(
            getpwnam=lambda name: SimpleNamespace(pw_name=name, pw_uid=service_uid, pw_gid=service_gid),
            getpwall=lambda: [],
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "grp",
        SimpleNamespace(
            getgrnam=lambda name: SimpleNamespace(gr_name=name, gr_gid=service_gid, gr_mem=[]),
        ),
    )
    # Existing generic POSIX namespace code may reach the host's Windows SID
    # boundary. Neutral descriptors keep that boundary owned without changing
    # sys.platform, os.name, pathlib classes or the service namespace checker.
    monkeypatch.setattr(folders, "my_sid", lambda: service.SYSTEM)
    monkeypatch.setattr(folders, "folder_grants", lambda path: (service.SYSTEM, []))
    monkeypatch.setattr(service, "in_a_home", lambda places, roots: None)
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: None)
    monkeypatch.setattr(service, "_package_problem", lambda path: None)
    monkeypatch.setattr("latet.config.in_container", lambda: False)
    called = []
    settings = SimpleNamespace(master_key_file=secret)

    def validated(path):
        called.append(path)
        return settings

    monkeypatch.setattr(cli, "_local", validated)
    original_open = os.open

    def content_open(path, *args, **kwargs):
        if Path(path) == secret:
            pytest.fail("service metadata preflight read the selected secret")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", content_open)
    return SimpleNamespace(
        config=config, secret=secret, metadata=metadata, called=called, settings=settings, gid=service_gid
    )


@pytest.mark.parametrize("target", ["secret", "config"])
@pytest.mark.parametrize(
    "policy", ["safe", "root-only", "unrelated-group", "group-write", "other-access", "no-traverse"]
)
def test_linux_install_checks_access_for_the_service_identity(service, linux_machine_files, target, policy):
    files = linux_machine_files
    path = files.secret if target == "secret" else files.config
    if target == "config":
        files.config.write_text("{}\n", encoding="utf-8")
        del files.settings.master_key_file
    if policy == "root-only":
        files.metadata[path].st_mode = stat.S_IFREG | 0o600
        files.metadata[path].st_gid = 0
    elif policy == "unrelated-group":
        files.metadata[path].st_gid = files.gid + 1
    elif policy == "group-write":
        files.metadata[path].st_mode = stat.S_IFREG | 0o660
    elif policy == "other-access":
        files.metadata[path].st_mode = stat.S_IFREG | 0o644
    elif policy == "no-traverse":
        files.metadata[path.parent].st_mode = stat.S_IFDIR | 0o740
    result = run_cli("server", "service", "install", "--config", str(files.config), "--print")
    if policy == "safe":
        assert result.exit_code == 0, result.output
        assert files.called == [files.config]
        assert "User=latet" in result.output and "Group=latet" in result.output
    else:
        assert result.exit_code == 2, result.output
        assert files.called == [], "unsafe service metadata reached secret-reading settings validation"
        assert "ExecStart=" not in result.output
    assert "synthetic-secret" not in result.output


@pytest.mark.parametrize("failed_step", [None, 0, 1, 2, 3, 4])
def test_windows_partial_install_stays_disabled_under_the_virtual_account(service, tmp_path, monkeypatch, failed_step):
    root = tmp_path / "Latet"
    for path in (root, root / "data", root / "logs"):
        path.mkdir()
    monkeypatch.setattr(service, "_namespace_problem", lambda path, **kwargs: None)
    monkeypatch.setattr(service, "trusted_path_problem", lambda path: None)
    monkeypatch.setattr(service, "_tree_problem", lambda path: None)
    monkeypatch.setattr(service, "windows_path_problem", lambda path, **kwargs: None)
    persisted, snapshots, commands = {}, [], []

    def scm(command):
        index = len(commands)
        commands.append(command)
        if index == failed_step:
            raise service.InstallError("The service manager refused installation.")
        verb = command[1]
        options = dict(zip(command[3::2], command[4::2], strict=False))
        if verb == "create":
            persisted.update(account=options.get("obj=", "LocalSystem"), start=options.get("start=", "demand"))
        elif verb == "config":
            if "obj=" in options:
                persisted["account"] = options["obj="]
            if "start=" in options:
                persisted["start"] = options["start="]
        snapshots.append(dict(persisted))

    monkeypatch.setattr(service, "_run", scm)
    steps = service.windows_commands([r"C:\Latet\python.exe", "-m", "latet.server.winservice"], root)
    if failed_step is None:
        service.install_windows(steps, root)
        assert persisted == {"account": r"NT SERVICE\LatetServer", "start": "delayed-auto"}
        assert commands[-1][1] == "config" and "start=" in commands[-1]
        assert {command[1] for command in commands[:-1]} >= {"description", "failure", "failureflag"}
    else:
        with pytest.raises(service.InstallError, match="service manager refused"):
            service.install_windows(steps, root)
        assert len(commands) == failed_step + 1
    assert all(state["account"] == r"NT SERVICE\LatetServer" for state in snapshots)
    unfinished = snapshots[:-1] if failed_step is None else snapshots
    assert all(state["start"] == "disabled" for state in unfinished)


@pytest.mark.parametrize(
    "field",
    [
        "master_key_file",
        "database_password_file",
        "valkey_password_file",
        "metrics_token_file",
        "reviewer_key_file",
        "key",
    ],
)
def test_secret_metadata_is_checked_before_validated_settings_read_it(service, printable, monkeypatch, field):
    import latet.cli.server as cli

    secret = printable.with_name("selected-secret")
    secret.write_text("synthetic-secret", encoding="utf-8")
    printable.write_text(json.dumps({field: str(secret)}), encoding="utf-8")
    monkeypatch.setattr(service, "platform", lambda: "windows")

    def metadata(path):
        directory = path != secret
        flags = 3 if directory else 0
        return service.WindowsSecurity(
            "S-1-5-21-1-2-3-1001" if path == secret else service.SYSTEM,
            True,
            tuple(
                service.WindowsACE(sid, mask, flags)
                for sid, mask in (
                    (service.SYSTEM, service.FULL),
                    (service.ADMINISTRATORS, service.FULL),
                    (service.SERVICE_SID, service.READ_EXECUTE),
                )
            ),
            directory,
        )

    monkeypatch.setattr(service, "windows_security", metadata)
    called = []

    def validated(path):
        called.append("validated settings")
        return SimpleNamespace(**{field: secret})

    monkeypatch.setattr(cli, "_local", validated)
    original_open = os.open

    def content_open(path, *args, **kwargs):
        if Path(path) == secret:
            pytest.fail("preflight read the selected secret's contents")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", content_open)
    result = run_cli("server", "service", "install", "--config", str(printable), "--print")
    assert result.exit_code == 2, result.output
    assert field in result.output
    assert called == []
    assert "synthetic-secret" not in result.output


@pytest.mark.parametrize("arguments", [(), ("--yes",), ("--print",), ("--yes", "--print")])
def test_agent_refusal_precedes_even_metadata_and_privilege_checks(service, monkeypatch, arguments):
    monkeypatch.setenv("CODEX_THREAD_ID", "synthetic-service-agent")

    def forbidden(*args, **kwargs):
        pytest.fail("agent reached service metadata or privilege work")

    for name in ("platform", "privileged", "installation_problem", "windows_security", "windows_folder"):
        monkeypatch.setattr(service, name, forbidden)
    result = run_cli("server", "service", "install", *arguments)
    assert result.exit_code == 2
    assert "agent" in result.output.lower()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows argv evidence needs Windows")
def test_native_windows_python_receives_the_generated_argument_boundaries(service, tmp_path):
    arguments = ["", "plain", "two words", 'D:\\a "b"\\', "%NAME% $HOME & |", "ends\\"]
    code = "import json,sys; print(json.dumps(sys.argv[1:]))"
    steps = service.windows_commands([sys.executable, "-I", "-S", "-c", code, *arguments], tmp_path)
    binary = steps[0][steps[0].index("binPath=") + 1]
    result = subprocess.run(
        binary,
        executable=sys.executable,
        cwd=tmp_path,
        timeout=15,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert json.loads(result.stdout) == arguments
    assert result.stderr == ""


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows SID evidence needs Windows")
def test_native_windows_computes_the_same_service_sid_without_registering_it(service):
    result = subprocess.run(
        [str(service._system_directory() / "sc.exe"), "showsid", service.SERVICE_NAME],
        timeout=15,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
    )
    # Numeric SID extraction is evidence only; production authorization never parses command output.
    found = re.findall(r"S-1-5-80-(?:\d+-){4}\d+", result.stdout)
    assert found == [service.SERVICE_SID]
