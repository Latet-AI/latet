"""The dashboard's request exceptions (P2-7, D3, R-75): ``GET /exceptions`` lists each with what it names, where, for whom
and its state now; ``DELETE /exceptions/{id}`` takes one away at once (it raises protection); a change that adds one
waits for you, and says before you approve what it would let through and for whom (the whole identity, or a project)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from starlette.testclient import TestClient

from latet.config import Config, ExceptionComponent, IdentityConfig, RequestException, load_config, save_config
from latet.gateway.app import create_app
from latet.gateway.request_profiles import REVISION
from latet.runtime import make_runtime
from latet.state.local import LocalSettings

from ..conftest import ROOT_KEY

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}


def _exception(**given) -> RequestException:
    fields = {
        "destination": "anthropic",
        "profile": "anthropic.messages.1",
        "components": [ExceptionComponent(kind="header", name="x-company-team")],
        "reason": "custom-header",
        "description": "the header our gateway wants",
    }
    return RequestException(**(fields | given))


def _approved(days_ago: float, **given) -> RequestException:
    granted = datetime.now(UTC) - timedelta(days=days_ago)
    return _exception(**({"id": "x0123456789", "granted": granted, "revision": REVISION} | given))


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    return tmp_path


def _daemon(files, config: Config):
    save_config(config, files / "config.yaml")
    # Approved before this start: Latet's stamps are taken as approved only with your approval behind them (Minor 5).
    LocalSettings(files / "config.yaml", files / "data").save_reviewed(config)
    rt = make_runtime(data=files / "data", config_file=files / "config.yaml")
    client = TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283")
    client.__enter__()
    code = client.post("/_latet/admin/login-code", headers={"x-latet-token": str(rt.gateway.bridge_token)})
    client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
    return rt, client


def _closed(rt, client) -> None:
    client.__exit__(None, None, None)
    rt.close()


def test_the_list_says_what_each_exception_names_where_for_whom_and_its_state(files):
    rows = [
        _approved(25, issuer="owner"),
        _approved(1, id="x00000000ab", permanent=True, project=str(files / "shop"), issuer="owner"),
    ]
    rt, client = _daemon(files, Config(identity=IdentityConfig(enabled=False), request_exceptions=rows))
    try:
        answer = client.get(f"{API}/exceptions").json()
    finally:
        _closed(rt, client)
    assert answer["permanent_allowed"] is True
    first, second = answer["items"]
    # Who approved each (P2-6's Minor 9, ruling (b)): by its category alone.
    assert first["issuer"] == "owner" and second["issuer"] == "owner"
    assert first["id"] == "x0123456789" and first["state"] == "expiring" and first["days_left"] in (4, 5)
    assert first["scope"] == "identity" and first["project"] is None
    assert first["destination"] == "anthropic" and first["profile"] == "anthropic.messages.1"
    assert first["components"] == [{"kind": "header", "name": "x-company-team", "role": "checked", "type": None}]
    assert first["reason"] == "custom-header" and first["permanent"] is False and first["granted"]
    assert second["state"] == "permanent" and second["ends"] is None and second["days_left"] is None
    # A project's by the name of its folder alone, never the folder.
    assert second["scope"] == "project" and second["project"] == "shop"
    assert str(files) not in str(answer)


def test_revoking_takes_the_exception_away_at_once(files):
    rt, client = _daemon(files, Config(identity=IdentityConfig(enabled=False), request_exceptions=[_approved(2)]))
    try:
        missing = client.delete(f"{API}/exceptions/x9999999999", headers=UI)
        revoked = client.delete(f"{API}/exceptions/x0123456789", headers=UI)
        after = client.get(f"{API}/exceptions").json()
        waiting = client.get(f"{API}/changes").json()["changes"]
    finally:
        _closed(rt, client)
    assert missing.status_code == 404
    assert revoked.status_code == 200 and revoked.json()["ok"] is True
    assert after["items"] == [] and waiting == []  # raising protection waits for nobody
    assert load_config(files / "config.yaml").request_exceptions == []


def test_a_change_adding_an_exception_says_what_it_lets_through_before_it_is_approved(files):
    rt, client = _daemon(files, Config(identity=IdentityConfig(enabled=False)))
    try:
        data = yaml.safe_load((files / "config.yaml").read_text(encoding="utf-8")) or {}
        data["request_exceptions"] = [_exception().model_dump(mode="json", exclude_none=True)]
        (files / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        reloaded = client.post("/_latet/admin/reload", headers={"x-latet-token": str(rt.gateway.bridge_token)})
        [change] = client.get(f"{API}/changes").json()["changes"]
        listed = client.get(f"{API}/exceptions").json()["items"]
    finally:
        _closed(rt, client)
    assert reloaded.status_code == 200
    assert any("request exception for anthropic" in line for line in change["loosened"])
    [shown] = change["exceptions"]
    assert shown["how"] == "added" and shown["scope"] == "identity" and shown["id"] is None
    assert shown["components"][0]["name"] == "x-company-team" and shown["days"] == 30 and shown["permanent"] is False
    assert listed == []  # the settings in use have none until you approve it


def test_the_list_is_judged_by_the_decision_clock_and_the_editions_answer(files):
    """Review Minor 5 (P9, P10): the states are judged by the runtime's exception clock (PostgreSQL's on a server),
    never this process's own clock, and a permanent exception the edition does not allow is listed as forbidden."""
    rows = [_approved(2, issuer="owner"), _approved(1, id="x00000000ab", permanent=True, issuer="owner")]
    rt, client = _daemon(files, Config(identity=IdentityConfig(enabled=False), request_exceptions=rows))
    try:
        rt.exception_clock = lambda: datetime.now(UTC) + timedelta(days=40)
        rt.permanent_allowed = lambda: False
        answer = client.get(f"{API}/exceptions").json()
    finally:
        _closed(rt, client)
    first, second = answer["items"]
    assert first["state"] == "expired" and first["days_left"] == 0
    assert answer["permanent_allowed"] is False and second["state"] == "forbidden"


def test_a_change_lists_only_the_exceptions_it_lowers_protection_by(files):
    """Review Minor 5 (P8): a change that adds one exception beside an approved one lists the new one alone."""
    rt, client = _daemon(files, Config(identity=IdentityConfig(enabled=False), request_exceptions=[_approved(2)]))
    try:
        data = yaml.safe_load((files / "config.yaml").read_text(encoding="utf-8")) or {}
        added = _exception(components=[ExceptionComponent(kind="header", name="x-company-cost")])
        data["request_exceptions"] = [*data["request_exceptions"], added.model_dump(mode="json", exclude_none=True)]
        (files / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        client.post("/_latet/admin/reload", headers={"x-latet-token": str(rt.gateway.bridge_token)})
        [change] = client.get(f"{API}/changes").json()["changes"]
    finally:
        _closed(rt, client)
    [shown] = change["exceptions"]
    assert shown["how"] == "added" and shown["components"][0]["name"] == "x-company-cost"
