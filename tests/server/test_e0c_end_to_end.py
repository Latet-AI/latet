"""E0c's CLI, computer and real two-instance server contract, with isolated PostgreSQL and Valkey.

The in-process transport replaces only the client socket. Both operations apps, credentials, masking, propagation,
administration and sealed stores are real. Installed-agent TLS/plugin transport has a separate Task 6/20 gate.
"""

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from psycopg import sql
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.config import Config, Destination, data_dir
from latet.gateway.routes import Upstream
from latet.server import client as client_module
from latet.server.app import open_server
from latet.server.connection import load_connection, load_server_key, load_server_token
from latet.server.db import Database, migrate
from latet.server.operations import server_app
from latet.server.tokens import route_key

from ..e2e.fake_upstream import FakeUpstream
from ..unit import conftest as computer_fixtures
from .conftest import dump, valkey_dump
from .support import captured_logs, until
from .test_two_instances import _forms, _held_nothing

pytestmark = pytest.mark.server

# Reuse the established isolated computer and agent environment fixtures in this server test module.
computer = computer_fixtures.computer
quiet = computer_fixtures.quiet

MACHINE = "/_latet/server/v1"
ADMIN = f"{MACHINE}/admin"
API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
EMILY_PAT = "ghp_" + "R7tK2pQ9xZ4mW8vB3nL6hJ1cF5dS0aY2eU4i"
BILLING_PAT = "ghp_" + "Q1wE2rT3yU4iO5pA6sD7fG8hJ9kL0zX1cV2b"
TERM = "Kestrel Analytics"
METRICS_TOKEN = "metrics-e0c-t16-" + "m" * 32
REVIEWER_KEY = "sk-server-e0c-t16-" + "r" * 40


def run_cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def _ask(text):
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def _send(client, secret, text):
    return client.post(
        f"/u/{route_key(secret)}/llm/v1/chat/completions",
        json=_ask(text),
        headers={"x-latet-token": secret, "authorization": "Bearer sk-test"},
    )


def _answer(response):
    assert response.status_code == 200
    return "".join(
        choice.get("delta", {}).get("content") or ""
        for line in response.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line.removeprefix("data: ")).get("choices", [])
    )


def _non_metadata_dump(db):
    """Inspect every cell except the independently checked, explicitly permitted metadata columns."""
    allowed = {
        ("identities", "name"),
        ("identities", "email"),
        ("identities", "external_id"),
        ("tokens", "label"),
        ("admin_tokens", "label"),
    }
    out = bytearray()
    with db.connection() as conn:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (db.schema,)
        ).fetchall()
        for (table,) in tables:
            rows = conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
            assert rows.description is not None
            columns = [column.name for column in rows.description]
            for row in rows:
                for column, value in zip(columns, row, strict=True):
                    if (table, column) in allowed:
                        continue
                    out += value if isinstance(value, bytes | bytearray | memoryview) else str(value).encode()
                    out += b"\n"
    return bytes(out)


def _checked_metadata(db):
    """Permit only the exact identity/label values this test deliberately stored."""
    with db.connection() as conn:
        identities = conn.execute("SELECT kind, name, email, external_id FROM identities").fetchall()
        assert len(identities) == 2
        assert set(identities) == {
            ("person", "Emily Carter", "emily.carter@acme.com", None),
            ("key", "Billing CI", None, None),
        }
        labels = conn.execute("SELECT label FROM tokens").fetchall()
        assert len(labels) == 2 and {row[0] for row in labels} == {"key", "e0c-t16-laptop"}
        labels = {row[0] for row in conn.execute("SELECT label FROM admin_tokens")}
        assert "e0c-t16-administrator" in labels
        assert labels <= {"e0c-t16-administrator", "e0c-t16-second-admin"}


@pytest.fixture
def cluster(settings, schema, prefix, computer, quiet, monkeypatch):
    # Current global isolated_homes supplies all seven homes; computer/quiet keep real CLI isolation.
    metrics_file = computer / "e0c-t16-metrics.token"
    metrics_file.write_text(METRICS_TOKEN, "utf-8")
    reviewer_file = computer / "e0c-t16-reviewer.key"
    reviewer_file.write_text(REVIEWER_KEY + "\n", "utf-8")
    configured = settings.model_copy(update={"metrics_token_file": metrics_file, "reviewer_key_file": reviewer_file})
    master_key = configured.master_key_file.read_text("utf-8").strip()
    db = Database(configured.database_url, schema=schema, max_size=1)
    try:
        migrate(db)
    finally:
        db.close()
    with captured_logs() as logs, ExitStack() as owned:
        servers = []
        for _ in range(2):
            server = open_server(configured, schema=schema, prefix=prefix)
            owned.callback(server.close)
            assert server.reviewer_key == REVIEWER_KEY and REVIEWER_KEY not in repr(server)
            servers.append(server)
        first_server, second_server = servers
        upstream = FakeUpstream().start()
        owned.callback(upstream.stop)
        # Builtin provider routes use the owned model too; Config destinations cannot use reserved names.
        monkeypatch.setattr("latet.gateway.app.DEFAULT_UPSTREAMS", (Upstream("openai", "/openai", upstream.url),))
        first_server.shared.set(Config(upstreams=[Destination(name="llm", url=upstream.url)]), actor="test")
        url = first_server.settings.url
        with (
            TestClient(server_app(first_server), base_url=url) as first,
            TestClient(server_app(second_server), base_url=url) as second,
        ):
            route = {"to": first}

            def in_process_http(address, fingerprint, timeout, *, _verify=None):
                return route["to"], False

            monkeypatch.setattr(client_module, "_http", in_process_http)
            admin_token = first_server.admin.issue("e0c-t16-administrator")
            monkeypatch.setenv("LATET_SERVER_URL", url)
            monkeypatch.setenv("LATET_SERVER_ADMIN_TOKEN", admin_token)
            monkeypatch.setattr("latet.cli.connect.socket.gethostname", lambda: "e0c-t16-laptop")
            yield SimpleNamespace(
                first=first,
                second=second,
                route=route,
                upstream=upstream,
                url=url,
                logs=logs,
                server=first_server,
                servers=servers,
                admin_token=admin_token,
                master_key=master_key,
            )


def test_a_person_and_an_api_key_on_two_instances(cluster):
    added = run_cli("server", "people", "add", "Emily Carter", "--email", "emily.carter@acme.com")
    assert added.exit_code == 0, added.output
    lines = added.output.splitlines()
    command = next(index for index, line in enumerate(lines) if line.strip().startswith("latet connect "))
    code = lines[command + 1].strip()  # Task 13 prints the code separately for hidden terminal entry.
    made = run_cli("server", "keys", "add", "Billing CI")
    assert made.exit_code == 0, made.output
    key = next(word for word in made.output.split() if word.startswith("latet_key_"))

    cluster.route["to"] = cluster.second
    connected = run_cli("connect", cluster.url, "--code", "-", "--yes", input=f"{code}\n")
    assert connected.exit_code == 0, connected.output
    assert code not in connected.output
    connection = load_connection()
    assert connection is not None and connection.url == cluster.url
    token = load_server_token(data_dir(), connection)
    assert token is not None
    computer_key = load_server_key(data_dir(), connection, token)  # kept with the token, for the CLI alone (R16)
    assert computer_key is not None and computer_key.startswith("latet_ck_")
    accepted_status = run_cli("status")
    assert accepted_status.exit_code == 0 and "Server token: accepted;" in accepted_status.output
    person = f"/u/{route_key(token)}"
    mine = {"x-latet-token": token}
    admin = {"x-latet-admin": cluster.admin_token}

    for client in (cluster.first, cluster.second):
        assert EMILY_PAT in _answer(_send(client, token, f"deploy with {EMILY_PAT}"))
        assert BILLING_PAT in _answer(_send(client, key, f"rotate {BILLING_PAT}"))
    assert EMILY_PAT not in cluster.upstream.all_received()
    assert BILLING_PAT not in cluster.upstream.all_received()

    cluster.route["to"] = cluster.first
    own = run_cli("server", "config", "--key", "Billing CI", "add", "terms", f"{{value: {TERM}, type: CLIENT}}")
    assert own.exit_code == 0, own.output
    assert TERM in _answer(_send(cluster.first, key, f"invoice {TERM}"))
    assert TERM not in cluster.upstream.all_received()
    # The same literal belongs to no personal term: a wrong settings scope would mask it here too.
    before = len(cluster.upstream.received)
    assert TERM in _answer(_send(cluster.second, token, f"discuss {TERM}"))
    assert TERM in b"".join(cluster.upstream.received[before:]).decode()

    for client in (cluster.first, cluster.second):
        destination = {"destinations": [{"url": cluster.upstream.url, "agent": "claude"}]}
        named = client.post(f"{person}/_latet/admin/destinations", json=destination, headers=mine)
        assert named.status_code == 200 and named.json()["names"]
        fake = client.post(f"{person}/_latet/admin/user-name", json={"name": "emily"}, headers=mine)
        assert fake.status_code == 200 and "surrogate" in fake.json()
        assert client.get(f"{person}/_latet/admin/destinations", headers=mine).status_code == 404
        sent = (list(cluster.upstream.received), list(cluster.upstream.requests), list(cluster.upstream.ws_received))
        refused = client.post(f"{person}/openai/v1/files", json={"file": EMILY_PAT}, headers=mine)
        assert refused.status_code == 400
        assert (cluster.upstream.received, cluster.upstream.requests, cluster.upstream.ws_received) == sent

    for client in (cluster.first, cluster.second):  # the token Claude Code holds opens no dashboard by itself
        alone = client.post(f"{MACHINE}/login-code", headers=mine)
        assert alone.status_code == 403 and alone.json()["reason"] == "computer_key_required"
        changed = client.put(f"{MACHINE}/identity", json={"usernames": ["e0c-t16-attacker"]}, headers=mine)
        assert changed.status_code == 403 and changed.json()["reason"] == "computer_key_required"
    printed = run_cli("dashboard", "--print-url")  # the CLI sends the key it keeps
    assert printed.exit_code == 0 and f"{cluster.url}/_latet/ui/login?code=" in printed.output
    assert computer_key not in printed.output
    signed = cluster.first.post(f"{MACHINE}/login-code", headers={**mine, "x-latet-computer-key": computer_key})
    assert signed.status_code == 200
    login = signed.json()
    link, login_code = login["url"], login["code"]
    assert cluster.first.get(link, follow_redirects=False).status_code == 303
    session = cluster.first.cookies["latet_session"]
    cluster.second.cookies.update(cluster.first.cookies)
    view = cluster.second.get(f"{API}/config").json()
    review = {**view["auto_mode_review"], "user_name": "surrogate"}
    saved = cluster.second.put(f"{API}/config", json={"auto_mode_review": review, "etag": view["etag"]}, headers=UI)
    assert saved.status_code == 200
    stale = cluster.first.put(f"{API}/config", json={"terms": [], "etag": view["etag"]}, headers=UI)
    assert stale.status_code == 409 and stale.json()["reason"] == "settings_stale"

    issued = cluster.first.post(
        f"{ADMIN}/admin-tokens", json={"label": "e0c-t16-second-admin", "days": 1}, headers=admin
    )
    assert issued.status_code == 201
    delegated = issued.json()
    other_admin = {"x-latet-admin": delegated["token"]}
    assert cluster.second.get(f"{ADMIN}/status", headers=other_admin).status_code == 200
    listed = cluster.second.get(f"{ADMIN}/admin-tokens", headers=admin)
    assert delegated["id"] in {item["id"] for item in listed.json()["tokens"]}
    assert delegated["token"] not in listed.text
    assert cluster.first.delete(f"{ADMIN}/admin-tokens/{delegated['id']}", headers=admin).status_code == 200
    asyncio.run(until(lambda: cluster.second.get(f"{ADMIN}/status", headers=other_admin).status_code == 401))

    for client in (cluster.first, cluster.second):
        assert client.get("/_latet/metrics").status_code == 404
        port = cluster.server.settings.metrics_port
        metrics = client.get(
            f"http://127.0.0.1:{port}/_latet/metrics", headers={"authorization": f"Bearer {METRICS_TOKEN}"}
        )
        assert metrics.status_code == 200 and "latet_build_info" in metrics.text
        _held_nothing(metrics.text, token, key, EMILY_PAT, BILLING_PAT, TERM)

    # Capture actual learned originals/fakes before disabling closes the person's runtime.
    originals, fakes, payload_fakes = set(), set(), set()
    for server, client in ((cluster.servers[0], cluster.first), (cluster.servers[1], cluster.second)):
        assert client.portal is not None
        for secret in (token, key):
            identity = server.identities.by_secret(secret)
            assert identity is not None
            runtime = client.portal.call(server.spaces.runtime, identity)
            recorder = runtime.recorder
            assert recorder is not None and recorder.drain(timeout=10), "owned history did not finish recording"
            for entity in runtime.built.vault.entities():
                originals.add(entity.original)
                fakes.add(entity.surrogate)
                if entity.original in {EMILY_PAT, BILLING_PAT, TERM}:
                    payload_fakes.add(entity.surrogate)
    assert fakes and {EMILY_PAT, BILLING_PAT, TERM} <= originals
    assert len(payload_fakes) >= 3
    for fake in payload_fakes:
        assert any(form in cluster.upstream.all_received() for form in _forms(fake))

    disabled = run_cli("server", "people", "disable", "Emily Carter")
    assert disabled.exit_code == 0, disabled.output
    for client in (cluster.first, cluster.second):
        asyncio.run(until(lambda client=client: _send(client, token, "hello").status_code in (401, 403)))
        assert _send(client, key, "still enabled").status_code == 200
    cluster.route["to"] = cluster.second
    for client in (cluster.first, cluster.second):
        refused = client.get(f"{MACHINE}/whoami", headers=mine)
        assert refused.status_code == 401 and refused.json() == {"error": "unauthorized"}

    refused_words = "Server token: the server no longer accepts this computer's token"  # R7: why, and what to do

    def status_refuses_token():
        result = run_cli("status")
        return result.exit_code == 3 and refused_words in " ".join(result.output.split())

    asyncio.run(until(status_refuses_token))
    status = run_cli("status")
    assert status.exit_code == 3 and refused_words in " ".join(status.output.split())
    assert "`latet disconnect`, then `latet connect" in " ".join(status.output.split())

    cluster.route["to"] = cluster.first
    log = run_cli("server", "log", "--json")
    assert log.exit_code == 0, log.output
    actions = {json.loads(line)["action"] for line in log.output.splitlines() if line.startswith("{")}
    assert {"person.added", "key.added", "person.disabled"} <= actions
    secrets = (
        token,
        computer_key,
        key,
        code,
        session,
        cluster.admin_token,
        delegated["token"],
        EMILY_PAT,
        BILLING_PAT,
        METRICS_TOKEN,
        REVIEWER_KEY,
        cluster.master_key,
        login_code,
        link,
        person,
        f"/u/{route_key(key)}",
        "sk-test",
    )
    metadata = (
        "e0c-t16-administrator",
        "e0c-t16-second-admin",
        "e0c-t16-laptop",
        "Billing CI",
        "Emily Carter",
        "emily.carter@acme.com",
    )
    pg = dump(cluster.server.db).decode(errors="replace")
    valkey = valkey_dump(cluster.server.bus, cluster.server.bus.prefix).decode(errors="replace")
    # No metadata exception applies to any credential, code, route, payload sentinel or actual fake.
    for text in (log.output, cluster.logs.getvalue(), pg, valkey):
        _held_nothing(text, *secrets, EMILY_PAT, BILLING_PAT, TERM, *fakes)
    # Identity/label plaintext is permitted only in these independently checked PostgreSQL cells.
    _checked_metadata(cluster.server.db)
    _held_nothing(_non_metadata_dump(cluster.server.db).decode(errors="replace"), *metadata, *originals)
    # Logs and Valkey retain the full original absence oracle, including metadata and every learned original.
    for text in (log.output, cluster.logs.getvalue(), valkey):
        _held_nothing(text, *metadata, *originals)
