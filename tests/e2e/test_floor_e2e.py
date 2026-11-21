"""A change below the company's floor is refused wherever it is made: the dashboard's saves answer 409 naming the
settings, and a reload the daemon refuses exits 2, leaving the settings in use as they were."""

import base64
import io
import json
import urllib.error
from email.message import Message

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.config import Config, IdentityConfig, LlmReview, Profile, ProjectEntry, save_config
from latet.floor import FLOOR_UNAVAILABLE, FloorUnavailable
from latet.gateway.app import create_app
from latet.gateway.routes import Upstream
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY
from ..floors import CompanyFloor, FloorEdition
from .test_columns_e2e import _login, _orders_exchange, upstream  # noqa: F401 - upstream: the fixture

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}


def _cli(*args):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args))


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon whose company sets the profile at max, with the CLI's own calls going to it."""
    from latet.cli import admin

    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    save_config(Config(profile="max", identity=IdentityConfig(enabled=False)), tmp_path / "config.yaml")
    rt = make_runtime(
        data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=FloorEdition(CompanyFloor())
    )
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:

        def call(method, path, body=None, timeout=60):
            headers = {"x-latet-token": rt.gateway.bridge_token, "x-latet-command": "1"}
            r = client.request(method, path, json=None if method == "GET" else (body or {}), headers=headers)
            answer = r.json()
            return answer | {"status": r.status_code} if r.status_code >= 400 else answer

        monkeypatch.setattr(admin, "_admin", call)
        code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
        client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
        yield rt, client
    rt.close()


def test_the_dashboard_refuses_a_save_below_the_floor_naming_the_setting(daemon):
    rt, client = daemon
    r = client.put(f"{API}/config", json={"profile": "balanced"}, headers=UI)
    assert r.status_code == 409
    assert r.json() == {
        "error": "Set by your company, not changed: profile: from max to balanced",
        "floor": ["profile: from max to balanced"],
    }
    assert rt.config.profile == "max"


def test_marking_a_value_not_sensitive_is_refused_when_none_of_yours_counts(daemon):
    _, client = daemon
    r = client.post(f"{API}/allow", json={"value": "acme.com"}, headers=UI)
    assert r.status_code == 409 and r.json()["floor"] == ["values never to mask: 1 added"]


def test_an_import_below_the_floor_is_refused(daemon):
    _, client = daemon
    rules = {"file": "latet_rules: 1\nprofile: secrets\n", "apply": True, "all": True}
    r = client.post(f"{API}/rules/import", json=rules, headers=UI)
    assert r.status_code == 409 and "profile: from max to secrets" in r.json()["floor"]


def test_a_projects_own_settings_cannot_turn_latet_off(daemon, tmp_path):
    rt, client = daemon
    project = rt.projects.resolve(str(tmp_path / "shop"))
    r = client.put(f"{API}/projects/{project.id}/personal", json={"enabled": False}, headers=UI)
    assert r.status_code == 409 and r.json()["floor"] == ["project shop: Latet turned off"]


def test_a_projects_page_shows_your_own_settings_as_you_set_them_and_its_rules_as_they_apply(daemon, tmp_path):
    rt, client = daemon
    shop = str(tmp_path / "shop")
    rt.apply_config(rt.config.model_copy(update={"projects": [ProjectEntry(path=shop, enabled=False)]}), save=False)
    project = rt.projects.resolve(shop)
    view = client.get(f"{API}/projects/{project.id}").json()
    assert view["personal"]["enabled"] is False  # yours, as you set it before the floor came
    assert view["enabled"] is True and view["effective"]["profile"] == "max"  # what applies there


def test_a_save_beside_settings_already_below_the_floor_goes_through(daemon):
    rt, client = daemon
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.BALANCED}), save=False)  # as an older config.yaml
    r = client.put(f"{API}/config", json={"history": "metadata"}, headers=UI)
    assert r.status_code == 200 and rt.config.history == "metadata"
    assert client.get(f"{API}/me").json()["profile"] == "max"  # the profile in use
    assert client.get(f"{API}/config").json()["profile"] == "balanced"  # yours, as you set it


def test_approving_a_change_that_went_below_the_floor_since_is_refused(daemon):
    rt, client = daemon
    rt.edition.locked = None  # type: ignore[attr-defined]
    change = rt.propose(rt.config.model_copy(update={"profile": Profile.BALANCED}), "command", "hermes")
    assert change is not None
    rt.edition.locked = CompanyFloor()  # type: ignore[attr-defined]
    r = client.post(f"{API}/changes/{change.id}", json={"action": "approve"}, headers=UI)
    assert r.status_code == 409 and r.json()["floor"] == ["profile: from max to balanced"]
    assert rt.mediator.approvals.settled[change.id] == "rejected" and rt.config.profile == "max"


def test_a_reload_of_a_config_yaml_below_the_floor_is_refused(daemon):
    rt, client = daemon
    save_config(Config(profile="secrets", identity=IdentityConfig(enabled=False)), rt.config_file)  # by hand
    headers = {"x-latet-token": rt.gateway.bridge_token, "x-latet-command": "1"}
    r = client.post("/_latet/admin/reload", headers=headers)
    assert r.status_code == 409 and r.json()["floor"] == ["profile: from max to secrets"]
    assert rt.config.profile == "max" and rt.mediator.approvals.changes == {}


def test_a_config_yaml_read_again_is_raised_never_refused(daemon):
    """L5: a reload the daemon makes of itself (``file``, not a command) is weighed as arriving settings are: one that
    lowers protection waits for you, one that only comes closer to the floor applies, and nothing is refused."""
    rt, client = daemon
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.SECRETS}), save=False)  # as an older config.yaml
    token = {"x-latet-token": rt.gateway.bridge_token}  # no x-latet-command: the file read again
    save_config(Config(profile="balanced", identity=IdentityConfig(enabled=False)), rt.config_file)
    closer = client.post("/_latet/admin/reload", headers=token)
    assert closer.status_code == 200 and closer.json()["applied"] is True and rt.config.profile == "balanced"
    save_config(Config(profile="secrets", identity=IdentityConfig(enabled=False)), rt.config_file)
    lower = client.post("/_latet/admin/reload", headers=token)
    assert lower.status_code == 200 and lower.json()["applied"] is False and rt.config.profile == "balanced"
    assert rt.in_use().profile == "max"


def test_while_the_floor_cannot_be_said_nothing_changes_and_the_dashboard_says_so_in_fixed_words(daemon):
    """A server's policy this instance cannot read (Latet Enterprise): every change, and a text tried, answer 503 with
    the dashboard's own words, never the error's, nor a traceback."""
    rt, client = daemon
    rt.edition.locked = None  # type: ignore[attr-defined]
    waiting = rt.propose(rt.config.model_copy(update={"profile": Profile.BALANCED}), "command", "hermes")
    assert waiting is not None
    before = rt.config

    def unreadable(identity):
        raise FloorUnavailable("this instance cannot read the server's policy (made by a newer Latet)")

    rt.edition.floor = unreadable  # type: ignore[method-assign]
    imported = "latet_rules: 1\nterms: [{value: Orion}]\n"
    command = {"x-latet-token": rt.gateway.bridge_token, "x-latet-command": "1"}
    save_config(Config(profile="max", history="metadata", identity=IdentityConfig(enabled=False)), rt.config_file)
    answers = [
        client.put(f"{API}/config", json={"history": "metadata"}, headers=UI),
        client.post(f"{API}/rules/import", json={"file": imported, "apply": True, "all": True}, headers=UI),
        client.post(f"{API}/playground", json={"text": "write to emily.carter@acme.com", "profile": "max"}, headers=UI),
        client.post("/_latet/admin/reload", headers=command),
        client.post(f"{API}/changes/{waiting.id}", json={"action": "approve"}, headers=UI),
    ]
    for answer in answers:
        assert (answer.status_code, answer.json()) == (503, {"error": FLOOR_UNAVAILABLE}), answer.text
    assert rt.config is before and waiting.id in rt.mediator.approvals.changes  # nothing applied, nothing decided


def test_the_playground_tries_a_profile_raised_to_the_floor(daemon):
    _, client = daemon
    body = {"text": "write to emily.carter@acme.com", "profile": "secrets"}  # secrets alone leaves addresses
    assert "emily.carter@acme.com" not in client.post(f"{API}/playground", json=body, headers=UI).json()["masked"]


def test_a_reload_of_a_config_yaml_below_the_floor_exits_2(daemon):
    rt, _ = daemon
    save_config(Config(profile="balanced", identity=IdentityConfig(enabled=False)), rt.config_file)  # by hand
    result = _cli("reload")
    assert result.exit_code == 2, result.output
    assert "Set by your company, not changed: profile: from max to balanced." in result.output
    assert rt.config.profile == "max"


def test_the_playground_tries_a_text_with_the_settings_in_use(daemon):
    rt, client = daemon
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.SECRETS}), save=False)  # yours: secrets only
    masked = client.post(f"{API}/playground", json={"text": "write to emily.carter@acme.com"}, headers=UI).json()
    assert "emily.carter@acme.com" not in masked["masked"]  # max masks it: the profile in use


def test_the_columns_of_an_output_are_weighed_by_the_settings_in_use(tmp_path, monkeypatch, upstream):  # noqa: F811
    """Yours keep strict data mode off (balanced); the company's max turns it on: an output was masked so, and its
    columns say so."""
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    config = Config(identity=IdentityConfig(enabled=False), profile="balanced")
    save_config(config, tmp_path / "config.yaml")
    rt = make_runtime(
        config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", edition=FloorEdition(CompanyFloor())
    )
    rt.gateway.upstreams = [Upstream("openai", "/openai", upstream.url)]
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            _login(client, rt)
            exchange, slot = _orders_exchange(client, rt)
            answer = client.get(f"{API}/exchanges/{exchange}/columns", params={"slot": slot}).json()
        assert rt.config.data_mode() == "normal" and answer["strict"] is True
    finally:
        rt.close()


def test_the_local_reviewer_takes_its_new_key_with_the_settings_in_use(daemon, monkeypatch):
    """The company's guidance for the local reviewer (Latet Enterprise) stays when its key changes."""
    rt, client = daemon

    class Guided(CompanyFloor):
        def raise_to(self, config):
            raised = super().raise_to(config)
            review = raised.llm_review.model_copy(update={"guidance": "Ward names are confidential."})
            return raised.model_copy(update={"llm_review": review})

    rt.edition.locked = Guided()  # type: ignore[attr-defined]
    review = LlmReview(enabled=True, url="http://127.0.0.1:9/v1", model="qwen")
    rt.apply_config(rt.config.model_copy(update={"llm_review": review}), save=False)
    assert rt.mediator.llm_review.guidance == "Ward names are confidential."
    r = client.delete(f"{API}/reviewer/key", headers=UI)
    assert r.status_code == 200, r.text
    assert rt.mediator.llm_review.guidance == "Ward names are confidential."


# --- the command line's look at a refusal ---------------------------------------------------------------------------


def _answering(monkeypatch, status, body):
    """The CLI's call to the daemon answered ``status`` with ``body`` (whatever listens on Latet's port)."""

    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, "refused", Message(), io.BytesIO(body))

    monkeypatch.setattr("latet.service.daemon.local_urlopen", urlopen)
    monkeypatch.setattr("latet.storage.keys.load_bridge_token", lambda data: "t")
    monkeypatch.setattr("latet.config.load_config", lambda *a, **k: Config())


def test_the_command_line_names_what_the_company_set_only_from_a_refusal_of_the_floor(monkeypatch):
    from latet.cli import admin

    said = ["profile: from max to balanced", "terms: 1 value" + chr(0x202E) + " no longer masked"]  # an override
    _answering(monkeypatch, 409, json.dumps({"error": "x", "floor": said}).encode())
    assert admin._admin("POST", "/_latet/admin/reload")["floor"] == said
    result = CliRunner().invoke(admin.app, ["reload"])
    assert result.exit_code == 2 and chr(0x202E) not in result.output and "\\u202e" in result.output
    assert "Set by your company, not changed: profile: from max to balanced; terms: 1 value" in result.output
    for status, floor in [
        (400, said),
        (500, said),
        (409, "profile: from max to balanced"),
        (409, [1]),
        (409, ["x" * 300]),
    ]:
        _answering(monkeypatch, status, json.dumps({"error": "x", "floor": floor}).encode())
        assert "floor" not in admin._admin("POST", "/_latet/admin/reload")
