"""Task 23B's actual gate and space callers; prepared without running or collecting tests.

The stores/authentication are memory stand-ins. Admission, ASGI handoff, refusal, and cancellation
are the production consumers. A timeout is only a watchdog; queue/events establish every ordering.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from starlette.responses import Response

from latet.api.rest import API, COOKIE
from latet.server import app as server_app
from latet.server import spaces
from latet.server.grants import DEFAULT, Grant
from latet.server.identities import Identity
from latet.server.logs import REFUSED
from latet.server.tokens import ROUTE_LENGTH

URL = "https://latet.acme.lan"
ENGINE = "/_latet/engine/v1"
WAIT = f"{ENGINE}/approvals/wait"
HOLD = f"{ENGINE}/aws-sessions/session-one/hold"
STREAM = f"{API}/stream"
APPROVALS_STREAM = f"{API}/approvals/stream"
BOUND = 2


def _identity(name):
    return Identity(name, "Test person", None, "person", False, 0, None, "local", None)


def _granted(identity):
    """What a person's token grants: all three permissions, as every person's token holds, and it serves agents."""
    return Grant(identity, "t_" + identity.id.ljust(10, "x"), DEFAULT, agents=True) if identity is not None else None


class _Call:
    def __init__(self, gate, path, identity="one", method="GET", kind="http"):
        route = identity.ljust(ROUTE_LENGTH, "x")
        addressed = path.startswith(ENGINE) or path.startswith("/v1/") or kind == "websocket"
        outer = f"/u/{route}{path}" if addressed else path
        self.scope = {
            "type": kind,
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": method,
            "scheme": "https",
            "path": outer,
            "raw_path": outer.encode(),
            "query_string": b"",
            "headers": [
                (b"host", b"latet.acme.lan"),
                (b"cookie", f"{COOKIE}={identity}".encode()),
                (b"x-latet-token", identity.encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("latet.acme.lan", 443),
        }
        self.messages, self.reads = [], 0
        self.task = asyncio.create_task(gate(self.scope, self.receive, self.send))

    async def receive(self):
        self.reads += 1
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(self, message):
        self.messages.append(message)

    async def answer(self):
        await asyncio.wait_for(self.task, BOUND)
        starts = [message for message in self.messages if message["type"] == "http.response.start"]
        assert len(starts) == 1
        return starts[0]


class _Gate:
    def __init__(self, monkeypatch):
        self.entered, self.requested = asyncio.Queue(), asyncio.Queue()
        self.release = asyncio.Event()
        self.calls = []
        self.people = {name: _identity(name) for name in ("one", "two")}
        self.build_error = None
        self.app_error = None
        self.hold_build = False
        self.server = SimpleNamespace(
            settings=SimpleNamespace(url=URL, requests_in_flight=16),
            identities=SimpleNamespace(remembered_grant=lambda **kw: _granted(self.people[kw["route"].rstrip("x")])),
            spaces=SimpleNamespace(app=self.app),
        )
        self.gate = server_app.ServerGate(self.unexpected, self.server)
        monkeypatch.setattr(self.gate, "_session_held", self.people.get)

        async def owner(_server, token, _bucket):
            return _granted(self.people.get(token))

        monkeypatch.setattr(server_app, "secret_grant", owner)

    async def unexpected(self, scope, receive, send):
        pytest.fail("request bypassed the identity's space")

    async def app(self, identity):
        self.requested.put_nowait(identity.id)
        if self.hold_build:
            await self.release.wait()
        if self.build_error is not None:
            raise self.build_error
        return self.serve

    async def serve(self, scope, receive, send):
        await receive()
        self.entered.put_nowait((scope["latet.identity"], scope["path"]))
        await self.release.wait()
        if self.app_error is not None:
            raise self.app_error
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})
        else:
            await Response(status_code=204)(scope, receive, send)

    def call(self, path, **kwargs):
        call = _Call(self.gate, path, **kwargs)
        self.calls.append(call)
        return call

    async def held(self, path, **kwargs):
        call = self.call(path, **kwargs)
        expected = (kwargs.get("identity", "one"), path)
        arrival = asyncio.create_task(self.entered.get())
        try:
            done, _ = await asyncio.wait((arrival, call.task), timeout=BOUND, return_when=asyncio.FIRST_COMPLETED)
            if call.task in done:
                call.task.result()
                pytest.fail(f"positive request ended before holding: messages={call.messages!r}")
            if arrival not in done:
                raise TimeoutError("owned positive request did not reach its consumer")
            entered = arrival.result()
        except TimeoutError as exc:
            if call.task.done():
                call.task.result()
            exc.add_note(
                f"positive setup: messages={call.messages!r}; reads={call.reads}; requested={self.requested.qsize()}"
            )
            raise
        finally:
            arrival.cancel()
            await asyncio.gather(arrival, return_exceptions=True)
        assert entered == expected
        return call

    async def refused(self, path, **kwargs):
        before = self.requested.qsize()
        call = self.call(path, **kwargs)
        checkpoint = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
        await checkpoint
        assert self.requested.qsize() == before
        assert call.reads == 0
        _refused(call, await call.answer())

    async def close(self):
        self.release.set()
        for call in self.calls:
            if not call.task.done():
                call.task.cancel()
        await asyncio.gather(*(call.task for call in self.calls), return_exceptions=True)


@pytest.fixture
async def gate(monkeypatch):
    harness = _Gate(monkeypatch)
    try:
        yield harness
    finally:
        await harness.close()
        assert not any(_surface_counts(harness))


def _surface_counts(harness):
    gate = harness.gate
    return gate._long, gate._streams, gate._dashboard_in_flight, gate._bridge_in_flight


def _refused(call, answer):
    assert answer["status"] == 429
    assert dict(answer["headers"])[b"retry-after"] == str(server_app.CROWDED_RETRY_AFTER).encode()
    assert call.scope[REFUSED] is True
    assert call.reads == 0


@pytest.mark.parametrize(
    ("path", "method", "kind", "expected"),
    [
        (STREAM, "GET", "http", True),
        (STREAM, "HEAD", "http", True),
        (APPROVALS_STREAM, "POST", "http", True),
        (APPROVALS_STREAM, "HEAD", "http", True),
        (WAIT, "POST", "http", True),
        (WAIT, "GET", "http", False),
        (HOLD, "GET", "http", True),
        (HOLD, "POST", "http", False),
        (f"{ENGINE}/aws-sessions/a/b/hold", "GET", "http", False),
        (f"{ENGINE}/aws-sessions//hold", "GET", "http", False),
        (f"{ENGINE}/aws-sessions/a/hold/extra", "GET", "http", False),
        (f"{ENGINE}/ping", "GET", "http", False),
        (f"{API}/stats", "GET", "http", False),
        ("/v1/realtime", "GET", "websocket", True),
    ],
)
def test_one_existing_space_classifier_recognizes_exact_long_routes(path, method, kind, expected):
    # WebSockets remain long for Space.long; only the gate's HTTP long budget excludes them.
    assert spaces._long({"type": kind, "path": path, "method": method}) is expected


@pytest.mark.parametrize(
    ("path", "method"),
    [(WAIT, "POST"), (HOLD, "GET"), (STREAM, "GET"), (STREAM, "HEAD"), (APPROVALS_STREAM, "HEAD")],
)
async def test_sixty_fifth_long_request_is_refused_before_space_or_body(gate, caplog, path, method):
    for _ in range(64):
        await gate.held(WAIT, method="POST")
    before = gate.requested.qsize()
    for _ in range(2):
        await gate.refused(path, method=method)
    assert gate.requested.qsize() == before
    lines = [record for record in caplog.records if record.name == server_app.__name__]
    assert len(lines) == 1
    assert "64" in lines[0].getMessage()


@pytest.mark.parametrize("path", [STREAM, APPROVALS_STREAM])
async def test_four_dashboard_streams_share_a_cap_but_bridge_and_another_identity_continue(gate, path):
    for stream in (STREAM, APPROVALS_STREAM, STREAM, APPROVALS_STREAM):
        await gate.held(stream)
    before = gate.requested.qsize()
    await gate.refused(path, method="HEAD")
    assert gate.requested.qsize() == before
    await gate.held(WAIT, method="POST")
    await gate.held(STREAM, identity="two")


@pytest.mark.parametrize(("path", "method"), [(f"{API}/stats", "GET"), (f"{ENGINE}/mask", "POST")])
async def test_seventeenth_surface_request_is_refused_before_space_or_body(gate, path, method):
    for _ in range(16):
        await gate.held(path, method=method)
    before = gate.requested.qsize()
    await gate.refused(path, method=method)
    assert gate.requested.qsize() == before
    await gate.held(path, method=method, identity="two")
    # The other surface and the destination each retain their own allowance.
    other = f"{ENGINE}/mask" if path.startswith(API) else f"{API}/stats"
    await gate.held(other, method="POST" if other.startswith(ENGINE) else "GET")
    await gate.held("/v1/messages", method="POST")
    await gate.held(WAIT, method="POST")


async def test_websockets_use_their_own_cap_and_leave_the_http_long_budget_free(gate):
    for _ in range(64):
        await gate.held("/v1/realtime", kind="websocket")
    for _ in range(64):
        await gate.held(WAIT, method="POST")
    assert len(gate.gate._sockets) == 1
    await gate.refused(WAIT, method="POST")


async def test_long_admission_happens_before_awaiting_the_space(gate):
    gate.hold_build = True
    for _ in range(64):
        gate.call(WAIT, method="POST")
        assert await asyncio.wait_for(gate.requested.get(), BOUND) == "one"
    await gate.refused(HOLD)
    assert gate.requested.empty()


@pytest.mark.parametrize(
    ("path", "method", "cap"),
    [(WAIT, "POST", 64), (STREAM, "GET", 4), (f"{API}/stats", "GET", 16), (f"{ENGINE}/mask", "POST", 16)],
)
async def test_cancelled_request_releases_its_surface_admission(gate, path, method, cap):
    calls = [await gate.held(path, method=method) for _ in range(cap)]
    calls[0].task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await calls[0].task
    await gate.held(path, method=method)
    await gate.refused(path, method=method)


@pytest.mark.parametrize("error", [spaces.SpaceUnavailable(), spaces.IdentityUnavailable(), RuntimeError()])
async def test_failed_space_lookup_does_not_leak_long_admission(gate, error):
    gate.build_error = error
    for _ in range(65):
        call = gate.call(WAIT, method="POST")
        assert (await call.answer())["status"] == (401 if isinstance(error, spaces.IdentityUnavailable) else 503)
        assert not any(_surface_counts(gate))
    gate.build_error = None
    await gate.held(WAIT, method="POST")


async def test_cancellation_while_awaiting_space_releases_long_admission(gate):
    gate.hold_build = True
    calls = []
    for _ in range(64):
        call = gate.call(WAIT, method="POST")
        calls.append(call)
        assert await asyncio.wait_for(gate.requested.get(), BOUND) == "one"
    for call in calls:
        call.task.cancel()
    await asyncio.gather(*(call.task for call in calls), return_exceptions=True)
    assert not any(_surface_counts(gate))
    gate.hold_build = False
    await gate.held(WAIT, method="POST")


async def test_admission_is_owned_by_each_gate_instance(gate, monkeypatch):
    second = _Gate(monkeypatch)
    try:
        for _ in range(64):
            await gate.held(WAIT, method="POST")
        await second.held(WAIT, method="POST")
        await gate.held(WAIT, method="POST", identity="two")
    finally:
        await second.close()


@pytest.mark.parametrize(
    ("path", "method", "cap"),
    [(WAIT, "POST", 64), (STREAM, "GET", 4), (f"{API}/stats", "GET", 16), (f"{ENGINE}/mask", "POST", 16)],
)
@pytest.mark.parametrize("failed", [False, True])
async def test_normal_and_failed_apps_release_admission(gate, path, method, cap, failed):
    gate.release.set()
    if failed:
        gate.app_error = ValueError("owned failure")
    for _ in range(cap + 1):
        call = gate.call(path, method=method)
        if failed:
            with pytest.raises(ValueError, match="owned failure"):
                await asyncio.wait_for(call.task, BOUND)
        else:
            assert (await call.answer())["status"] == 204
        assert not any(_surface_counts(gate))


@pytest.mark.parametrize("path", [WAIT, HOLD, STREAM, APPROVALS_STREAM])
async def test_existing_held_caller_closes_and_releases_when_space_ends(path):
    entered, finish = asyncio.Event(), asyncio.Event()
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        entered.set()
        await finish.wait()

    async def receive():
        await finish.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    space = spaces.Space(_identity("one"), None, app, None, contextlib.AsyncExitStack(), 0)
    call = asyncio.create_task(
        spaces._held(space, {"type": "http", "method": "POST" if path == WAIT else "GET", "path": path}, receive, send)
    )
    try:
        await asyncio.wait_for(entered.wait(), BOUND)
        assert call in space.long
        space.end_long()
        await asyncio.wait_for(call, BOUND)
        assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert not space.long and not space.ended
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
