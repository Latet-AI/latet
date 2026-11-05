"""A live launch key never reaches the Live Reviewer (root's ruling on P2-6a's re-review, R-1): a tool output that holds
one waits there masked, its stand-in in the view where the other values show their originals, and neither the batch,
what the dashboards are told, nor the history's restored view carries the key. Over the gateway as through the
plugin's mask call."""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette

from latet.api.rest import restore_detail
from latet.bridge.api import Bridge
from latet.engine.masker import MaskReport
from latet.engine.types import SlotKind
from latet.gateway.routes import Upstream

from .conftest import EMAIL

KEY = "latet_" + "lk_" + "Qm9_" * 10 + "z1A"
ENV = f"PATH=/usr/bin\nANTHROPIC_CUSTOM_HEADERS=x-latet-launch-key: {KEY}\nMAIL={EMAIL}\n"
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1", envelope_expected=frozenset({("model", "m")}))


def _told(rt, monkeypatch) -> list:
    """Everything the dashboards are told while the test runs."""
    told: list = []
    broadcaster = rt.mediator.approvals.broadcaster
    publish = broadcaster.publish

    def recording(payload, *args, **kwargs):
        told.append(json.dumps(payload, default=str))
        return publish(payload, *args, **kwargs)

    monkeypatch.setattr(broadcaster, "publish", recording)
    return told


def _shown(views: list[dict]) -> str:
    return json.dumps(views, default=str)


def test_the_gateways_live_batch_shows_the_key_masked(live):
    body = json.dumps(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "Why does the build fail?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": ENV},
            ],
        }
    ).encode()
    mediator = live.mediator
    prepared = mediator.prepare(
        OPENAI, "POST", "/openai/v1/chat/completions", {"content-type": "application/json"}, body
    )
    assert not prepared.block_reason, prepared.block_reason
    config = mediator.live_config(prepared.session)
    assert config is not None
    items = mediator._live_items(prepared, config)
    batch = mediator.live_batch(prepared.session, config, items, "agent", False)
    assert batch is not None
    shown = _shown(batch.summary()["items"])
    assert KEY not in shown, "the Live Reviewer's view holds the key"
    assert EMAIL in shown, "the other values show their originals, as before"
    if prepared.lease is not None:
        prepared.lease.release()


@pytest.mark.parametrize("item", [{"kind": "tool_result", "tool": "Bash"}, {"kind": "user_text"}])
async def test_the_plugins_live_batch_and_what_the_dashboards_are_told_hold_no_key(live, monkeypatch, item):
    told = _told(live, monkeypatch)
    bridge = Bridge(live.mediator, live.gateway.bridge_token)
    transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
        asked = {"session": "live-key", "items": [{"text": ENV, **item}]}
        answer = await client.post(
            "/_latet/engine/v1/mask", json=asked, headers={"x-latet-token": live.gateway.bridge_token}
        )
        pending = answer.json()["pending"]
        batch = live.mediator.approvals.batches[pending]
        shown = _shown(batch.summary()["items"])
        assert KEY not in shown and EMAIL in shown
        assert told and all(KEY not in payload for payload in told), "the dashboards were told the key"
        assert await live.mediator.approvals.decide(pending, False)


def test_the_historys_restored_view_keeps_the_keys_stand_in(live):
    """The dashboard's exchange detail rebuilds originals from the vault (``restore_detail``): never a live key's."""
    mediator = live.mediator
    session = mediator.engine.session("conv:history-key")
    done = mediator.mask_value(ENV, SlotKind.TOOL_RESULT, session, MaskReport(), tool="Bash")
    assert KEY not in done.text
    replacements = []
    for r in done.replacements:
        at = done.text.find(r.replacement)
        replacements.append(
            {"masked_start": at, "masked_end": at + len(r.replacement), "type": r.etype, "class": r.eclass.name,
             "entity": r.entity_id}
        )  # fmt: skip
    detail = {"request": {"slots": [{"masked": done.text, "replacements": replacements, "block": "b"}]}, "leaks": []}
    shown = json.dumps(restore_detail(detail, mediator.engine.vault))
    assert KEY not in shown and EMAIL in shown
