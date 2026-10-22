"""Task 15 contracts against shipped artifacts and, separately, Docker's rendered JSON.

No command is executed here. The execution seat supplies LATET_TEST_CONTAINER_COMPOSE_JSON
after rendering with an explicit empty env file and synthetic inputs; see the evidence plan.
Missing shipped features are structural failures, not evidence of a caught security mutation.
"""

import ipaddress
import json
import os
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTANCES = ("latet-server-1", "latet-server-2")
SERVER = {"postgres", "valkey", "caddy", *INSTANCES}
SECRET_FILES = {
    "latet-master-key": "master.key",
    "latet-db-password": "db.password",
    "latet-valkey-password": "valkey.password",
    "latet-metrics-token": "metrics.token",
}


@pytest.fixture(params=["source", "docker-rendered"])
def compose(request):
    if request.param == "source":
        return yaml.safe_load((ROOT / "compose.yaml").read_text("utf-8"))
    supplied = os.environ.get("LATET_TEST_CONTAINER_COMPOSE_JSON")
    if supplied is None:
        pytest.skip("Docker-rendered evidence deferred: supply task-owned config JSON after exclusive-seat validation")
    path = Path(supplied)
    assert path.suffix == ".json", "only the explicitly supplied rendered JSON is read"
    return json.loads(path.read_text("utf-8"))


@pytest.fixture
def services(compose):
    actual = compose["services"]
    assert actual.keys() >= SERVER, "Task 15 server profile has not been shipped"
    return actual


def _networks(service):
    return set(service.get("networks") or [])


def _command(service, key="command"):
    command = service.get(key) or []
    return command if isinstance(command, str) else " ".join(command)


def _active_text(path):
    assert path.is_file(), f"Task 15 required artifact missing: {path.relative_to(ROOT)}"
    return "\n".join(line.split("#", 1)[0] for line in path.read_text("utf-8").splitlines())


def test_computer_remains_the_default(compose):
    computer = compose["services"]["latet"]
    assert not computer.get("profiles")
    port = computer["ports"][0]
    if isinstance(port, str):
        assert port == "127.0.0.1:${LATET_PORT:-5283}:${LATET_PORT:-5283}"
    else:
        assert port["host_ip"] == "127.0.0.1" and str(port["published"]) == str(port["target"])
    assert not _networks(computer) & {"latet-front", "latet-back"}


def test_exact_server_profile(services):
    assert {name for name, service in services.items() if "server" in service.get("profiles", [])} == SERVER
    for name in INSTANCES:
        assert services[name]["command"] == ["server", "run"]
        assert services[name]["healthcheck"]["test"] == ["CMD", "latet", "server", "ready"]


def test_private_back_network_excludes_caddy(compose, services):
    assert compose["networks"]["latet-back"].get("internal", False) is True
    assert _networks(services["caddy"]) == {"latet-front"}
    assert _networks(services["postgres"]) == {"latet-back"}
    assert _networks(services["valkey"]) == {"latet-back"}
    assert {name for name, service in services.items() if "latet-back" in _networks(service)} == {
        "postgres",
        "valkey",
        *INSTANCES,
    }
    for name in INSTANCES:
        assert _networks(services[name]) == {"latet-front", "latet-back"}
        env = services[name]["environment"]
        assert str(env["LATET_SERVER_DATABASE_PRIVATE_NETWORK"]) == "1"
        assert str(env["LATET_SERVER_VALKEY_PRIVATE_NETWORK"]) == "1"


def test_only_caddy_publishes_server_tls(services):
    for name in SERVER - {"caddy"}:
        assert not services[name].get("ports"), name
    ports = services["caddy"]["ports"]
    assert len(ports) == 1
    if isinstance(ports[0], str):
        assert ports == ["443:443"]
    else:
        assert str(ports[0]["published"]) == "443" and ports[0]["target"] == 443
        assert ports[0].get("protocol", "tcp") == "tcp"
    for name in SERVER:
        assert services[name].get("network_mode") != "host"
        assert not services[name].get("privileged", False)


def test_instances_trust_only_fixed_caddy(compose, services):
    address = services["caddy"]["networks"]["latet-front"]["ipv4_address"]
    assert address == "172.28.0.2"
    allocation = compose["networks"]["latet-front"]["ipam"]["config"]
    assert allocation == [{"subnet": "172.28.0.0/24", "ip_range": "172.28.0.128/25"}]
    assert ipaddress.ip_address(address) in ipaddress.ip_network(allocation[0]["subnet"])
    assert ipaddress.ip_address(address) not in ipaddress.ip_network(allocation[0]["ip_range"])
    for name in INSTANCES:
        env = services[name]["environment"]
        assert env["LATET_SERVER_FORWARDED_ALLOW_IPS"] == address
        assert "FORWARDED_ALLOW_IPS" not in env


def test_secrets_stay_in_bind_files(compose, services):
    for name, filename in SECRET_FILES.items():
        secret = compose["secrets"][name]
        assert Path(secret["file"]).name == filename
        assert Path(secret["file"]).parent.name == "secrets"
        assert "environment" not in secret
    for name in INSTANCES:
        instance = services[name]
        env = instance["environment"]
        assert env["LATET_SERVER_DATABASE_URL"] == "postgresql://latet@postgres:5432/latet"
        assert env["LATET_SERVER_VALKEY_URL"] == "redis://valkey:6379/0"
        assert env["LATET_MASTER_KEY_FILE"] == "/run/secrets/latet-master-key"
        assert env["LATET_SERVER_DATABASE_PASSWORD_FILE"] == "/run/secrets/latet-db-password"
        assert env["LATET_SERVER_VALKEY_PASSWORD_FILE"] == "/run/secrets/latet-valkey-password"
        assert env["LATET_SERVER_METRICS_TOKEN_FILE"] == "/run/secrets/latet-metrics-token"
        mounted = {item if isinstance(item, str) else item["source"] for item in instance["secrets"]}
        assert set(SECRET_FILES) <= mounted
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/secrets/latet-db-admin-password"
    for name in SERVER:
        env = services[name].get("environment") or {}
        assert not services[name].get("env_file"), "no implicit environment-file secret fallback"
        for key in env:
            if any(part in key for part in ("PASSWORD", "TOKEN", "MASTER_KEY", "REVIEWER_KEY")):
                assert key.endswith("_FILE"), (name, key)


def test_the_instances_connect_to_postgresql_as_a_role_that_is_no_superuser(compose, services):
    """The image's superuser keeps a password of its own, which no instance mounts; a script run once on a new data
    volume makes the role latet, no superuser, owning the database latet (S-M5: a superuser can run a program in the
    database's container with COPY ... PROGRAM)."""
    postgres = services["postgres"]
    env = postgres["environment"]
    assert "POSTGRES_USER" not in env and "POSTGRES_DB" not in env  # the image's own superuser and database
    assert Path(compose["secrets"]["latet-db-admin-password"]["file"]).parent.name == "secrets"
    mounted = {item if isinstance(item, str) else item["source"] for item in postgres["secrets"]}
    assert mounted == {"latet-db-admin-password", "latet-db-password"}
    for name in INSTANCES:
        given = {item if isinstance(item, str) else item["source"] for item in services[name]["secrets"]}
        assert "latet-db-admin-password" not in given
    volumes = [
        tuple(item.split(":")[:2]) if isinstance(item, str) else (item["source"], item["target"])
        for item in postgres["volumes"]
    ]  # the source's own form ("./deploy/...:/docker-..."), or Docker's rendered one (an absolute source)
    [script] = [source for source, target in volumes if target.startswith("/docker-entrypoint-initdb.d/")]
    assert script.replace("\\", "/").endswith("deploy/postgres-init.sh")
    text = _active_text(ROOT / "deploy" / "postgres-init.sh")
    assert re.search(r"CREATE ROLE latet LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", text)
    assert "CREATE DATABASE latet OWNER latet;" in text
    assert "/run/secrets/latet-db-password" in text and "*[!0-9a-fA-F]*" in text  # hexadecimal, or it stops
    assert re.search(r"psql [^\n]*<<EOSQL", text) and "$password" not in text.split("<<EOSQL")[0].split("psql")[-1]
    assert "pg_isready -h 127.0.0.1" in _command(postgres["healthcheck"], "test")


def test_valkey_memory_and_argv(services):
    valkey = services["valkey"]
    command = _command(valkey)
    health = _command(valkey["healthcheck"], "test")
    assert re.search(r"(?:--)?maxmemory-policy\s+noeviction", command)
    assert re.search(r"(?:--)?maxmemory\s+\S+", command)
    assert "client-output-buffer-limit" not in command
    assert "latet-valkey-password" in command
    if not re.search(r"valkey-server\s+-\s*(?:$|[|;&])", command):
        assert "umask 077" in command
        assert valkey.get("tmpfs")
    for text in (command, health):
        assert not re.search(r"\$\$?\(\s*cat\b", text), "secret command substitution must not enter argv"
        assert not re.search(r"(?:--requirepass|\s-a|--pass)\s", text), "password must not be an argument"
    assert "VALKEYCLI_AUTH" in health or "REDISCLI_AUTH" in health
    assert "latet-valkey-password" in health


def test_logs_are_bounded_and_nonblocking(services):
    for name in SERVER:
        logging = services[name]["logging"]
        assert logging["driver"] in {"local", "json-file"}
        options = logging["options"]
        assert options["mode"] == "non-blocking"
        for key in ("max-buffer-size", "max-size"):
            assert re.fullmatch(r"[1-9][0-9]*[kKmMgG]?[bB]?", str(options[key])), (name, key)
        assert int(options["max-file"]) > 0


def test_instances_have_container_restrictions(services):
    for name in INSTANCES:
        instance = services[name]
        assert instance["read_only"] is True
        assert instance["cap_drop"] == ["ALL"]
        assert not instance.get("cap_add")
        assert any(value in {"no-new-privileges:true", "no-new-privileges=true"} for value in instance["security_opt"])
        assert instance["init"] is True
        assert instance["tmpfs"]
        assert "mem_limit" not in instance
        assert "memory" not in instance.get("deploy", {}).get("resources", {}).get("limits", {})


def test_metrics_remain_on_the_dedicated_loopback_socket(services):
    for name in INSTANCES:
        env = services[name]["environment"]
        assert env.get("LATET_SERVER_METRICS_HOST", "127.0.0.1") == "127.0.0.1"
        assert str(env.get("LATET_SERVER_METRICS_PORT", "7283")) == "7283"
        assert not services[name].get("ports")


def test_caddy_has_tls_streaming_ready_host_and_binary_body_bound():
    text = _active_text(ROOT / "deploy" / "Caddyfile")
    assert "tls /certs/server.crt /certs/server.key" in text
    assert re.search(r"request_body\s*\{\s*max_size\s+32MiB\s*\}", text)
    assert "reverse_proxy latet-server-1:5283 latet-server-2:5283" in text
    assert "health_uri /_latet/ready" in text
    assert re.search(r"health_headers\s*\{\s*Host\s+\{\$LATET_SERVER_HOST\}\s*\}", text)
    assert "flush_interval -1" in text
    site = text.split("{$LATET_SERVER_HOST} {", 1)[1]
    assert not re.search(r"^\s*(?:log|log_append|read_timeout|response_header_timeout)\b", site, re.MULTILINE)
    assert "metrics" not in text.lower()
    assert "7283" not in text


def test_caddy_excludes_http_error_records_containing_credentials():
    text = _active_text(ROOT / "deploy" / "Caddyfile")
    global_options = text.split("{$LATET_SERVER_HOST} {", 1)[0]
    assert re.search(r"log\s+default\s*\{\s*exclude\s+http\.log\.error\s*\}", global_options)


def test_image_exports_server_extra_and_runs_unprivileged():
    text = _active_text(ROOT / "Dockerfile")
    export = next(line for line in text.splitlines() if "uv export" in line)
    assert "--extra server" in export
    assert "--extra ner" in export and "--extra ocr" in export
    assert "--frozen" in export and "--no-dev" in export
    assert re.search(r"^USER 10001:10001$", text, re.MULTILINE)
    assert "THIRD_PARTY_NOTICES.md" in text, "notices must be copied into the image, then checked in the built artifact"


def test_build_context_and_git_exclude_operational_inputs():
    docker = set(_active_text(ROOT / ".dockerignore").split())
    git = set(_active_text(ROOT / ".gitignore").split())
    for directory in ("secrets", "certs"):
        assert f"{directory}/" in docker or f"/{directory}/" in docker
        assert f"{directory}/" in git or f"/{directory}/" in git
    for entry in ("api-key.txt", ".superpowers/", ".claude/", "scratchpad/"):
        assert entry in docker or f"/{entry}" in docker
    assert ".env" in docker and ".env.*" in docker
    assert "!tests/data/technical_kinds.json" in docker


def test_optional_reviewer_key_is_comment_only():
    text = (ROOT / "compose.yaml").read_text("utf-8")
    comments = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    for value in ("latet-reviewer-key", "./secrets/reviewer.key", "LATET_SERVER_REVIEWER_KEY_FILE"):
        assert value in comments
    compose = yaml.safe_load(text)
    assert "latet-reviewer-key" not in compose.get("secrets", {})
    for name in INSTANCES:
        assert "LATET_SERVER_REVIEWER_KEY_FILE" not in compose.get("services", {}).get(name, {}).get("environment", {})
