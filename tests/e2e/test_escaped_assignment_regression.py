"""Synthetic serialized-tool assignment regression. No production vault or payload."""

import json

import pytest
from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from .fake_upstream import FakeUpstream


@pytest.mark.parametrize("surface", ["user", "tool", "subagent"])
@pytest.mark.parametrize("value", ["secret", "canary", "Synthet1c-Real-Secret-939"])
def test_serialized_assignment_masking_and_final_check_agree(surface, value):
    fake = FakeUpstream()
    fake.start()
    try:
        built = build(Config(ner="off"), Vault(b"8" * 32))
        route = Upstream(
            "chatgpt", "/backend-api/codex", fake.url, keep_prefix=True, envelope_expected=frozenset({("model", "x")})
        )
        config = GatewayConfig(port=5283, upstreams=[route], mediator=Mediator(built.engine, built.verifier))
        snippet = f'+@pytest.mark.parametrize("region", ["us; token={value}", "bad"])'
        text = json.dumps({"output": snippet})
        if surface == "user":
            items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]
        else:
            items = [
                {"type": "function_call", "call_id": "call_1", "name": "terminal", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": text},
            ]
            if surface == "subagent":
                items.insert(
                    0,
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "Isolated worker task: review a synthetic diff."}],
                    },
                )
        body = {"model": "x", "stream": True, "store": False, "input": items}
        with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
            response = client.post(
                "/backend-api/codex/responses",
                json=body,
                headers={"authorization": "Bearer synthetic-only", "session_id": "20260101_000000_abcdef"},
            )
        assert response.status_code == 200, response.text
        assert len(fake.requests) == 1
        if value != "secret":
            assert value not in fake.all_received(), "synthetic credential leaked"
        # The structural placeholder is not a new secret merely because JSON escapes its quote.
        else:
            assert not any(e.original == "secret\\" for e in built.vault.entities())
    finally:
        fake.stop()
