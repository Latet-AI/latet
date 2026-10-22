"""The existing Bridge hold response under the existing space cancellation consumer.

These unit witnesses do not establish Valkey credential lifetime/signing or cross-instance revocation.
Those named service witnesses remain in the evidence manifest, pending the runtime seat and Task 6.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from latet.bridge import api as bridge_api
from latet.server import spaces


class _Credentials:
    def __init__(self):
        self.sessions = {"owned-session"}
        self.dropped = []

    def has_session(self, session):
        return session in self.sessions

    def drop_session(self, session):
        self.dropped.append(session)
        self.sessions.discard(session)


@pytest.mark.parametrize("ending", ["space", "client", "expired"])
async def test_real_bridge_hold_distinguishes_space_end_client_loss_and_credential_end(monkeypatch, ending):
    aws = _Credentials()
    bridge = bridge_api.Bridge(SimpleNamespace(engine=None), "", aws=aws, on_server=SimpleNamespace(identity="one"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/_latet/engine/v1/aws-sessions/owned-session/hold",
        "headers": [],
        "latet.identity": "one",
        "path_params": {"id": "owned-session"},
    }
    sent, continue_send = asyncio.Event(), asyncio.Event()

    async def receive():
        await continue_send.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sent.set()
            await continue_send.wait()

    async def endpoint(inner, receiving, sending):
        response = await bridge.hold_aws_session(Request(inner, receiving))
        assert response.status_code == 200
        await response(inner, receiving, sending)

    monkeypatch.setattr(bridge_api, "SESSION_BEAT", 0)
    space = spaces.Space(SimpleNamespace(id="one"), None, endpoint, None, contextlib.AsyncExitStack(), 0)
    task = asyncio.create_task(spaces._held(space, scope, receive, send))
    try:
        await asyncio.wait_for(sent.wait(), 2)
        if ending == "space":
            space.end_long()
        elif ending == "client":
            task.cancel()
        else:
            aws.sessions.clear()
            continue_send.set()
        if ending == "client":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            await asyncio.wait_for(task, 2)
        assert not space.long and not space.ended
        assert aws.has_session("owned-session") is (ending == "space")
        assert aws.dropped == ([] if ending == "space" else ["owned-session"])
    finally:
        continue_send.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
