"""Each answered scan and mask of the applications API is one `apps` event for the edition (R7): the key, its token's
id, the values a mask masked by type, the texts not answered; never a text, a value or a fake. An unmask is no event
here (the administration log records each, and reaches the SIEM from there), and an event that cannot be recorded never
breaks the call."""

import json
import logging

from starlette.testclient import TestClient

from latet.bridge.apps import APPS_API
from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, OnServer, create_app
from latet.gateway.mediate import Mediator

from ..conftest import ROOT_KEY

KEY, TOKEN = "u_billingcix", "t_k3yfortest"
GRANT = {"permissions": ["scan", "mask", "unmask"], "token": TOKEN}
EMAIL = "mario.rossi@acme.it"
URL = "https://latet.acme.lan"


def _app(record, grant=GRANT):
    built = build(Config.model_validate({"profile": "balanced", "ner": "off"}), Vault(ROOT_KEY))
    on_server = OnServer(URL, KEY, kind="key", record=record)
    app = create_app(
        GatewayConfig(mediator=Mediator(built.engine, built.verifier), bridge_token="b", on_server=on_server)
    )

    async def as_the_gate(scope, receive, send):  # what the server's gate hands on with a key's call
        if scope["type"] == "http":
            scope = scope | {"latet.identity": KEY, "latet.grant": grant}
        await app(scope, receive, send)

    return as_the_gate


def test_a_scan_and_a_mask_are_each_one_event_of_counts_and_an_unmask_none():
    events: list[dict] = []
    texts = {"texts": [f"write to {EMAIL}", "nothing here", f"and copy {EMAIL}"]}  # one value twice: counted once (M8)
    with TestClient(_app(events.append), base_url=URL) as client:
        assert client.post(f"{APPS_API}/scan", json=texts).status_code == 200
        masked = client.post(f"{APPS_API}/mask", json=texts)
        assert masked.status_code == 200
        fake = masked.json()["results"][0]["text"]
        assert client.post(f"{APPS_API}/unmask", json={"texts": [fake]}).status_code == 503  # no log here: no answer
    assert [(e["type"], e["outcome"], e["identity"], e["kind"], e["agent"], e["rule"]) for e in events] == [
        ("apps", "scan", KEY, "key", "apps", TOKEN), ("apps", "mask", KEY, "key", "apps", TOKEN),
    ]  # fmt: skip
    assert events[0]["masked"] == {}  # a scan masks nothing: its event counts the call
    assert events[1]["masked"] == {"EMAIL": 1} and events[1]["stopped"] == 0
    text = json.dumps(events)
    assert EMAIL not in text and fake not in text and "nothing here" not in text


def test_an_event_that_cannot_be_recorded_never_breaks_the_call():
    def refuse(event):
        raise RuntimeError("the edition is closing")

    with TestClient(_app(refuse), base_url=URL) as client:
        assert client.post(f"{APPS_API}/mask", json={"texts": [f"write to {EMAIL}"]}).status_code == 200


def test_the_texts_a_mask_does_not_answer_are_counted_as_stopped(monkeypatch):
    """A text the local reviewer or the dump guard stops is answered ``blocked``: the event counts it, and counts none of
    its values, which never left."""
    from latet.bridge import apps as apps_module

    events: list[dict] = []
    real = apps_module._masked_results

    def one_blocked(out, degraded):
        out[1] = out[1] | {"blocked": "the local reviewer stopped this text"}
        return real(out, degraded)

    monkeypatch.setattr(apps_module, "_masked_results", one_blocked)
    with TestClient(_app(events.append), base_url=URL) as client:
        texts = {"texts": [f"write to {EMAIL}", "call +39 347 123 4567", f"and copy {EMAIL}"]}
        answered = client.post(f"{APPS_API}/mask", json=texts).json()["results"]
    assert "blocked" in answered[1]
    [event] = events
    assert event["masked"] == {"EMAIL": 1} and event["stopped"] == 1


def test_a_grant_that_names_no_token_id_is_said_in_the_log_never_written_as_one(caplog):
    """The gate's grant names the token by its id (``t_`` and 10 characters): anything else is never written in the
    event, and its call is answered all the same, said by the error's type."""
    events: list[dict] = []
    secret = "latet_key_" + "s3cr3t" * 6
    with (
        caplog.at_level(logging.WARNING),
        TestClient(_app(events.append, GRANT | {"token": secret}), base_url=URL) as c,
    ):
        assert c.post(f"{APPS_API}/scan", json={"texts": ["hello"]}).status_code == 200
    assert events == []
    assert "ValueError" in caplog.text and secret not in caplog.text


def test_a_grant_that_names_no_token_is_said_in_the_log_never_an_event_without_one(caplog):
    """The gate always names the call's token: a grant without one (or with an empty one) records no event that names
    none, and its call is answered all the same, said by the error's type."""
    for grant in ({"permissions": ["scan"]}, {"permissions": ["scan"], "token": ""}, GRANT | {"token": None}):
        events: list[dict] = []
        caplog.clear()
        with caplog.at_level(logging.WARNING), TestClient(_app(events.append, grant), base_url=URL) as client:
            assert client.post(f"{APPS_API}/scan", json={"texts": ["hello"]}).status_code == 200
        assert events == [] and "ValueError" in caplog.text
