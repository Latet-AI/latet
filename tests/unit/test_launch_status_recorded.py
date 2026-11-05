"""What a request's launch was decided as, recorded on its exchange by fixed categories (root's ruling I-5 on P2-6a's
review): ``launched`` (its launch key verified), ``launched-key-missing`` (a session Latet launched, without a key it
can verify), ``trusted`` (the plugin's binding of a hand-configured Claude Code), ``claimed`` (anything else). P2-7
shows it on the dashboard and in ``latet doctor``; nothing of the key, the folder or the project is in it."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from latet.config import Config, ProjectEntry
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.launch_authority import LAUNCH_CATEGORIES
from latet.gateway.launch_keys import Binding, LocalLaunchKeys
from latet.projects import Projects
from latet.storage.exchanges import safe_summary

from .test_request_leaks import _Answer
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks

_TOKEN = "launch-status-bridge-token"
_BODY = json.dumps({"model": "m", "messages": [{"role": "user", "content": "ordinary request"}]}).encode()
_ANSWER = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"ordinary provider reply"}}]}'


@pytest.mark.parametrize("category", [*LAUNCH_CATEGORIES, None])
def test_the_summary_keeps_a_launch_category_and_nothing_else(category):
    assert safe_summary({"launch": category})["launch"] == category


@pytest.mark.parametrize("given", ["verified", "latet_lk_" + "A" * 43, "/home/emily/repo", 1, True, {"a": 1}])
def test_anything_else_is_no_launch_category(given):
    assert safe_summary({"launch": given})["launch"] is None


async def _exchange(tmp_path: Path, launch: str) -> dict:
    """One Chat Completions request for a project folder through the real local gateway, recorder and store: with the
    key of a launch opened for that folder (``verified``), with a key Latet does not know (``unknown``: a daemon
    restarted, say), or with none; the summary the history keeps of it."""
    folder = tmp_path / "repo"
    folder.mkdir()
    sent: list[bytes] = []

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Answer(_ANSWER))

    owner = LocalLaunchKeys(b"s" * 32)
    with _sinks(tmp_path) as (mediator, recorder, store):
        mediator.engine.scopes = Projects(
            Config(ner="off", projects=[ProjectEntry(path=str(folder))]), mediator.engine.vault, files=False
        )
        configured = mediator.engine.scopes.resolve(str(folder))
        headers = {"content-type": "application/json", "x-latet-project": str(folder)}
        if launch == "verified":
            opened = owner.commit_open(
                owner.begin_open("computer"),
                Binding(str(folder.resolve()).encode(), configured.root.encode(), configured.id),
            )
            hold = owner.reserve_hold(opened["launch_id"], "computer", 0)
            hold.acknowledge()
            headers["x-latet-launch-key"] = opened["key"]
        elif launch == "unknown":
            headers["x-latet-launch-key"] = "latet_lk_" + base64.urlsafe_b64encode(b"\x07" * 32).rstrip(b"=").decode()
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token=_TOKEN,
                fixed_routes=[OPENAI],
                launch_keys=owner,
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            response = await asyncio.wait_for(
                client.post("/openai/v1/chat/completions", headers=headers, content=_BODY), _TIMEOUT
            )
            assert response.status_code == 200, response.text
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]
        detail = store.detail(row["id"])
    owner.close()
    assert len(sent) == 1
    assert "latet_lk_" not in json.dumps((row, detail)), "nothing of a key in the history"
    return row


@pytest.mark.parametrize(
    ("launch", "expected"),
    [("verified", "launched"), ("unknown", "launched-key-missing"), ("none", "claimed")],
)
async def test_the_history_says_what_a_requests_launch_was_decided_as(tmp_path, launch, expected):
    row = await _exchange(tmp_path, launch)
    assert row["launch"] == expected
