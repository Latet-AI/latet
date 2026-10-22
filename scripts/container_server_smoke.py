"""Exercise the built server image in an owned Compose project; print no captured output.

Run with an explicit Docker executable and scratch parent, for example in CI:
python3 scripts/container_server_smoke.py --docker /usr/bin/docker --work-dir /tmp
Uses Compose's !override support (2.24.4+). No build, model download or shared test
service lifecycle is performed. Windows callers use the existing outer Job runner
to bound Docker plugin descendants; POSIX children have a separate process group.
An existing standalone Compose can be selected with --compose-executable when
isolated homes prevent Docker's plugin discovery; its Docker config stays empty.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
INSTANCES = ("latet-server-1", "latet-server-2")
FILES = {
    "latet-master-key": "master.key",
    "latet-db-password": "db.password",
    "latet-valkey-password": "valkey.password",
    "latet-metrics-token": "metrics.token",
}
# PostgreSQL's own superuser's password: given to its container alone, never to an instance or the initialization.
DATABASE_ADMIN = ("latet-db-admin-password", "db-admin.password")
ADMIN = "/_latet/server/v1/admin"
CANARY = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
SMOKE_LIMITS = {
    "latet-server-1": (4 * 1024**3, 2),
    "latet-server-2": (4 * 1024**3, 2),
    "postgres": (1024**3, 1),
    "valkey": (1280 * 1024**2, 1),
    "caddy": (256 * 1024**2, 1),
    "fake-upstream": (256 * 1024**2, 1),
}


def child_environment(source: dict[str, str], run: Path) -> dict[str, str]:
    environment = {key: source[key] for key in ("PATH", "SystemRoot", "WINDIR", "PATHEXT") if key in source}
    for name in (
        "HOME",
        "USERPROFILE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "LOCALAPPDATA",
    ):
        environment[name] = str(run / "homes" / name.lower())
    environment.update(TEMP=str(run / "temp"), TMP=str(run / "temp"), TMPDIR=str(run / "temp"))
    environment.update(
        COMPOSE_DISABLE_ENV_FILE="1", DOCKER_CONFIG=str(run / "docker-config"), LATET_SERVER_HOST="localhost"
    )
    if os.name == "nt":
        home = Path(environment["USERPROFILE"])
        environment.update(HOMEDRIVE=home.drive, HOMEPATH=str(home)[len(home.drive) :])
    return environment


def compose_command(
    docker: str,
    root: Path,
    run: Path,
    project: str,
    *arguments: str,
    compose_executable: str | None = None,
) -> list[str]:
    prefix = [compose_executable] if compose_executable else [docker, "--config", str(run / "docker-config"), "compose"]
    return [
        *prefix,
        "--file",
        str(root / "compose.yaml"),
        "--file",
        str(run / "override.yaml"),
        "--project-directory",
        str(root),
        "--env-file",
        str(run / "compose-empty.env"),
        "--project-name",
        project,
        "--profile",
        "server",
        *arguments,
    ]


def fake_provider_healthcheck(port: int = 8000) -> dict[str, Any]:
    probe = (
        "import json, urllib.request; "
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        f"response = opener.open('http://127.0.0.1:{port}/', timeout=2); "
        "data = response.read(1025); assert len(data) <= 1024 and json.loads(data) == {}"
    )
    return {
        "test": ["CMD", "python", "-c", probe],
        "interval": "2s",
        "timeout": "3s",
        "start_period": "5s",
        "retries": 30,
    }


def initialization_command(
    docker: str,
    image: str,
    project: str,
    network: str,
    environment: dict[str, str],
    files: dict[str, Path],
) -> list[str]:
    if environment.get("LATET_MASTER_KEY_FILE") != "/run/secrets/latet-master-key" or "latet-master-key" not in files:
        raise ValueError("initialization requires the pre-created master-key file")
    for key in environment:
        if any(part in key for part in ("PASSWORD", "TOKEN", "MASTER_KEY", "REVIEWER_KEY")) and not key.endswith(
            "_FILE"
        ):
            raise ValueError("initialization accepts secret files only")
    command = [
        docker,
        "create",
        "--name",
        project + "-init",
        "--label",
        "com.docker.compose.project=" + project,
        "--log-driver",
        "none",
        "--network",
        network,
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "2",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]
    for name, path in files.items():
        command += ["--mount", f"type=bind,source={path},target=/run/secrets/{name},readonly"]
    for key, value in environment.items():
        command += ["--env", f"{key}={value}"]
    return [*command, image, "server", "init"]


def folder_owner(directory: Path) -> str:
    """The folder's owner as Docker's --user takes it: `latet server certificate` writes its key only into a folder of
    its own user that nobody else can write, so the one-shot runs as that user, with no capability past it."""
    found = directory.stat()
    return f"{found.st_uid}:{found.st_gid}"


def certificate_command(docker: str, image: str, project: str, directory: Path, url: str, *, user: str) -> list[str]:
    return [
        docker,
        "run",
        "--name",
        project + "-cert",
        "--label",
        "com.docker.compose.project=" + project,
        "--rm",
        "--network",
        "none",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        user,
        "--entrypoint",
        "latet",
        "--mount",
        f"type=bind,source={directory},target=/certs",
        image,
        "server",
        "certificate",
        "--make",
        "--url",
        url,
        "--out",
        "/certs",
    ]


def administration_token(output: bytes) -> str:
    text = output.decode("utf-8")
    found = re.findall(
        r"^An administration token \([^\r\n]+\), shown once:[^\r\n]*\r?\n  (latet_adm_[A-Za-z0-9_-]+)\r?$",
        text,
        re.MULTILINE,
    )
    if len(found) != 1 or "No master key:" in text or "A new master key is in " in text:
        raise ValueError("initialization did not use the existing key and issue one token")
    return found[0]


def require_owned_resources(resources: list[dict[str, Any]], project: str) -> None:
    for resource in resources:
        labels = resource.get("Config", {}).get("Labels") or resource.get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            raise RuntimeError("container smoke cleanup refused a foreign resource")


def verify_inspection(
    records: dict[str, Any],
    rendered: dict[str, Any],
    networks: dict[str, Any],
) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise RuntimeError("container smoke native inspection failed")

    front, back = (rendered["networks"][name]["name"] for name in ("latet-front", "latet-back"))
    require(set(records) == set(SMOKE_LIMITS))
    require(networks[back]["Internal"] is True and networks[front]["Internal"] is False)
    proxy = rendered["services"]["caddy"]["networks"]["latet-front"]["ipv4_address"]
    for name, record in records.items():
        host, configuration, actual = record["HostConfig"], record["Config"], record["NetworkSettings"]
        expected = {front, back} if name in INSTANCES else ({back} if name in {"postgres", "valkey"} else {front})
        require(set(actual["Networks"]) == expected)
        require(not host.get("Privileged", False))
        memory, cpus = SMOKE_LIMITS[name]
        require(0 < host["Memory"] <= memory and 0 < host["NanoCpus"] <= cpus * 1000000000)
        published = {key: value for key, value in (actual.get("Ports") or {}).items() if value}
        if name == "caddy":
            port = rendered["services"]["caddy"]["ports"][0]
            require(published == {"443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port["published"])}]})
            require(actual["Networks"][front]["IPAddress"] == proxy)
        else:
            require(not published)
        logging = host["LogConfig"]
        require(logging["Type"] in {"json-file", "local"})
        options = logging["Config"]
        require(options["mode"] == "non-blocking" and int(options["max-file"]) > 0)
        for key in ("max-size", "max-buffer-size"):
            require(re.fullmatch(r"[1-9][0-9]*[kKmMgG]?[bB]?", str(options[key])) is not None)
        if name in INSTANCES:
            require(configuration["User"] == "10001:10001")
            require(host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"] and not host.get("CapAdd"))
            require(any(value in {"no-new-privileges:true", "no-new-privileges=true"} for value in host["SecurityOpt"]))
            require("/tmp" in host["Tmpfs"])
            environment = dict(value.split("=", 1) for value in configuration["Env"])
            require(
                environment["LATET_SERVER_FORWARDED_ALLOW_IPS"] == proxy and "FORWARDED_ALLOW_IPS" not in environment
            )
            require(
                environment["LATET_SERVER_METRICS_HOST"] == "127.0.0.1"
                and environment["LATET_SERVER_METRICS_PORT"] == "7283"
            )


def verify_valkey(
    configuration: dict[str, str], unauthenticated: bytes, help_output: bytes, auth_variable: str
) -> None:
    pubsub = configuration["client-output-buffer-limit"].split()
    index = pubsub.index("pubsub")
    if not (
        configuration["maxmemory-policy"] == "noeviction"
        and 0 < int(configuration["maxmemory"]) <= 1024**3
        and pubsub[index + 1 : index + 4] == ["33554432", "8388608", "60"]
        and b"NOAUTH" in unauthenticated
        and b"PONG" not in unauthenticated
        and auth_variable in {"VALKEYCLI_AUTH", "REDISCLI_AUTH"}
        and auth_variable.encode() in help_output
    ):
        raise RuntimeError("container smoke native Valkey checks failed")


def verify_private_outputs(outputs: dict[str, bytes], secret_values: list[str]) -> None:
    if any(value and value.encode() in output for value in secret_values for output in outputs.values()):
        raise RuntimeError("container smoke found a generated secret in ordinary process output")


def verify_instance_coverage(before: dict[str, bytes], after: dict[str, bytes]) -> None:
    def successes(text: bytes) -> int:
        found = re.findall(rb'^latet_http_requests_total\{kind="agent",code="200"\} ([0-9]+)$', text, re.MULTILINE)
        if len(found) > 1:
            raise RuntimeError("container smoke received ambiguous instance metrics")
        return int(found[0]) if found else 0

    if set(before) != set(INSTANCES) or set(after) != set(INSTANCES):
        raise RuntimeError("container smoke lacks independent instance coverage")
    if any(successes(after[name]) - successes(before[name]) != 1 for name in INSTANCES):
        raise RuntimeError("container smoke canaries did not reach both instances")


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL to the command's process group (POSIX). A group already gone is no failure: ESRCH, or on macOS EPERM for
    a group whose processes have all ended and wait to be reaped (the command ending just before), which the wait then
    reaps; a command that has not ended within it raises."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        process.wait(timeout=2)


def captured(
    command: list[str],
    *,
    timeout: float = 30,
    max_bytes: int = 2 * 1024 * 1024,
    environment: dict[str, str] | None = None,
    receipt: Path | None = None,
) -> bytes:
    """Bound combined output and time; never attach credentials or child errors to an exception."""
    with contextlib.ExitStack() as handles:
        output_file = metadata_file = None
        if receipt is not None:
            # Reserve both private files before the child starts. Never replace prior evidence or follow its links.
            output_file = handles.enter_context(
                os.fdopen(os.open(receipt.with_suffix(".stdout"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
            )
            metadata_file = handles.enter_context(
                os.fdopen(os.open(receipt.with_suffix(".json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
            )
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        assert process.stdout is not None
        output, overflow, read_failed = bytearray(), threading.Event(), threading.Event()

        def read() -> None:
            assert process.stdout is not None
            try:
                while block := process.stdout.read1(8192):
                    if len(output) + len(block) > max_bytes:
                        overflow.set()
                        return
                    output.extend(block)
            except OSError:
                read_failed.set()

        reader = threading.Thread(target=read, daemon=True)
        deadline = time.monotonic() + timeout
        reader.start()
        try:
            while process.poll() is None and not overflow.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            if process.poll() is None:
                if os.name != "nt":
                    _kill_group(process)
                else:
                    process.kill()
            process.wait(timeout=2)
            reader.join(timeout=1)
            if reader.is_alive() and os.name != "nt":
                _kill_group(process)
                reader.join(timeout=1)
        timed_out = time.monotonic() >= deadline
        if output_file is not None and metadata_file is not None:
            output_file.write(output)
            metadata_file.write(
                json.dumps(
                    {
                        "argv": command,
                        "returncode": process.returncode,
                        "captured_bytes": len(output),
                        "output_limit_bytes": max_bytes,
                        "timed_out": timed_out,
                        "overflow": overflow.is_set(),
                        "read_failed": read_failed.is_set(),
                        "reader_reaped": not reader.is_alive(),
                    },
                    indent=2,
                ).encode()
            )
        if not reader.is_alive():
            process.stdout.close()
        if reader.is_alive() or overflow.is_set() or read_failed.is_set() or process.returncode or timed_out:
            raise RuntimeError(
                f"container smoke command {step(command)} failed or exceeded its bound (exit {process.returncode}, "
                f"timed out {timed_out}, over its output bound {overflow.is_set()})"
            )
        return bytes(output)


def step(command: list[str]) -> str:
    """The step a command is, in words that hold no value it was given: Docker's or Compose's subcommand, and for an
    exec its service and the command it runs there, never the paths, names or variables around them."""
    words = command[1:]
    if "--config" in words[:1]:
        words = words[2:]
    if "compose" in words[:1] or "--profile" in words:
        rest = words[words.index("--profile") + 2 :] if "--profile" in words else words[1:]
        if rest[:1] == ["exec"]:
            service = [word for word in rest[1:] if not word.startswith("-")][:2]
            return "compose exec " + " ".join(service)
        return "compose " + (rest[0] if rest else "")
    return words[0] if words else ""


def request(
    port: int,
    context: ssl.SSLContext,
    method: str,
    path: str,
    token: str = "",
    body: Any = None,
    *,
    timeout: float = 5,
) -> Any:
    # A direct connection has no environment proxy or redirect machinery.
    connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=timeout)
    headers = {"content-type": "application/json"}
    if token:
        headers["x-latet-admin"] = token
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024 + 1)
        if response.status not in (200, 201) or len(data) > 2 * 1024 * 1024:
            raise RuntimeError("container smoke HTTP assertion failed")
        return json.loads(data)
    finally:
        connection.close()


def run_smoke(
    docker: str,
    image: str,
    parent: Path,
    compose_executable: str | None = None,
    *,
    retain_evidence: bool = False,
) -> None:
    run = Path(tempfile.mkdtemp(prefix="latet-container-smoke-", dir=parent)).resolve()
    if retain_evidence:
        print(run, flush=True)
    summary = {
        "instance_readiness": False,
        "proxy_readiness": False,
        "canary_restored": False,
        "canary_masked": False,
        "both_instances_canary": False,
        "cleanup_complete": False,
        "stage8_native": False,
        "caddy_byte_boundary": False,
        "websocket_and_stream": False,
        "eop31_consumer_link": False,
    }
    project = "e0c-t15-" + secrets.token_hex(8)
    environment = child_environment(dict(os.environ), run)
    for key in (
        "HOME",
        "USERPROFILE",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    (run / "docker-config").mkdir()
    (run / "compose-empty.env").touch()
    secret_dir, cert_dir = run / "secrets", run / "certs"
    secret_dir.mkdir(mode=0o700)
    cert_dir.mkdir(mode=0o700)  # written by the certificate one-shot as its owner; Caddy reads it as root
    files = {name: secret_dir / filename for name, filename in FILES.items()}
    generated_secrets = [CANARY]
    for name, path in files.items():
        value = (
            base64.b64encode(secrets.token_bytes(32)).decode() if name == "latet-master-key" else secrets.token_hex(32)
        )
        generated_secrets.append(value)
        path.write_text(value + "\n", encoding="utf-8", newline="\n")
        path.chmod(0o444)
    database_admin = secret_dir / DATABASE_ADMIN[1]
    generated_secrets.append(admin_password := secrets.token_hex(32))
    database_admin.write_text(admin_password + "\n", encoding="utf-8", newline="\n")
    database_admin.chmod(0o444)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Distinct subnets and project names isolate this run from the shared test-service project.
    octet = secrets.randbelow(200) + 20
    address = f"172.29.{octet}."
    environment["LATET_SERVER_URL"] = f"https://localhost:{port}"
    fake = run / "provider.py"
    fake.write_text(
        "from fake_upstream import FakeUpstream\n"
        "from starlette.responses import JSONResponse\nfrom starlette.routing import Route\n"
        "fake = FakeUpstream()\napp = fake.app\n"
        "async def received(request):\n    return JSONResponse({'received': fake.all_received()})\n"
        "app.routes.insert(0, Route('/_smoke/received', received))\n",
        encoding="utf-8",
        newline="\n",
    )
    # JSON is YAML; explicit tagged replacement prevents retaining public ports or the shipped subnet.
    (run / "override.yaml").write_text(
        "services:\n"
        + "".join(
            f"  {name}:\n    image: {json.dumps(image)}\n    mem_limit: {SMOKE_LIMITS[name][0]}\n    cpus: 2\n"
            f"    environment:\n      LATET_SERVER_FORWARDED_ALLOW_IPS: {address}2\n"
            for name in INSTANCES
        )
        + f"  postgres: {{mem_limit: {SMOKE_LIMITS['postgres'][0]}, cpus: 1}}\n"
        + f"  valkey: {{mem_limit: {SMOKE_LIMITS['valkey'][0]}, cpus: 1}}\n"
        + f"  caddy:\n    mem_limit: {SMOKE_LIMITS['caddy'][0]}\n    cpus: 1\n"
        f'    ports: !override ["127.0.0.1:{port}:443"]\n'
        + f"    networks:\n      latet-front:\n        ipv4_address: {address}2\n"
        + "    volumes:\n"
        + f"      - {json.dumps(str(cert_dir) + ':/certs:ro')}\n"
        + "  fake-upstream:\n"
        + f"    image: {json.dumps(image)}\n    profiles: [server]\n"
        f"    mem_limit: {SMOKE_LIMITS['fake-upstream'][0]}\n    cpus: 1\n"
        + "    entrypoint: [python, -m, uvicorn, provider:app, --host, 0.0.0.0, --port, '8000', "
        "--no-access-log, --log-level, warning]\n"
        + f"    healthcheck: {json.dumps(fake_provider_healthcheck())}\n"
        + "    logging: {driver: json-file, options: {mode: non-blocking, max-buffer-size: 4m, "
        'max-size: 10m, max-file: "5"}}\n'
        + "    environment: {PYTHONPATH: /smoke}\n    volumes:\n"
        + f"      - {json.dumps(str(fake) + ':/smoke/provider.py:ro')}\n"
        + f"      - {json.dumps(str(ROOT / 'tests/e2e/fake_upstream.py') + ':/smoke/fake_upstream.py:ro')}\n"
        + f"    networks:\n      latet-front:\n        ipv4_address: {address}3\n"
        + "networks:\n  latet-front:\n    ipam:\n"
        + f"      config: !override [{{subnet: {address}0/24, ip_range: {address}128/25}}]\n"
        + "secrets:\n"
        + "".join(
            f"  {name}:\n    file: {json.dumps(str(path))}\n"
            for name, path in {**files, DATABASE_ADMIN[0]: database_admin}.items()
        ),
        encoding="utf-8",
        newline="\n",
    )
    prefix = [docker, "--config", str(run / "docker-config")]
    receipt_number = 0

    def execute(command: list[str], timeout: float) -> bytes:
        nonlocal receipt_number
        receipt_number += 1
        receipt = run / f"call-{receipt_number:04d}" if retain_evidence else None
        return captured(command, environment=environment, timeout=timeout, receipt=receipt)

    def call(*args: str, timeout: float = 30) -> bytes:
        return execute(prefix + list(args), timeout)

    def compose(*args: str, timeout: float = 30) -> bytes:
        return execute(
            compose_command(docker, ROOT, run, project, *args, compose_executable=compose_executable),
            timeout,
        )

    try:
        certificate = certificate_command(
            docker, image, project, cert_dir, environment["LATET_SERVER_URL"], user=folder_owner(cert_dir)
        )
        call(*certificate[1:])
        rendered = json.loads(compose("config", "--format", "json"))
        startup_deadline = time.monotonic() + 180

        def startup_remaining(maximum: float = 180) -> float:
            remaining = startup_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("container smoke startup exceeded its shared deadline")
            return min(maximum, remaining)

        compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(max(1, int(startup_remaining()))),
            "postgres",
            "valkey",
            timeout=startup_remaining(),
        )
        command = initialization_command(
            docker,
            image,
            project,
            rendered["networks"]["latet-back"]["name"],
            rendered["services"][INSTANCES[0]]["environment"],
            files,
        )
        call(*command[1:], timeout=startup_remaining(30))
        info = json.loads(call("inspect", project + "-init", timeout=startup_remaining(30)))[0]
        if info["HostConfig"]["LogConfig"]["Type"] != "none":
            raise RuntimeError("initialization logging was not disabled")
        admin = administration_token(call("start", "--attach", project + "-init", timeout=startup_remaining(90)))
        generated_secrets.append(admin)
        compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(max(1, int(startup_remaining()))),
            *INSTANCES,
            "caddy",
            "fake-upstream",
            timeout=startup_remaining(),
        )
        for name in INSTANCES:
            compose("exec", "-T", name, "latet", "server", "ready", timeout=startup_remaining(30))
        summary["instance_readiness"] = True
        context = ssl.create_default_context(cafile=str(cert_dir / "server.crt"))
        for attempt in range(60):
            try:
                request(port, context, "GET", "/_latet/ready", timeout=startup_remaining(2))
                break
            except (OSError, RuntimeError):
                if attempt == 59:
                    raise
                time.sleep(min(2, startup_remaining()))
        summary["proxy_readiness"] = True
        shared = request(port, context, "GET", ADMIN + "/shared", admin)
        # Settings alone: the Community Edition shares no protection rule (the canary is a secret under every profile).
        request(
            port,
            context,
            "PUT",
            ADMIN + "/shared",
            admin,
            {
                **shared["settings"],
                "etag": shared["etag"],
                "upstreams": [{"name": "smoke", "url": f"http://{address}3:8000"}],
            },
        )
        key = request(port, context, "POST", ADMIN + "/identities", admin, {"kind": "key", "name": "Container smoke"})
        base = urlsplit(key["base"]).path
        generated_secrets.extend((key["key"], base.rsplit("/", 1)[-1]))
        metrics_reader = (
            "import http.client, sys\nfrom pathlib import Path\n"
            "token = Path('/run/secrets/latet-metrics-token').read_text('utf-8-sig').strip()\n"
            "connection = http.client.HTTPConnection('127.0.0.1', 7283, timeout=3)\n"
            "connection.request('GET', '/_latet/metrics', headers={'Authorization': 'Bearer ' + token})\n"
            "response = connection.getresponse()\ndata = response.read(1048577)\n"
            "if response.status != 200 or len(data) > 1048576: raise SystemExit('metrics witness refused')\n"
            "connection.close()\nsys.stdout.buffer.write(data)\n"
        )
        before = {name: compose("exec", "-T", name, "python", "-c", metrics_reader, timeout=10) for name in INSTANCES}
        # The identity's own token goes in x-latet-token, never the administrator header.
        for _ in range(2):
            connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=10)
            try:
                connection.request(
                    "POST",
                    base + "/smoke/v1/messages",
                    json.dumps({"model": "smoke", "messages": [{"role": "user", "content": "key " + CANARY}]}),
                    {"content-type": "application/json", "x-latet-token": key["key"]},
                )
                response = connection.getresponse()
                body = response.read(1024 * 1024)
                if response.status != 200 or CANARY.encode() not in body:
                    raise RuntimeError("container smoke did not restore its canary")
            finally:
                connection.close()
        summary["canary_restored"] = True
        # Metrics.scrape serves a rendering again for one second; obtain fresh independent counters.
        time.sleep(1.1)
        after = {name: compose("exec", "-T", name, "python", "-c", metrics_reader, timeout=10) for name in INSTANCES}
        verify_instance_coverage(before, after)
        summary["both_instances_canary"] = True
        received = json.loads(
            compose(
                "exec",
                "-T",
                "fake-upstream",
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen("
                "'http://127.0.0.1:8000/_smoke/received', timeout=5).read().decode())",
            )
        )
        if not received["received"] or CANARY in received["received"]:
            raise RuntimeError("container smoke did not mask its canary")
        summary["canary_masked"] = True
        # These probes inspect this same bringup before teardown. Their bounded output stays private.
        identifiers = {name: compose("ps", "--quiet", name).decode().strip() for name in SMOKE_LIMITS}
        if not all(identifiers.values()):
            raise RuntimeError("container smoke could not identify its own services")
        records = {name: json.loads(call("inspect", identifier))[0] for name, identifier in identifiers.items()}
        networks = {
            value["name"]: json.loads(call("network", "inspect", value["name"]))[0]
            for name, value in rendered["networks"].items()
            if name in {"latet-front", "latet-back"}
        }
        verify_inspection(records, rendered, networks)
        metrics_probe = (
            "import socket, sys\n"
            "with socket.create_connection(('127.0.0.1', 7283), timeout=2): pass\n"
            "for address in sys.argv[1:]:\n"
            "    try:\n        connection = socket.create_connection((address, 7283), timeout=1)\n"
            "    except OSError:\n        continue\n"
            "    connection.close()\n    raise SystemExit('metrics reachable off loopback')\n"
        )
        for name in INSTANCES:
            if compose("exec", "-T", name, "id", "-u").strip() != b"10001":
                raise RuntimeError("container smoke instance does not run as its own unprivileged user")
            addresses = [value["IPAddress"] for value in records[name]["NetworkSettings"]["Networks"].values()]
            compose("exec", "-T", name, "python", "-c", metrics_probe, *addresses)
        configuration = compose(
            "exec",
            "-T",
            "valkey",
            "sh",
            "-ec",
            'password=; IFS= read -r password < /run/secrets/latet-valkey-password || test -n "$password"; '
            'VALKEYCLI_AUTH="$password" valkey-cli --raw CONFIG GET '
            "maxmemory maxmemory-policy client-output-buffer-limit",
        )
        lines = configuration.decode().splitlines()
        unauthenticated = compose("exec", "-T", "valkey", "sh", "-c", "valkey-cli ping; :")
        help_output = compose("exec", "-T", "valkey", "valkey-cli", "--help")
        verify_valkey(dict(zip(lines[::2], lines[1::2], strict=True)), unauthenticated, help_output, "VALKEYCLI_AUTH")
        outputs = {}
        for name, identifier in identifiers.items():
            outputs[name + "-logs"] = call("logs", identifier)
            outputs[name + "-argv"] = call("top", identifier, "-eo", "pid,args")
        verify_private_outputs(outputs, generated_secrets)
        summary["stage8_native"] = True
    finally:
        # Inspect actual labels before removal; a collision must not widen cleanup to another project.
        try:
            cleanup_deadline = time.monotonic() + 60

            def cleanup_call(*args: str) -> bytes:
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("container smoke cleanup exceeded its deadline")
                return call(*args, timeout=min(10, remaining))

            for kind in ("container", "network", "volume"):
                listing = [kind, "ls", "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.ID}}"]
                if kind == "container":
                    listing += ["--all"]
                elif kind == "volume":
                    listing[-1] = "{{.Name}}"
                identifiers = cleanup_call(*listing).decode().split()
                if identifiers:
                    require_owned_resources(json.loads(cleanup_call(kind, "inspect", *identifiers)), project)
            # A killed Docker client does not prove either daemon-side one-shot has exited or auto-removed.
            for suffix in ("init", "cert"):
                oneshots = (
                    cleanup_call(
                        "container",
                        "ls",
                        "--all",
                        "--filter",
                        "name=^/" + project + "-" + suffix + "$",
                        "--format",
                        "{{.ID}}",
                    )
                    .decode()
                    .split()
                )
                if oneshots:
                    require_owned_resources(json.loads(cleanup_call("container", "inspect", *oneshots)), project)
                    cleanup_call("rm", "--force", "--volumes", *oneshots)
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("container smoke cleanup exceeded its deadline")
            compose("down", "--volumes", "--remove-orphans", "--timeout", "10", timeout=remaining)
            summary["cleanup_complete"] = True
        finally:
            if run.parent != parent.resolve() or not run.name.startswith("latet-container-smoke-"):
                raise RuntimeError("container smoke scratch ownership changed")
            if retain_evidence:
                descriptor = os.open(run / "summary.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(json.dumps(summary, indent=2).encode())
            elif summary["cleanup_complete"]:
                shutil.rmtree(run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--image", default="latet:ci")
    parser.add_argument("--compose-executable", type=Path)
    parser.add_argument(
        "--retain-evidence", action="store_true", help="Keep bounded private receipts in the owned run directory."
    )
    args = parser.parse_args()
    if not args.docker.is_absolute() or not args.docker.is_file() or not args.work_dir.is_absolute():
        raise SystemExit("container smoke requires an explicit Docker executable and scratch parent")
    if args.compose_executable and (not args.compose_executable.is_absolute() or not args.compose_executable.is_file()):
        raise SystemExit("container smoke requires an absolute existing Compose executable")
    try:
        run_smoke(
            str(args.docker),
            args.image,
            args.work_dir.resolve(),
            str(args.compose_executable) if args.compose_executable else None,
            retain_evidence=args.retain_evidence,
        )
    except Exception as exc:
        if args.retain_evidence:
            raise SystemExit(1) from None
        # The script's own failures say a step in fixed words; anything else is named by its type alone.
        said = (
            str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("container smoke") else type(exc).__name__
        )
        raise SystemExit(
            f"Server container smoke failed: {said}. Captured credentials and child output are private."
        ) from None
    if not args.retain_evidence:
        print("Server container smoke passed.")


if __name__ == "__main__":
    main()
