"""Task 23B's actual gate and space callers; prepared without running or collecting tests.

The stores/authentication are memory stand-ins. Admission, ASGI handoff, refusal, and cancellation
are the production consumers. A timeout is only a watchdog; queue/events establish every ordering.
"""

import asyncio
import contextlib
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.responses import Response

from latet.api.rest import API, COOKIE
from latet.gateway.launch_keys import HOLD_BYTES
from latet.server import app as server_app
from latet.server import spaces
from latet.server.grants import DEFAULT, GATEWAY, Grant, bridge_needs
from latet.server.identities import Identity
from latet.server.launch_controls import LAUNCH_HOLD_TICKET, LaunchHoldAdmission, LaunchHoldTicket
from latet.server.launch_mutations import LAUNCH_MUTATION, LaunchMutations
from latet.server.logs import REFUSED
from latet.server.tokens import ROUTE_LENGTH

URL = "https://latet.acme.lan"
ENGINE = "/_latet/engine/v1"
WAIT = f"{ENGINE}/approvals/wait"
HOLD = f"{ENGINE}/aws-sessions/session-one/hold"
LAUNCH = f"{ENGINE}/launches"
LAUNCH_HOLD = f"{LAUNCH}/{'a' * 32}/hold"
STREAM = f"{API}/stream"
APPROVALS_STREAM = f"{API}/approvals/stream"
BOUND = 2


def _identity(name):
    return Identity(name, "Test person", None, "person", False, 0, None, "local", None)


def _granted(identity):
    """What a person's token grants: all three permissions, as every person's token holds, and it serves agents."""
    return Grant(identity, "t_" + identity.id.ljust(10, "x"), DEFAULT, agents=True) if identity is not None else None


class _Call:
    def __init__(self, gate, path, identity="one", method="GET", kind="http", query_string=b""):
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
            "query_string": query_string,
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
        self.carriers = []
        self.people = {name: _identity(name) for name in ("one", "two")}
        self.build_error = None
        self.app_error = None
        self.hold_build = False
        self.server = SimpleNamespace(
            settings=SimpleNamespace(url=URL, requests_in_flight=16),
            identities=SimpleNamespace(remembered_grant=lambda **kw: _granted(self.people[kw["route"].rstrip("x")])),
            spaces=SimpleNamespace(app=self.app, launch_fits=lambda: True),
            launch_holds=LaunchHoldAdmission(),
            launch_mutations=LaunchMutations(),
        )
        self.mutation_baseline = self.server.launch_mutations.retained_bytes
        self.gate = server_app.ServerGate(self.unexpected, cast(server_app.Server, self.server))
        monkeypatch.setattr(self.gate, "_session_held", self.people.get)

        async def owner(_server, token, _bucket):
            return _granted(self.people.get(token))

        monkeypatch.setattr(server_app, "secret_grant", owner)

    async def unexpected(self, scope, receive, send):
        pytest.fail("request bypassed the identity's space")

    async def app(self, identity, *, launch_carrier=None):
        """``spaces.app``'s signature: a destination request is handed its launch-key carrier (none here: [])."""
        self.carriers.append(launch_carrier)
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
        assert all(LAUNCH_MUTATION not in call.scope for call in self.calls)
        self.server.launch_mutations.require_drained()
        assert self.server.launch_mutations.retained_bytes == self.mutation_baseline


@pytest.fixture
async def gate(monkeypatch):
    harness = _Gate(monkeypatch)
    try:
        yield harness
    finally:
        await harness.close()
        assert not any(_surface_counts(harness))
        assert harness.server.launch_holds.count == 0


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


def test_server_launch_controls_require_gateway_permissions():
    launch = LAUNCH_HOLD.removesuffix("/hold")
    controls = (LAUNCH, f"{LAUNCH}/prepare", f"{LAUNCH}/recover", LAUNCH_HOLD, f"{launch}/end", f"{launch}/exit")
    assert {path: bridge_needs(path) for path in controls} == dict.fromkeys(controls, GATEWAY)


def test_server_launch_classifiers_keep_existing_and_malformed_controls():
    assert bridge_needs(HOLD) == GATEWAY
    assert bridge_needs(f"{ENGINE}/ping") == frozenset()
    malformed = f"{LAUNCH}/{'A' * 32}/hold"
    for path in (f"{ENGINE}/unrecognized", malformed, f"{LAUNCH}/{'a' * 31}/hold", f"{LAUNCH_HOLD}/extra"):
        assert bridge_needs(path) == DEFAULT
    assert server_app.computer_call(LAUNCH, "POST", LAUNCH.encode())
    assert server_app.computer_call(LAUNCH_HOLD, "GET", LAUNCH_HOLD.encode())
    assert not server_app.computer_call(LAUNCH_HOLD, "GET", LAUNCH_HOLD.replace("/hold", "/%68old").encode())
    assert not spaces._long({"type": "http", "path": malformed, "method": "GET"})
    assert not spaces._long({"type": "http", "path": LAUNCH_HOLD, "method": "POST"})


def test_canonical_launch_hold_is_long_for_space_disposal():
    assert spaces._long({"type": "http", "path": LAUNCH_HOLD, "method": "GET"})


async def test_canonical_launch_hold_reaches_space_outside_ordinary_bridge_and_long_budgets(gate):
    mask = f"{ENGINE}/mask"
    for _ in range(16):
        await gate.held(mask, method="POST")
    await gate.refused(mask, method="POST")
    await gate.refused(f"{LAUNCH}/{'A' * 32}/hold")
    await gate.held(HOLD)
    assert _surface_counts(gate) == ({"one": 1}, {}, {}, {"one": 16})

    launch = await gate.held(LAUNCH_HOLD, query_string=b"generation=0")
    assert launch.reads == 1
    assert gate.requested.qsize() == 18
    assert _surface_counts(gate) == ({"one": 1}, {}, {}, {"one": 16})


async def test_launch_hold_cap_is_shared_across_gates_and_identities_on_one_instance(gate, monkeypatch):
    admission = gate.server.launch_holds
    empty = admission.retained_bytes
    for _ in range(128):
        await gate.held(LAUNCH_HOLD, query_string=b"generation=0")
    gate.gate = server_app.ServerGate(gate.unexpected, cast(server_app.Server, gate.server))
    for _ in range(128):
        await gate.held(LAUNCH_HOLD, identity="two", query_string=b"generation=0")
    assert admission.count == 256
    assert admission.retained_bytes == empty + 256 * HOLD_BYTES
    await gate.refused(LAUNCH_HOLD, identity="two", query_string=b"generation=0")
    assert gate.requested.qsize() == 256
    assert not any(_surface_counts(gate))

    other = _Gate(monkeypatch)
    try:
        await other.held(LAUNCH_HOLD, query_string=b"generation=0")
        assert other.server.launch_holds.count == 1 and admission.count == 256
    finally:
        await other.close()
        assert other.server.launch_holds.count == 0


async def test_launch_hold_cancelled_space_lookup_refunds_unadopted_ticket(gate):
    gate.hold_build = True
    admission = gate.server.launch_holds
    empty = admission.retained_bytes
    call = gate.call(LAUNCH_HOLD, query_string=b"generation=0")
    assert await asyncio.wait_for(gate.requested.get(), BOUND) == "one"
    assert admission.count == 1 and admission.retained_bytes == empty + HOLD_BYTES
    call.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call.task, BOUND)
    assert admission.count == 0 and admission.retained_bytes == empty
    assert call.reads == 0 and gate.entered.empty()
    gate.hold_build = False
    await gate.held(LAUNCH_HOLD, query_string=b"generation=0")
    assert admission.count == 1


async def test_adopted_launch_hold_stays_charged_until_independent_drain(gate):
    adopted, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tickets = []
    drains = []
    admission = gate.server.launch_holds
    empty = admission.retained_bytes

    async def drain(ticket):
        try:
            draining.set()
            await release.wait()
        finally:
            ticket.close()

    async def consume(scope, receive, send):
        ticket = scope[LAUNCH_HOLD_TICKET]
        assert isinstance(ticket, LaunchHoldTicket)
        assert ticket.adopt()
        tickets.append(ticket)
        adopted.set()
        try:
            await gate.release.wait()
        finally:
            task = asyncio.create_task(drain(ticket))
            drains.append(task)
            await asyncio.shield(task)

    async def app(identity):
        return consume

    gate.server.spaces.app = app
    call = gate.call(LAUNCH_HOLD, query_string=b"generation=0")
    try:
        await asyncio.wait_for(adopted.wait(), BOUND)
        assert admission.count == 1
        call.task.cancel()
        await asyncio.wait_for(draining.wait(), BOUND)
        assert admission.retained_bytes == empty + HOLD_BYTES
        call.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(call.task, BOUND)
        assert admission.count == 1 and admission.retained_bytes == empty + HOLD_BYTES
        release.set()
        await asyncio.wait_for(drains[0], BOUND)
        assert admission.count == 0 and admission.retained_bytes == empty
        successor = admission.reserve()
        assert successor is not None
        try:
            tickets[0].close()
            assert admission.count == 1
        finally:
            successor.release_unadopted()
    finally:
        release.set()
        if not call.task.done():
            call.task.cancel()
        await asyncio.gather(call.task, *drains, return_exceptions=True)


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
    assert gate.carriers[-1] == []  # a destination request, with no launch key: its carrier is empty
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


async def test_a_bridge_call_never_hands_the_launch_carrier_to_the_space(gate):
    """The review's Minor 3: on a server the carrier header is taken off every call of an identity's address, its
    bridge's too, before the space gets it, as a computer takes it off every path."""
    seen = []
    serve = gate.serve

    async def recording(scope, receive, send):
        seen.append([name.lower() for name, _ in scope["headers"]])
        await serve(scope, receive, send)

    gate.serve = recording
    call = gate.call(f"{ENGINE}/mask", method="POST")
    call.scope["headers"].append((b"x-latet-launch-key", b"latet_lk_" + b"A" * 43))  # before its task first runs
    assert await asyncio.wait_for(gate.entered.get(), BOUND) == ("one", f"{ENGINE}/mask")
    assert seen and all(b"x-latet-launch-key" not in names for names in seen), seen


async def test_a_launch_control_takes_its_slot_only_once_authenticated(gate, monkeypatch):
    """The review's Minor 4: an unauthenticated client sending launch controls to well-formed addresses holds none of
    the instance's launch-control slots while its token is checked; an authenticated one takes its slot."""
    mutations = gate.server.launch_mutations
    checked = asyncio.Event()
    release = asyncio.Event()
    taken: list[int] = []
    real = server_app.secret_grant

    async def slow_token(server, token, bucket):
        taken.append(mutations.retained_bytes - gate.mutation_baseline)
        checked.set()
        await release.wait()
        return await real(server, token, bucket)

    monkeypatch.setattr(server_app, "secret_grant", slow_token)
    call = gate.call(LAUNCH, method="POST")
    await asyncio.wait_for(checked.wait(), BOUND)
    assert taken == [0], "no slot is held while the token is checked"
    release.set()
    assert await asyncio.wait_for(gate.entered.get(), BOUND) == ("one", LAUNCH)
    assert mutations.retained_bytes > gate.mutation_baseline, "authenticated, it holds its slot in its space"
    gate.release.set()
    await asyncio.wait_for(call.task, BOUND)
    assert mutations.retained_bytes == gate.mutation_baseline


async def test_a_server_refuses_the_path_form_of_a_launch_key(gate):
    """Root's ruling I-4 on P2-6a: the path form (``/_k/<key>/...``) is a computer's alone; a server's gate refuses it
    before any space is asked, its key read nowhere."""
    key = "latet_lk_" + "A" * 43
    call = gate.call("/v1/chat/completions", method="POST")
    route = "one".ljust(ROUTE_LENGTH, "x")
    path = f"/u/{route}/_k/{key}/v1/chat/completions"
    call.scope["path"], call.scope["raw_path"] = path, path.encode()  # before its task first runs
    before = gate.requested.qsize()
    answer = await call.answer()
    assert answer["status"] == 400 and gate.requested.qsize() == before
    body = b"".join(message.get("body", b"") for message in call.messages if message["type"] == "http.response.body")
    assert b"request launch authority refused" in body and key.encode() not in body
