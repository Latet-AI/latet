"""D04 service witnesses: real held space apps and shared sealed AWS credentials.

The ASGI scopes represent already-authenticated gate output. These tests do not
replace the separate token/route admission or live operating-system socket tests.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.types import ASGIApp, Message, Scope

from latet.bridge import api as bridge_api
from latet.gateway.approvals import Batch
from latet.gateway.sigv4 import AwsCredentials, sign
from latet.server.live import ValkeyAwsSessions
from latet.server.tokens import route_key

from .support import URL, listening, until

pytestmark = pytest.mark.server

ENGINE = "/_latet/engine/v1"
SIGN_URL = "https://bedrock-runtime.us-east-1.amazonaws.com/model/test/invoke"
SIGN_BODY = b'{"prompt":"synthetic D04 request"}'
SIGN_TIME = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


@dataclass
class _Poll:
    task: asyncio.Task[None]
    first_body: asyncio.Event
    disconnect: asyncio.Event
    release_send: asyncio.Event
    statuses: list[int]


@contextlib.asynccontextmanager
async def _poll(
    app: ASGIApp, identity: str, token: str, suffix: str, *, body: dict[str, Any] | None = None
) -> AsyncIterator[_Poll]:
    """Keep an actual ASGI response open; every owned task is ended and awaited."""
    raw = json.dumps(body).encode() if body is not None else b""
    disconnect, first_body, release_send = asyncio.Event(), asyncio.Event(), asyncio.Event()
    statuses: list[int] = []
    supplied = False
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "POST" if body is not None else "GET",
        "scheme": "https",
        "path": f"{ENGINE}/{suffix}",
        "raw_path": f"{ENGINE}/{suffix}".encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"latet.acme.lan"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            (b"x-latet-token", token.encode()),
        ],
        "server": ("latet.acme.lan", 443),
        "client": ("127.0.0.1", 45321),
        "latet.identity": identity,
        "latet.space": f"{URL}/u/{route_key(token)}",
    }

    async def receive() -> Message:
        nonlocal supplied
        if not supplied:
            supplied = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
        elif message["type"] == "http.response.body" and message.get("body") == b"\n":
            first_body.set()
            await release_send.wait()

    task = asyncio.create_task(app(scope, receive, send), name="task23b-d04-poll")
    try:
        yield _Poll(task, first_body, disconnect, release_send, statuses)
    finally:
        disconnect.set()
        release_send.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


async def _held_positive(poll: _Poll) -> None:
    await until(lambda: poll.first_body.is_set() or poll.task.done())
    if poll.task.done():
        poll.task.result()  # A real setup/handler error must not masquerade as a lifetime RED.
    assert poll.first_body.is_set(), "the actual AWS hold never emitted its first newline"
    assert poll.statuses == [200]
    assert not poll.task.done()


def _signed_on_both(stores, session: str, expected: AwsCredentials) -> None:
    wanted = sign("POST", SIGN_URL, SIGN_BODY, expected, "us-east-1", now=SIGN_TIME)
    for store in stores:
        # Actual Valkey decryption/identity binding, followed by local signing; no AWS request.
        credentials = store.session(session)
        assert credentials == expected
        assert sign("POST", SIGN_URL, SIGN_BODY, credentials, "us-east-1", now=SIGN_TIME) == wanted


@pytest.mark.parametrize("ending", ["discard", "ear-loss"])
async def test_server_owned_close_ends_both_polls_but_shared_credentials_still_sign(instances, ending):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    enrolled = a.identities.enrol(code, "D04 laptop")
    assert enrolled is not None
    _, token, _ = enrolled
    credentials = AwsCredentials("AKIDEXAMPLED04", "synthetic-d04-secret", "synthetic-d04-session-token")
    stores = [ValkeyAwsSessions(server.bus, server.master, emily.id) for server in (a, b)]
    session = stores[0].add_session(credentials)
    async with listening(a, b):
        app = await b.spaces.app(emily)
        space = b.spaces._spaces[emily.id]
        approvals = space.runtime.mediator.approvals
        batch = await approvals.open(Batch("cc:d04", None, "claude-code", [], polled=True))
        async with (
            _poll(app, emily.id, token, f"aws-sessions/{session}/hold") as held,
            _poll(app, emily.id, token, "approvals/wait", body={"id": batch.id}) as waiting,
        ):
            await _held_positive(held)
            await until(lambda: batch.id in approvals.backend._polling or waiting.task.done())
            if waiting.task.done():
                waiting.task.result()
            assert batch.id in approvals.backend._polling and not waiting.task.done()
            assert len(space.long) == 2  # Both actual existing polls reached their held consumers.
            _signed_on_both(stores, session, credentials)
            if ending == "discard":
                b.spaces._discard(space)
            else:
                b.silenced()  # Existing real-ear-loss path; no Valkey shutdown or mocked close.
                await until(lambda: not b.listening.is_set())
            await asyncio.wait_for(asyncio.gather(held.task, waiting.task), 5)
            assert space.closed and not space.long
            assert emily.id not in b.spaces._spaces
            _signed_on_both(stores, session, credentials)


@pytest.mark.parametrize("ending", ["client-away", "credentials-ended"])
async def test_real_held_session_is_removed_on_disconnect_or_credential_end(instances, clock, monkeypatch, ending):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    enrolled = a.identities.enrol(code, "D04 laptop")
    assert enrolled is not None
    _, token, _ = enrolled
    for server in (a, b):
        monkeypatch.setattr(server, "clock", clock)
    monkeypatch.setattr(bridge_api, "SESSION_BEAT", 0)
    expires = clock.now + 60 if ending == "credentials-ended" else None
    credentials = AwsCredentials("AKIDEXAMPLED04", "synthetic-d04-secret", "synthetic-d04-token", expires)
    stores = [ValkeyAwsSessions(server.bus, server.master, emily.id, clock=clock) for server in (a, b)]
    session = stores[0].add_session(credentials)
    async with listening(a, b):
        app = await b.spaces.app(emily)
        async with _poll(app, emily.id, token, f"aws-sessions/{session}/hold") as held:
            await _held_positive(held)
            _signed_on_both(stores, session, credentials)
            if ending == "client-away":
                held.disconnect.set()  # StreamingResponse observes an actual ASGI disconnect message.
            else:
                assert expires is not None
                clock.now = expires + 1
                held.release_send.set()  # The next real Valkey-backed beat observes the credential end.
            await asyncio.wait_for(asyncio.shield(held.task), 5)
            assert not any(store.has_session(session) for store in stores)


@pytest.mark.parametrize("ending", ["token-revoked", "identity-disabled"])
async def test_identity_end_closes_real_holds_and_all_own_sessions_but_not_a_colleague(instances, ending):
    a, b = instances
    emily, code = a.identities.add_person("Emily Carter")
    enrolled = a.identities.enrol(code, "D04 laptop")
    assert enrolled is not None
    _, token, _ = enrolled
    daniel, other_code = a.identities.add_person("Daniel Okafor")
    other_enrolled = a.identities.enrol(other_code, "D04 colleague")
    assert other_enrolled is not None
    _, other_token, _ = other_enrolled
    credentials = AwsCredentials("AKIDEXAMPLED04", "synthetic-d04-secret", "synthetic-d04-token")
    others = AwsCredentials("AKIDCOLLEAGUED04", "synthetic-colleague-secret", "synthetic-colleague-token")
    stores = [ValkeyAwsSessions(server.bus, server.master, emily.id) for server in (a, b)]
    colleague = [ValkeyAwsSessions(server.bus, server.master, daniel.id) for server in (a, b)]
    sessions = [
        stores[0].add_session(credentials),
        stores[1].add_session(credentials),
        stores[0].add_session(credentials),
    ]
    kept = colleague[0].add_session(others)
    async with listening(a, b):
        here, there, other_app = await a.spaces.app(emily), await b.spaces.app(emily), await b.spaces.app(daniel)
        async with (
            _poll(here, emily.id, token, f"aws-sessions/{sessions[0]}/hold") as first,
            _poll(there, emily.id, token, f"aws-sessions/{sessions[1]}/hold") as second,
            _poll(other_app, daniel.id, other_token, f"aws-sessions/{kept}/hold") as other,
        ):
            for poll in (first, second, other):
                await _held_positive(poll)
            for session in sessions:
                _signed_on_both(stores, session, credentials)
            _signed_on_both(colleague, kept, others)
            if ending == "token-revoked":
                assert a.identities.revoke_secret(token) == emily.id
            else:
                assert a.identities.disable(emily.id)
            await until(lambda: all(not store.has_session(session) for store in stores for session in sessions))
            await asyncio.wait_for(asyncio.gather(first.task, second.task), 5)
            assert not other.task.done()
            _signed_on_both(colleague, kept, others)
