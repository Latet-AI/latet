"""Newly learned credentials must be remasked in previously sent tool output."""

import json

from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.routes import Upstream

from .fake_upstream import FakeUpstream


def test_replayed_tool_remasks_later_learned_token():
    value = "canary-only-47"
    fake = FakeUpstream()
    fake.start()
    try:
        built = build(Config(ner="off"), Vault(b"6" * 32))
        route = Upstream(
            "chatgpt", "/backend-api/codex", fake.url, keep_prefix=True, envelope_expected=frozenset({("model", "x")})
        )
        mediator = Mediator(built.engine, built.verifier)
        config = GatewayConfig(port=5283, upstreams=[route], mediator=mediator)
        old = [
            {"type": "function_call", "call_id": "call_1", "name": "skill_view", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Read the " + value + " test documentation.",
            },
        ]

        def request(c, items):
            return c.post(
                "/backend-api/codex/responses",
                json={"model": "x", "stream": True, "store": False, "input": items},
                headers={"authorization": "Bearer fixture-only", "session_id": "20260101_000000_123abc"},
            )

        with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
            first = request(client, old)
            assert first.status_code == 200
            # A completed response proves receipt; the verifier must also have
            # recorded this exact historical text before learning the credential.
            assert len(fake.received) == 1
            assert json.loads(fake.received[0])["input"][1]["output"] == old[1]["output"]
            mediator.executor.submit(lambda: None).result(timeout=5)
            assert built.verifier.sent("session_id:20260101_000000_123abc", old[1]["output"])
            second = request(
                client,
                [
                    *old,
                    {"type": "function_call", "call_id": "call_2", "name": "read_file", "arguments": "{}"},
                    {
                        "type": "function_call_output",
                        "call_id": "call_2",
                        "output": "headers={'authorization':'Bearer " + value + "'}",
                    },
                ],
            )
            assert second.status_code == 200, second.text
            assert len(fake.requests) == 2
            received = json.loads(fake.received[-1])
            assert value not in json.dumps(received), "newly learned credential survived in historical text"
            assert len(received["input"]) == 4
            assert received["input"][1]["output"] != old[1]["output"]
            assert any(entity.original == value for entity in built.vault.entities())
    finally:
        fake.stop()
