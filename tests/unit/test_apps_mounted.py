"""The applications API is a server's alone: a space on a Latet server mounts it beside the plugin's bridge, and a
computer's Latet answers its paths as any path of Latet's it does not have."""

from starlette.testclient import TestClient

from latet.bridge.apps import APPS_API
from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, OnServer, create_app
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY


def _mediator() -> Mediator:
    built = build(Config.model_validate({"profile": "balanced", "ner": "off"}), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def _paths(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_a_computer_has_no_applications_api():
    app = create_app(GatewayConfig(mediator=_mediator(), bridge_token="bridge-token"))
    assert not {path for path in _paths(app) if path.startswith(APPS_API)}
    with TestClient(app, base_url="http://127.0.0.1:5283") as client:
        assert client.get("/_latet/engine/v1/ping", headers={"x-latet-token": "bridge-token"}).status_code == 200
        answer = client.post(f"{APPS_API}/mask", json={"texts": ["to mario.rossi@acme.it"]})
    assert answer.status_code == 404 and answer.json() == {"error": "there is no such route"}


def test_an_api_keys_space_on_a_server_mounts_it():
    on_server = OnServer("https://latet.acme.lan", "u_billingcix", kind="key")
    app = create_app(GatewayConfig(mediator=_mediator(), bridge_token="bridge-token", on_server=on_server))
    assert {f"{APPS_API}/scan", f"{APPS_API}/mask", f"{APPS_API}/unmask"} <= _paths(app)


def test_a_persons_space_on_a_server_has_none():
    """People use the dashboard and their agents: the server's gate refuses a person's token on the applications API,
    and a person's space has none of it whatever reached it."""
    for on_server in (OnServer("https://latet.acme.lan", "u_emilyxxxx"), OnServer("https://x", "u_e", kind="person")):
        app = create_app(GatewayConfig(mediator=_mediator(), bridge_token="bridge-token", on_server=on_server))
        assert not {path for path in _paths(app) if path.startswith(APPS_API)}
