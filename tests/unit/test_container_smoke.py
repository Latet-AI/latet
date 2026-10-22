"""Task15 smoke boundaries, without Docker or an application import."""

import errno
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ..e2e.fake_upstream import FakeUpstream

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def smoke():
    path = ROOT / "scripts" / "container_server_smoke.py"
    assert path.is_file(), "Task15's actual container smoke has not been shipped"
    spec = importlib.util.spec_from_file_location("container_server_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_commands_disable_implicit_user_configuration(smoke, tmp_path):
    root, run = tmp_path / "checkout", tmp_path / "owned"
    command = smoke.compose_command("docker", root, run, "e0c-t15-owned", "config", "--quiet")
    assert command == [
        "docker",
        "--config",
        str(run / "docker-config"),
        "compose",
        "--file",
        str(root / "compose.yaml"),
        "--file",
        str(run / "override.yaml"),
        "--project-directory",
        str(root),
        "--env-file",
        str(run / "compose-empty.env"),
        "--project-name",
        "e0c-t15-owned",
        "--profile",
        "server",
        "config",
        "--quiet",
    ]
    environment = smoke.child_environment(
        {
            "PATH": "tools",
            "SystemRoot": "system",
            "LATET_SERVER_ADMIN_TOKEN": "do-not-inherit",
            "COMPOSE_FILE": "someone-elses.yaml",
            "HTTPS_PROXY": "http://proxy.invalid",
            "DOCKER_HOST": "tcp://elsewhere",
        },
        run,
    )
    assert environment["COMPOSE_DISABLE_ENV_FILE"] == "1"
    assert environment["PATH"] == "tools"
    assert not {"LATET_SERVER_ADMIN_TOKEN", "COMPOSE_FILE", "HTTPS_PROXY", "DOCKER_HOST"} & environment.keys()
    for name in (
        "HOME",
        "USERPROFILE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        assert Path(environment[name]).is_relative_to(run)
    assert Path(environment["TEMP"]).is_relative_to(run)


def test_init_is_created_with_logging_disabled_and_precreated_key(smoke, tmp_path):
    env = {"LATET_SERVER_URL": "https://localhost:18443", "LATET_MASTER_KEY_FILE": "/run/secrets/latet-master-key"}
    files = {"latet-master-key": tmp_path / "master.key"}
    command = smoke.initialization_command("docker", "latet:ci", "e0c-t15-owned", "owned-back", env, files)
    assert command[:2] == ["docker", "create"]
    assert command[command.index("--log-driver") + 1] == "none"
    assert command[command.index("--network") + 1] == "owned-back"
    assert command[command.index("--memory") + 1] == "4g"
    assert command[command.index("--memory-swap") + 1] == "4g"
    assert command[command.index("--cpus") + 1] == "2"
    assert "LATET_MASTER_KEY_FILE=/run/secrets/latet-master-key" in command
    assert f"type=bind,source={tmp_path / 'master.key'},target=/run/secrets/latet-master-key,readonly" in command
    assert command[-3:] == ["latet:ci", "server", "init"]
    with pytest.raises(ValueError, match=r"^initialization requires the pre-created master-key file$"):
        smoke.initialization_command("docker", "latet:ci", "e0c-t15-owned", "owned-back", {}, files)
    with pytest.raises(ValueError, match=r"^initialization requires the pre-created master-key file$"):
        smoke.initialization_command("docker", "latet:ci", "e0c-t15-owned", "owned-back", env, {})


def test_certificate_is_made_by_the_owner_of_its_folder(smoke, tmp_path):
    """`latet server certificate` writes its key only into a folder of its own user that nobody else can write: the
    one-shot runs as the folder's owner, and the folder stays 0700, never opened to everyone for it."""
    directory = tmp_path / "certs"
    directory.mkdir(mode=0o700)
    found = directory.stat()
    assert smoke.folder_owner(directory) == f"{found.st_uid}:{found.st_gid}"
    command = smoke.certificate_command(
        "docker", "latet:ci", "e0c-t15-owned", directory, "https://localhost:18443", user="1001:118"
    )
    assert command[command.index("--user") + 1] == "1001:118"


def test_a_failed_command_is_named_by_its_step_and_never_by_what_it_was_given(smoke, tmp_path):
    def compose(*arguments):
        return smoke.compose_command("docker", tmp_path, tmp_path / "run", "e0c-t15-owned", *arguments)

    assert smoke.step(compose("exec", "-T", "latet-server-1", "latet", "server", "ready")) == (
        "compose exec latet-server-1 latet"
    )
    assert smoke.step(compose("up", "-d", "--wait", "postgres")) == "compose up"
    certificate = smoke.certificate_command("docker", "latet:ci", "p", tmp_path, "https://localhost:1", user="1:1")
    assert smoke.step(["docker", "--config", str(tmp_path), *certificate[1:]]) == "run"


def test_certificate_oneshot_is_owned_bounded_and_restricted(smoke, tmp_path):
    assert hasattr(smoke, "certificate_command"), "the actual bounded certificate command has not been shipped"
    directory = tmp_path / "certs"
    directory.mkdir()
    command = smoke.certificate_command(
        "docker", "latet:ci", "e0c-t15-owned", directory, "https://localhost:18443", user=smoke.folder_owner(directory)
    )
    assert command[:2] == ["docker", "run"]
    assert command[command.index("--name") + 1] == "e0c-t15-owned-cert"
    assert command[command.index("--label") + 1] == "com.docker.compose.project=e0c-t15-owned"
    assert "--rm" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "512m"
    assert command[command.index("--memory-swap") + 1] == "512m"
    assert command[command.index("--cpus") + 1] == "1"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[command.index("--tmpfs") + 1] == "/tmp"
    assert command[command.index("--entrypoint") + 1] == "latet"
    assert f"type=bind,source={directory},target=/certs" in command
    assert command[-8:] == [
        "latet:ci",
        "server",
        "certificate",
        "--make",
        "--url",
        "https://localhost:18443",
        "--out",
        "/certs",
    ]


def test_init_token_is_parsed_only_from_its_once_shown_line(smoke):
    token = "latet_adm_" + "a" * 40
    output = f"The database is up to date.\nAn administration token (adm_test), shown once: keep it in your password manager.\n  {token}\nStart the server.\n"
    assert smoke.administration_token(output.encode()) == token
    for invalid in (token, output + output, "No master key: shown once:\n  " + token):
        with pytest.raises(
            ValueError, match=r"^initialization did not use the existing key and issue one token$"
        ) as error:
            smoke.administration_token(invalid.encode())
        assert token not in str(error.value)


def test_captured_command_output_is_private_and_bounded(smoke, capsys):
    token = "synthetic-credential-never-in-console"
    command = [sys.executable, "-I", "-c", f"print({token!r})"]
    assert smoke.captured(command, timeout=5, max_bytes=128).strip() == token.encode()
    assert capsys.readouterr() == ("", "")
    with pytest.raises(RuntimeError) as error:
        smoke.captured(command, timeout=5, max_bytes=8)
    assert token not in str(error.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX's")
def test_a_command_that_ends_before_its_group_is_killed_is_no_failure(smoke, monkeypatch):
    """macOS answers EPERM, not ESRCH, to a signal for a group whose processes have all ended and wait to be reaped: a
    command that ends between the check that it runs and the kill. Its end, never a failure of the capture."""
    real = smoke.os.killpg

    def ended_first(pgid, signal):
        real(pgid, signal)
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(smoke.os, "killpg", ended_first)
    command = [sys.executable, "-I", "-c", "import time; print('x' * 64, flush=True); time.sleep(30)"]
    with pytest.raises(RuntimeError):  # more output than allowed, killed: what the capture says, not EPERM
        smoke.captured(command, timeout=5, max_bytes=8)


def test_secret_values_cannot_become_init_environment(smoke, tmp_path):
    env = {"LATET_MASTER_KEY_FILE": "/run/secrets/latet-master-key", "LATET_SERVER_ADMIN_TOKEN": "synthetic-secret"}
    with pytest.raises(ValueError, match=r"^initialization accepts secret files only$") as error:
        smoke.initialization_command(
            "docker",
            "latet:ci",
            "e0c-t15-owned",
            "owned-back",
            env,
            {"latet-master-key": tmp_path / "master.key"},
        )
    assert "synthetic-secret" not in str(error.value)


def test_cleanup_refuses_resources_labeled_for_another_project(smoke):
    assert hasattr(smoke, "require_owned_resources"), "cleanup's actual label ownership guard has not been shipped"
    project = "e0c-t15-owned"
    smoke.require_owned_resources(
        [
            {"Config": {"Labels": {"com.docker.compose.project": project}}},
            {"Labels": {"com.docker.compose.project": project}},
        ],
        project,
    )
    for foreign in ({}, {"Labels": {"com.docker.compose.project": "shared-services"}}):
        with pytest.raises(RuntimeError):
            smoke.require_owned_resources([foreign], project)


def test_explicit_compose_executable_keeps_owned_empty_docker_configuration(smoke, tmp_path):
    root, run = tmp_path / "checkout", tmp_path / "owned"
    executable = str(tmp_path / "tools" / "docker-compose.exe")
    command = smoke.compose_command(
        "docker",
        root,
        run,
        "e0c-t15-owned",
        "config",
        "--quiet",
        compose_executable=executable,
    )
    assert command == [
        executable,
        "--file",
        str(root / "compose.yaml"),
        "--file",
        str(run / "override.yaml"),
        "--project-directory",
        str(root),
        "--env-file",
        str(run / "compose-empty.env"),
        "--project-name",
        "e0c-t15-owned",
        "--profile",
        "server",
        "config",
        "--quiet",
    ]
    environment = smoke.child_environment({"DOCKER_CONFIG": "do-not-read-user-config"}, run)
    assert environment["DOCKER_CONFIG"] == str(run / "docker-config")
    assert environment["COMPOSE_DISABLE_ENV_FILE"] == "1"


def test_fake_provider_healthcheck_probes_the_provider_instead_of_the_computer_daemon(smoke):
    assert hasattr(smoke, "fake_provider_healthcheck"), "the actual fake-provider healthcheck has not been shipped"
    fake = FakeUpstream().start()
    try:
        health = smoke.fake_provider_healthcheck(fake.port)
        assert health["test"][:2] == ["CMD", "python"]
        probe = [sys.executable, *health["test"][2:]]
        assert smoke.captured(probe, timeout=5) == b""
    finally:
        fake.stop()
    with pytest.raises(RuntimeError):
        smoke.captured(probe, timeout=5)


@pytest.mark.parametrize("status", [0, 3])
def test_retained_child_output_stays_in_its_owned_receipt_and_out_of_console(smoke, tmp_path, capsys, status):
    owned = tmp_path / "owned"
    owned.mkdir(mode=0o700)
    receipt = owned / "call-0001"
    token = "synthetic-private-init-credential"
    expected = (token + "\n").encode()
    command = [sys.executable, "-I", "-c", f"import sys; sys.stdout.buffer.write({expected!r}); sys.exit({status})"]
    if status:
        with pytest.raises(RuntimeError) as error:
            smoke.captured(command, timeout=5, max_bytes=128, receipt=receipt)
        assert token not in str(error.value)
    else:
        assert smoke.captured(command, timeout=5, max_bytes=128, receipt=receipt) == expected
    assert capsys.readouterr() == ("", "")
    assert receipt.with_suffix(".stdout").read_bytes() == expected
    metadata = json.loads(receipt.with_suffix(".json").read_text("utf-8"))
    assert metadata["returncode"] == status
    assert metadata["captured_bytes"] == len(expected)
    assert metadata["output_limit_bytes"] == 128
    assert set(owned.iterdir()) == {receipt.with_suffix(".stdout"), receipt.with_suffix(".json")}
    assert set(tmp_path.iterdir()) == {owned}


def test_retained_capture_refuses_to_overwrite_an_existing_receipt(smoke, tmp_path):
    receipt = tmp_path / "call-0001"
    output = receipt.with_suffix(".stdout")
    output.write_bytes(b"prior-private-evidence")
    marker = tmp_path / "must-not-run"
    command = [sys.executable, "-I", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    with pytest.raises(FileExistsError):
        smoke.captured(command, timeout=5, receipt=receipt)
    assert output.read_bytes() == b"prior-private-evidence"
    assert not marker.exists()


@pytest.fixture
def inspection_case():
    limits = {
        "latet-server-1": (4294967296, 2000000000),
        "latet-server-2": (4294967296, 2000000000),
        "postgres": (1073741824, 1000000000),
        "valkey": (1342177280, 1000000000),
        "caddy": (268435456, 1000000000),
        "fake-upstream": (268435456, 1000000000),
    }
    memberships = {
        "latet-server-1": {"owned-front": "172.29.20.4", "owned-back": "172.30.20.4"},
        "latet-server-2": {"owned-front": "172.29.20.5", "owned-back": "172.30.20.5"},
        "postgres": {"owned-back": "172.30.20.2"},
        "valkey": {"owned-back": "172.30.20.3"},
        "caddy": {"owned-front": "172.29.20.2"},
        "fake-upstream": {"owned-front": "172.29.20.3"},
    }
    records = {}
    for name, (memory, cpus) in limits.items():
        instance = name.startswith("latet-server-")
        records[name] = {
            "Config": {
                "User": "10001:10001" if instance else "",
                "Env": [
                    "LATET_SERVER_FORWARDED_ALLOW_IPS=172.29.20.2",
                    "LATET_SERVER_METRICS_HOST=127.0.0.1",
                    "LATET_SERVER_METRICS_PORT=7283",
                ],
            },
            "HostConfig": {
                "ReadonlyRootfs": instance,
                "CapDrop": ["ALL"] if instance else [],
                "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"] if instance else [],
                "Tmpfs": {"/tmp": ""} if instance else {},
                "Memory": memory,
                "NanoCpus": cpus,
                "LogConfig": {
                    "Type": "json-file",
                    "Config": {
                        "mode": "non-blocking",
                        "max-buffer-size": "4m",
                        "max-size": "10m",
                        "max-file": "5",
                    },
                },
            },
            "NetworkSettings": {
                "Networks": {network: {"IPAddress": address} for network, address in memberships[name].items()},
                "Ports": {"443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]} if name == "caddy" else {},
            },
        }
    rendered = {
        "networks": {"latet-front": {"name": "owned-front"}, "latet-back": {"name": "owned-back", "internal": True}},
        "services": {
            "caddy": {
                "networks": {"latet-front": {"ipv4_address": "172.29.20.2"}},
                "ports": [{"target": 443, "published": "18443", "host_ip": "127.0.0.1"}],
            },
        },
    }
    actual_networks = {"owned-front": {"Internal": False}, "owned-back": {"Internal": True}}
    return records, rendered, actual_networks


def test_actual_inspection_accepts_the_private_bounded_smoke_shape(smoke, inspection_case):
    assert hasattr(smoke, "verify_inspection"), "the actual-inspection validator has not been shipped"
    smoke.verify_inspection(*inspection_case)


@pytest.mark.parametrize(
    "broken",
    ["network", "internal", "port", "proxy", "uid", "readonly", "capability", "logging", "memory", "cpu", "metrics"],
)
def test_actual_inspection_refuses_a_weakened_container_boundary(smoke, inspection_case, broken):
    assert hasattr(smoke, "verify_inspection"), "the actual-inspection validator has not been shipped"
    records, rendered, actual_networks = inspection_case
    instance = records["latet-server-1"]
    if broken == "network":
        records["caddy"]["NetworkSettings"]["Networks"]["owned-back"] = {"IPAddress": "172.30.20.9"}
    elif broken == "internal":
        actual_networks["owned-back"]["Internal"] = False
    elif broken == "port":
        instance["NetworkSettings"]["Ports"]["5283/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "5283"}]
    elif broken == "proxy":
        instance["Config"]["Env"][0] = "LATET_SERVER_FORWARDED_ALLOW_IPS=*"
    elif broken == "uid":
        instance["Config"]["User"] = "0:0"
    elif broken == "readonly":
        instance["HostConfig"]["ReadonlyRootfs"] = False
    elif broken == "capability":
        instance["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif broken == "logging":
        instance["HostConfig"]["LogConfig"]["Config"]["mode"] = "blocking"
    elif broken == "memory":
        records["fake-upstream"]["HostConfig"]["Memory"] = 0
    elif broken == "cpu":
        instance["HostConfig"]["NanoCpus"] = 3000000000
    else:
        instance["Config"]["Env"][1] = "LATET_SERVER_METRICS_HOST=0.0.0.0"
    with pytest.raises(RuntimeError):
        smoke.verify_inspection(records, rendered, actual_networks)


@pytest.fixture
def valkey_case():
    return {
        "maxmemory": "1073741824",
        "maxmemory-policy": "noeviction",
        "client-output-buffer-limit": "normal 0 0 0 slave 268435456 67108864 60 pubsub 33554432 8388608 60",
    }


def test_actual_valkey_output_proves_memory_policy_pubsub_default_and_auth_support(smoke, valkey_case):
    assert hasattr(smoke, "verify_valkey"), "the actual-Valkey-output validator has not been shipped"
    smoke.verify_valkey(
        valkey_case, b"NOAUTH Authentication required.\n", b"VALKEYCLI_AUTH is supported", "VALKEYCLI_AUTH"
    )


@pytest.mark.parametrize("broken", ["eviction", "unbounded", "too_large", "pubsub", "no_password", "unsupported_auth"])
def test_actual_valkey_output_refuses_a_weakened_backend_boundary(smoke, valkey_case, broken):
    assert hasattr(smoke, "verify_valkey"), "the actual-Valkey-output validator has not been shipped"
    unauthenticated, help_output = b"NOAUTH Authentication required.\n", b"VALKEYCLI_AUTH is supported"
    if broken == "eviction":
        valkey_case["maxmemory-policy"] = "volatile-lru"
    elif broken == "unbounded":
        valkey_case["maxmemory"] = "0"
    elif broken == "too_large":
        valkey_case["maxmemory"] = "2147483648"
    elif broken == "pubsub":
        valkey_case["client-output-buffer-limit"] = "normal 0 0 0 pubsub 0 0 0"
    elif broken == "no_password":
        unauthenticated = b"PONG\n"
    else:
        help_output = b"REDISCLI_AUTH is supported"
    with pytest.raises(RuntimeError):
        smoke.verify_valkey(valkey_case, unauthenticated, help_output, "VALKEYCLI_AUTH")


@pytest.mark.parametrize("leak_at", [None, "logs", "argv"])
def test_actual_captured_logs_and_argv_never_contain_generated_secret_values(smoke, leak_at):
    assert hasattr(smoke, "verify_private_outputs"), "the actual-output secret-absence validator has not been shipped"
    secret = "synthetic-generated-metrics-token"
    outputs = {"logs": b"server ready\n", "argv": b"latet server run\n"}
    if leak_at:
        outputs[leak_at] += secret.encode()
        with pytest.raises(RuntimeError) as error:
            smoke.verify_private_outputs(outputs, [secret])
        assert secret not in str(error.value)
    else:
        smoke.verify_private_outputs(outputs, [secret])


@pytest.fixture
def instance_metrics():
    before = {
        "latet-server-1": b'latet_http_requests_total{kind="agent",code="200"} 3\n',
        "latet-server-2": b'latet_http_requests_total{kind="agent",code="200"} 7\n',
    }
    after = {
        "latet-server-1": b'latet_http_requests_total{kind="agent",code="200"} 4\n',
        "latet-server-2": b'latet_http_requests_total{kind="agent",code="200"} 8\n',
    }
    return before, after


def test_instance_coverage_requires_one_successful_agent_call_on_each_instance(smoke, instance_metrics):
    assert hasattr(smoke, "verify_instance_coverage"), (
        "the independent per-instance canary witness has not been shipped"
    )
    smoke.verify_instance_coverage(*instance_metrics)


def test_instance_coverage_accepts_first_agent_series_after_zero_baseline(smoke):
    assert hasattr(smoke, "verify_instance_coverage"), (
        "the independent per-instance canary witness has not been shipped"
    )
    before = {
        name: b'latet_http_requests_total{kind="health",code="200"} 50\n'
        for name in ("latet-server-1", "latet-server-2")
    }
    after = {
        name: b'latet_http_requests_total{kind="agent",code="200"} 1\n' for name in ("latet-server-1", "latet-server-2")
    }
    smoke.verify_instance_coverage(before, after)


@pytest.mark.parametrize("broken", ["missing_instance", "only_one_instance", "refused", "other_kind", "unchanged"])
def test_instance_coverage_refuses_missing_or_unrelated_success_evidence(smoke, instance_metrics, broken):
    assert hasattr(smoke, "verify_instance_coverage"), (
        "the independent per-instance canary witness has not been shipped"
    )
    before, after = instance_metrics
    if broken == "missing_instance":
        after.pop("latet-server-2")
    elif broken == "only_one_instance":
        after["latet-server-1"] = b'latet_http_requests_total{kind="agent",code="200"} 5\n'
        after["latet-server-2"] = before["latet-server-2"]
    elif broken == "refused":
        after["latet-server-2"] = before["latet-server-2"] + b'latet_http_requests_total{kind="agent",code="403"} 1\n'
    elif broken == "other_kind":
        after["latet-server-2"] = before["latet-server-2"] + b'latet_http_requests_total{kind="admin",code="200"} 1\n'
    else:
        after = before.copy()
    with pytest.raises(RuntimeError):
        smoke.verify_instance_coverage(before, after)
