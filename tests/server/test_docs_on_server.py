"""A Latet server serves the documentation of its own version at /_latet/docs/, as a computer's daemon does: public, with
no session, under the server's own name only, with the documentation's own security policy; and the dashboard of a
person signed in there hears that it has it, so that its links open the server's own copy."""

import base64
import json

import pytest
from starlette.testclient import TestClient

from latet.api import rest
from latet.server import app as app_module
from latet.server.app import create_server_app

from .support import URL
from .test_app import _enrolled, _signed_in

pytestmark = pytest.mark.server

HASH = "sha256-" + base64.b64encode(b"\x07" * 32).decode()


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """A small bundle in place of the package's, for the server's own routes and for its spaces' dashboards."""
    bundle = tmp_path / "docs"
    for rel, html in (
        ("index.html", "<h1>Latet</h1>"),
        ("it/dashboard/index.html", "<h1>Guida alla dashboard</h1>"),
        ("404.html", "<h1>Page not found</h1>"),
    ):
        (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle / rel).write_bytes(f"<!doctype html>{html}".encode())
    (bundle / ".csp.json").write_bytes(json.dumps({"script_hashes": [HASH]}).encode())
    (tmp_path / "secret.txt").write_bytes(b"outside the bundle")
    monkeypatch.setattr(app_module, "DOCS_DIR", bundle)
    monkeypatch.setattr(rest, "DOCS_DIR", bundle)
    return bundle


@pytest.fixture
def client(server):
    with TestClient(create_server_app(server), base_url=URL) as found:
        yield found


def test_a_server_serves_the_docs_without_a_session(server, client, docs):
    page = client.get("/_latet/docs/it/dashboard/")
    assert page.status_code == 200 and "Guida alla dashboard" in page.text
    policy = page.headers["content-security-policy"]
    assert f"'{HASH}'" in policy and "'wasm-unsafe-eval'" in policy and "frame-ancestors 'none'" in policy
    assert "http" not in policy and page.headers["referrer-policy"] == "no-referrer"
    missing = client.get("/_latet/docs/it/nowhere/")
    assert missing.status_code == 404 and "Page not found" in missing.text
    outside = client.get("/_latet/docs/%2e%2e/secret.txt")
    assert outside.status_code == 404 and "outside the bundle" not in outside.text
    assert client.get("/_latet/docs", follow_redirects=False).headers["location"] == "/_latet/docs/"
    assert client.get("/_latet/docs/", headers={"host": "evil.example"}).status_code == 403  # its own name only


def test_the_dashboard_on_a_server_hears_whether_it_has_the_docs(server, client, docs):
    assert client.get("/_latet/health").json() == {"ok": True, "server": True, "docs": True}  # the sign-in page's
    _, token = _enrolled(server, client)
    _signed_in(client, token)
    assert client.get("/_latet/api/v1/me").json()["docs"] is True
