"""Task 7 consumer contracts, prepared statically; runtime evidence awaits the coordinator's slot.

Tests in this file address existing commands. New identity-send/key-file capabilities live separately so an unknown
command/option is never mistaken for a security RED. All server traffic uses the existing loopback HTTPS fixture.
"""

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse, Response

from latet.config import data_dir
from latet.doctor import checks
from latet.server import connection as connections
from latet.server.client import ServerClient
from latet.server.tokens import route_key

from .conftest import connect_to, run_cli
from .fake_latet_server import EMILY


@pytest.fixture
def connected(latet_server, quiet):
    # latet_server depends on computer: quiet's removed agent overrides still resolve into its isolated home.
    done = connect_to(latet_server)
    assert done.exit_code == 0, done.output
    latet_server.asked.clear()
    return latet_server


@pytest.fixture
def safe_doctor(monkeypatch):
    # Keep run_all and the CLI formatter real; unrelated local checks must not start OCR, probe ports or read agents.
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


def _controlled(done):
    assert "No such command" not in done.output
    assert "No such option" not in done.output
    assert "Traceback" not in done.output
    assert done.exception is None or isinstance(done.exception, SystemExit), repr(done.exception)


def _private_output(done, *secrets):
    for secret in secrets:
        assert secret not in done.output
    assert "Traceback" not in done.output


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_connected_diagnostic_authenticates_with_server(connected, safe_doctor, command):
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert connected.url in done.output
    assert "Daemon:" not in done.output
    assert connected.asked == [("GET", "whoami")]


@pytest.mark.parametrize(("command", "exit_code"), [("status", 3), ("doctor", 1)])
@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_connected_diagnostic_refusal_is_nonzero_and_bounded(connected, safe_doctor, command, exit_code, status):
    connected.answers["whoami"] = JSONResponse(
        {"error": "controlled refusal", "details": ["fixed detail"]},
        status_code=status,
        headers={"retry-after": "0" if status == 503 else "60"},
    )
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == exit_code, done.output
    if status == 401:  # R7: why the server may refuse this computer's token, and what to do, in place of its word
        said = " ".join(done.output.split())
        assert "no longer accepts this computer's token (revoked, or your identity disabled)" in said
        assert "`latet disconnect`, then `latet connect" in said and "new code from an administrator" in said
    else:
        assert "controlled refusal" in done.output and "fixed detail" in done.output
    assert connected.asked == [("GET", "whoami")] * (2 if status == 503 else 1)
    if status == 429:
        assert "60" in done.output


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_connected_diagnostic_retries_one_busy_answer(connected, safe_doctor, command):
    connected.answers["whoami"] = [JSONResponse({"error": "busy"}, status_code=503, headers={"retry-after": "0"})]
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert connected.asked == [("GET", "whoami"), ("GET", "whoami")]


@pytest.mark.parametrize(("command", "exit_code"), [("status", 3), ("doctor", 1)])
@pytest.mark.parametrize(
    "body",
    [
        {**EMILY, "id": "u_otherother", "version": "test"},
        {**EMILY, "kind": "key", "version": "test"},
        {**EMILY},
        {**EMILY, "version": {"private": "nested-version-sentinel"}},
        {**EMILY, "version": ["nested-version-sentinel"]},
        {**EMILY, "version": True},
        {**EMILY, "version": "x" * 100_001},
        [],
    ],
    ids=["wrong-identity", "api-key", "missing-version", "object", "list", "boolean", "oversized", "array"],
)
def test_connected_diagnostic_rejects_invalid_success(connected, safe_doctor, command, exit_code, body):
    connected.answers["whoami"] = JSONResponse(body)
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == exit_code, done.output
    _private_output(done, "nested-version-sentinel", "x" * 1000)
    assert connected.asked == [("GET", "whoami")]
    assert "token works" not in done.output and "Server token: accepted" not in done.output


@pytest.mark.parametrize(("command", "exit_code"), [("status", 3), ("doctor", 1)])
def test_connected_diagnostic_rejects_non_json_success(connected, safe_doctor, command, exit_code):
    connected.answers["whoami"] = Response("private-body-sentinel", media_type="application/json")
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == exit_code, done.output
    _private_output(done, "private-body-sentinel")


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_connected_diagnostic_escapes_terminal_controls(connected, safe_doctor, command):
    controls = "\x1b\x85" + chr(0x2028) + chr(0x2029) + chr(0x202E)
    connection = connections.load_connection()
    assert connection is not None
    connections.save_connection(connection.model_copy(update={"user_name": "cached" + controls}))
    connected.answers["whoami"] = JSONResponse({**EMILY, "name": "name" + controls, "version": "v" + controls})
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code in (0, 1, 3), done.output  # bounded rejection is also safe
    assert connected.asked == [("GET", "whoami")]
    for control in controls:
        assert control not in done.output


@pytest.mark.parametrize(("command", "exit_code"), [("status", 3), ("doctor", 1)])
def test_missing_personal_token_never_constructs_network_client(
    connected, safe_doctor, monkeypatch, command, exit_code
):
    used = []
    monkeypatch.setattr(connections, "load_server_token", lambda data, connection, *, _selected=None: None)
    monkeypatch.setattr(ServerClient, "__init__", lambda *args, **kwargs: used.append("client"))
    done = run_cli(command)
    _controlled(done)
    assert used == [] and connected.asked == []
    assert done.exit_code == exit_code, done.output
    assert "token" in done.output.lower()


@pytest.mark.parametrize(("command", "exit_code"), [("status", 3), ("doctor", 1)])
def test_failed_folder_trust_precedes_every_sensitive_consumer(connected, safe_doctor, monkeypatch, command, exit_code):
    from latet.engine.reviewer import certificate
    from latet.integrations import base

    used = []

    def forbidden(name):
        def called(*args, **kwargs):
            used.append(name)
            raise RuntimeError("sensitive-read-sentinel")

        return called

    monkeypatch.setattr(
        connections, "connection_folder_problem", lambda: "unsafe connection folder; use a private folder"
    )
    monkeypatch.setattr(connections, "load_server_token", forbidden("token"))
    monkeypatch.setattr(connections, "server_certificate_file", forbidden("certificate"))
    monkeypatch.setattr(connections, "_server_pem", forbidden("certificate-snapshot"))
    monkeypatch.setattr(certificate, "load", forbidden("certificate-content"))
    monkeypatch.setattr(base, "load_records", forbidden("records"))
    monkeypatch.setattr(ServerClient, "__init__", forbidden("client"))
    monkeypatch.setattr(checks, "check_agents", forbidden("agents"))
    done = run_cli(command)
    _controlled(done)
    assert used == [] and connected.asked == []
    assert done.exit_code == exit_code, done.output
    assert "connection" in done.output.lower() and "private" in done.output.lower()
    _private_output(done, "sensitive-read-sentinel")


@pytest.mark.parametrize(
    "command", ["status", "doctor", "dashboard", "env", "config get profile", "rules list", "identity show"]
)
def test_malformed_connection_is_never_absent(computer, quiet, safe_doctor, monkeypatch, command):
    path = connections.connection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("url: []\nuser: invalid\nuser_name: malformed-sentinel\n", encoding="utf-8")
    used = []
    monkeypatch.setattr(ServerClient, "__init__", lambda *args, **kwargs: used.append("client"))
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda: used.append("daemon"))
    done = run_cli(*command.split())
    _controlled(done)
    assert done.exit_code != 0, done.output
    if command in ("status", "doctor"):
        assert done.exit_code == (3 if command == "status" else 1)
    assert "connection" in done.output.lower()
    _private_output(done, "malformed-sentinel")
    assert used == []


@pytest.mark.parametrize("command", ["status", "doctor"])
def test_unexpected_connected_errors_are_type_only(connected, safe_doctor, monkeypatch, command):
    def broken(self):
        raise RuntimeError("private-exception-sentinel")

    monkeypatch.setattr(ServerClient, "whoami", broken)
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == (3 if command == "status" else 1), done.output
    _private_output(done, "private-exception-sentinel")
    assert "RuntimeError" in done.output


@pytest.mark.parametrize("command", ["status", "doctor"])
@pytest.mark.parametrize(
    "content",
    [None, b"private-pem-sentinel", b"\xff\xfe", b"-----BEGIN CERTIFICATE-----\n!\n-----END CERTIFICATE-----"],
)
def test_unavailable_pin_remains_a_failure_with_certificate_diagnostic(
    connected, safe_doctor, monkeypatch, command, content
):
    # This is the existing public pin store, derived from the test server's fingerprint, never a user certificate.
    constructed = []
    init = ServerClient.__init__

    def observed(self, *args, **kwargs):
        constructed.append(True)
        init(self, *args, **kwargs)

    monkeypatch.setattr(ServerClient, "__init__", observed)
    path = data_dir() / "certificates" / f"{connected.fingerprint.removeprefix('sha256:')}.pem"
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    done = run_cli(command)
    _controlled(done)
    assert done.exit_code == (3 if command == "status" else 1), done.output
    assert "certificate" in done.output.lower()
    _private_output(done, "private-pem-sentinel", "BEGIN CERTIFICATE")
    assert constructed == [] and connected.asked == []


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_dashboard_refusal_reports_details_without_opening_browser(connected, monkeypatch, status):
    opened = []
    connected.answers["login-code"] = JSONResponse(
        {"error": "controlled dashboard refusal", "details": ["fixed detail"]},
        status_code=status,
        headers={"retry-after": "0" if status == 503 else "60"},
    )
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    done = run_cli("dashboard")
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert "controlled dashboard refusal" in done.output and "fixed detail" in done.output
    assert opened == []
    assert connected.asked == [("POST", "login-code")] * (2 if status == 503 else 1)
    if status == 429:
        assert "60" in done.output


@pytest.mark.parametrize(
    "command",
    [
        "config get profile",
        "config path",
        "config validate",
        "rules list",
        "rules export",
        "llm status",
        "project show",
        "identity show",
        "reload",
    ],
)
def test_connected_settings_refuse_before_local_work(connected, monkeypatch, command):
    used = []
    monkeypatch.setattr("latet.cli.admin._reload_answer", lambda: used.append("reload"))
    done = run_cli(*command.split())
    _controlled(done)
    assert done.exit_code == 2, done.output
    assert "latet dashboard" in done.output and connected.url in done.output
    assert used == [] and connected.asked == []


def test_file_test_and_schema_are_the_exact_existing_exceptions(connected, tmp_path):
    rule = tmp_path / "badge.yaml"
    rule.write_text("name: badge\ntype: BADGE\npattern: 'AB-[0-9]{6}'\n", encoding="utf-8")
    tested = run_cli("rules", "test", str(rule), "--json")
    schema = run_cli("config", "schema")
    _controlled(tested)
    _controlled(schema)
    assert tested.exit_code == schema.exit_code == 0
    assert "recognizers" in json.loads(tested.stdout)
    assert "properties" in json.loads(schema.stdout)
    assert connected.asked == []


def test_dashboard_explicit_print_is_the_only_link_output(connected, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    done = run_cli("dashboard", "--print-url")
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert f"{connected.url}/_latet/ui/login?code=" in done.stdout
    assert opened == [] and connected.asked == [("POST", "login-code")]


@pytest.mark.parametrize("raises", [False, True])
def test_dashboard_browser_failure_never_prints_link(connected, monkeypatch, raises):
    code = "browser-code-sentinel"
    connected.answers["login-code"] = JSONResponse({"code": code, "url": "https://ignored.invalid/" + code})
    opened = []

    def browser(url):
        opened.append(url)
        if raises:
            raise OSError(url)
        return False

    monkeypatch.setattr("webbrowser.open", browser)
    done = run_cli("dashboard")
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert opened == [f"{connected.url}/_latet/ui/login?code={code}"]
    _private_output(done, code, "https://ignored.invalid")
    assert "--print-url" in done.output


def test_dashboard_open_success_acknowledges_without_code(connected, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    done = run_cli("dashboard")
    _controlled(done)
    assert done.exit_code == 0, done.output
    assert len(opened) == 1 and "?code=" in opened[0]
    assert "?code=" not in done.output and connected.url in done.output


def test_local_status_keeps_existing_behavior(computer, quiet, monkeypatch):
    monkeypatch.setattr("latet.service.daemon.status", lambda: SimpleNamespace(running=False))
    done = run_cli("status")
    assert done.exit_code == 3 and done.output.strip() == "not running"


def test_connected_claude_environment_withholds_personal_token(connected):
    connection = connections.load_connection()
    assert connection is not None
    token = connections.load_server_token(data_dir(), connection)
    assert token is not None
    done = run_cli("env", "--agent", "claude", "--shell", "bash")
    assert done.exit_code == 0, done.output
    _private_output(done, token)
    assert "LATET_BRIDGE_TOKEN" not in done.stdout
    assert route_key(token) in done.stdout and "secret" in done.stderr


@pytest.mark.parametrize("raises", [False, True])
def test_local_browser_failure_has_the_same_no_implicit_link_boundary(computer, quiet, monkeypatch, raises):
    url = "http://127.0.0.1:19421/_latet/ui/login?code=local-code-sentinel"
    opened = []
    requests = []
    monkeypatch.setattr("latet.cli.main._ensure_daemon", lambda: None)
    monkeypatch.setattr("latet.storage.keys.load_bridge_token", lambda data: "local-token-sentinel")

    def local_open(request, timeout):
        requests.append(request)
        return BytesIO(json.dumps({"url": url}).encode())

    def browser(link):
        opened.append(link)
        if raises:
            raise OSError(link)
        return False

    monkeypatch.setattr("latet.service.daemon.local_urlopen", local_open)
    monkeypatch.setattr("webbrowser.open", browser)
    done = run_cli("dashboard")
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert opened == [url]
    assert len(requests) == 1 and requests[0].get_method() == "POST"
    assert requests[0].get_header("X-latet-token") == "local-token-sentinel"
    _private_output(done, "local-code-sentinel", "local-token-sentinel")
    assert "--print-url" in done.output


@pytest.mark.parametrize(("shell", "agent"), [("fish", "generic"), ("bash", "other")])
def test_existing_env_rejects_unknown_shell_or_agent(computer, quiet, shell, agent):
    done = run_cli("env", "--shell", shell, "--agent", agent)
    _controlled(done)
    assert done.exit_code != 0, done.output
    assert "OPENAI_BASE_URL" not in done.stdout
