"""The plugin asks the daemon whether a session goes through Latet; a session that does not is
recorded, so the dashboard shows it."""

import base64

import pytest
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY

pytestmark = pytest.mark.e2e

CHECK = "/_latet/engine/v1/route-check"


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
        yield rt, client
    rt.close()


def test_a_session_that_goes_around_latet_is_stopped_and_recorded(daemon):
    rt, client = daemon
    token = {"x-latet-token": rt.gateway.bridge_token}
    facts = {
        "bridge": "http://127.0.0.1:5283",
        "session": "cc-route",
        "env": {"ANTHROPIC_BASE_URL": "https://x.example"},
        "sources": {"ANTHROPIC_BASE_URL": "project"},
        "entrypoint": "cli",
        "surface": "terminal",
    }
    assert client.post(CHECK, json=facts).status_code == 401
    verdict = client.post(CHECK, json=facts, headers=token).json()
    assert verdict["ok"] is False and "x.example" in verdict["reason"]
    rt.recorder.drain()
    [last] = rt.exchanges.list(limit=1)[0]
    assert "route-bypass" in last["flags"] and last["agent"] == "claude-code"
    fine = {**facts, "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic"}, "sources": {}}
    assert client.post(CHECK, json=fine, headers=token).json() == {"ok": True, "reason": None, "toast": None}
