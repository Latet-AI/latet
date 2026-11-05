"""Finite lifecycle fencing and physically owned synthetic-wire controls."""

import io
import threading
from collections.abc import Callable

import pytest

from latet.launch_session import LaunchControlUnavailable, LaunchSession, claude_headers
from latet.launch_transport import LaunchWire

KEY = "latet_lk_" + "A" * 43
ID = "1" * 32
EPOCH = "2" * 32


class Child:
    code: int | None = None

    def poll(self):
        return self.code


class Hold:
    def __init__(self, generation):
        self.generation = generation
        self.closed = False

    def wait(self):
        pass

    def close(self):
        self.closed = True

    def drain(self):
        assert self.closed


class Controls:
    def __init__(self):
        self.calls = []
        self.holds = []
        self.epoch = EPOCH
        self.retirement = 0
        self.capacity = False
        self.before: Callable[[str, str, dict | None], None] | None = None

    def call(self, method, path, body):
        self.calls.append((method, path, None if body is None else dict(body)))
        if self.before is not None:
            self.before(method, path, body)
        if path.endswith("/prepare"):
            return {"epoch": self.epoch, "retirement": self.retirement}
        if path.endswith("/recover"):
            return {"launch_id": ID, "generation": 1}
        if path.endswith("/exit"):
            retired = self.retirement < (1 << 64) - 1
            return {"retired": retired, "epoch": self.epoch, "retirement": self.retirement + int(retired)}
        if path.endswith("/end"):
            return {"ended": True}
        if self.capacity:
            return {"key": None, "launch_id": None, "reason": "launch-capacity"}
        return {"key": KEY, "launch_id": ID}

    def hold(self, launch_id, generation):
        assert launch_id == ID
        held = Hold(generation + 1)
        self.holds.append(held)
        return held


def opened():
    controls, child = Controls(), Child()
    owner = LaunchSession.open(controls.call, controls.hold, {"folder": "/registered"})
    assert owner is not None and owner.generation == 1
    owner.bind_child(child)
    return controls, child, owner


def cleanup(owner, child):
    child.code = 0
    owner.mark_terminal()
    assert owner.finish()


def test_capacity_is_keyless_without_hold_or_retry():
    controls = Controls()
    controls.capacity = True
    assert LaunchSession.open(controls.call, controls.hold, {"folder": "/registered"}) is None
    assert len(controls.calls) == 1 and controls.holds == []


def test_one_recovery_uses_original_key_and_fresh_fence():
    controls, child, owner = opened()
    controls.epoch, controls.retirement = "3" * 32, 7
    try:
        assert owner.reconnect()
        method, path, payload = controls.calls[-1]
        assert method == "POST" and path.endswith("/recover")
        assert payload == {"folder": "/registered", "key": KEY, "launch_id": ID, "epoch": "3" * 32, "retirement": 7}
        assert owner.generation == 2 and controls.holds[0].closed
    finally:
        cleanup(owner, child)


@pytest.mark.parametrize("point", ["prepare", "recover"])
def test_terminal_during_actual_call_never_schedules_later_recovery(point):
    controls, child, owner = opened()
    entered, release = threading.Event(), threading.Event()
    results = []

    def before(_method, path, _body):
        if path.endswith("/" + point):
            entered.set()
            assert release.wait(2)

    controls.before = before
    worker = threading.Thread(target=lambda: results.append(owner.reconnect()))
    worker.start()
    try:
        assert entered.wait(2)
        assert owner.reconnect() is False  # at most one outstanding operation
        child.code = 0
        owner.mark_terminal()  # before close, join, disposal or EXIT
        owner.close_hold()
        assert owner.finish() is False  # still owned; no early retirement
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and results == [False]
    controls.before = None
    controls.epoch = "4" * 32  # restart while the actual child is already terminal
    assert owner.finish()
    suffixes = [path.rsplit("/", 1)[-1] for _, path, _ in controls.calls]
    assert suffixes.count("recover") == (1 if point == "recover" else 0)
    assert controls.calls[-1][2] == {"epoch": "4" * 32}
    assert owner.key == "" and len(controls.holds) == 1


def test_completed_actual_child_blocks_prepare_without_waiter_notification():
    controls, child, owner = opened()
    child.code = 0
    before = len(controls.calls)
    assert owner.reconnect() is False and len(controls.calls) == before
    owner.mark_terminal()
    assert owner.finish()


def test_live_child_shutdown_ends_but_does_not_confirm_exit():
    controls, _child, owner = opened()
    owner.mark_terminal()
    assert owner.finish() is False
    assert controls.calls[-1][1].endswith("/end")
    assert all(not path.endswith("/exit") for _, path, _ in controls.calls)
    assert owner.key == ""


def test_failed_post_is_not_replayed_inside_one_attempt():
    controls, child, owner = opened()

    def fail(_method, path, _body):
        if path.endswith("/recover"):
            raise LaunchControlUnavailable()

    controls.before = fail
    try:
        with pytest.raises(LaunchControlUnavailable):
            owner.reconnect()
        assert sum(path.endswith("/recover") for _, path, _ in controls.calls) == 1
    finally:
        controls.before = None
        cleanup(owner, child)


def test_max_retirement_stops_recovery_but_allows_terminal_exit():
    controls, child, owner = opened()
    controls.retirement = (1 << 64) - 1
    with pytest.raises(LaunchControlUnavailable):
        owner.reconnect()
    assert all(not path.endswith("/recover") for _, path, _ in controls.calls)
    child.code = 0
    owner.mark_terminal()
    assert owner.finish() is False  # EXIT attempts terminal confirmation, never wraps/retire at MAX
    assert controls.calls[-1][1].endswith("/exit")


@pytest.mark.parametrize("key", [KEY, None], ids=["owned", "keyless"])
def test_claude_carrier_preserves_ordinary_headers_and_scrubs_inherited_key(key):
    original = "X-Company: keep\nx-latet-launch-key: obsolete\nX-Duplicate: one\nX-Duplicate: two"
    merged = claude_headers(original, key)
    assert "X-Company: keep" in merged and "X-Duplicate: one\nX-Duplicate: two" in merged
    assert "obsolete" not in merged
    assert merged.count("x-latet-launch-key:") == (1 if key else 0)
    if key:
        assert merged.endswith("x-latet-launch-key: " + KEY)


def test_bad_hold_ack_is_closed_and_never_published():
    controls = Controls()
    wrong = Hold(99)
    with pytest.raises(LaunchControlUnavailable):
        LaunchSession.open(controls.call, lambda *_: wrong, {"folder": "/registered"})
    assert wrong.closed
    assert controls.calls[-1][1].endswith("/end")


class _Socket:
    def __init__(self):
        self.shutdowns = 0
        self.timeouts = []

    def shutdown(self, _how):
        self.shutdowns += 1

    def settimeout(self, value):
        self.timeouts.append(value)


class _Response:
    def __init__(self, raw=b"{}", status=200, before_read=None, content_type="application/json"):
        self.status = status
        self.content_type = content_type
        self.raw = io.BytesIO(raw)
        self.before_read = before_read
        self.closed = False

    def getheader(self, name, default=None):
        return {"content-encoding": "identity", "retry-after": "0", "content-type": self.content_type}.get(
            name, default
        )

    def read(self, limit):
        if self.before_read is not None:
            self.before_read()
        return self.raw.read(limit)

    def readline(self, limit):
        return self.raw.readline(limit)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, response, before_connect=None):
        self.response = response
        self.before_connect = before_connect
        self.sock = None
        self.requests = []
        self.closed = False

    def connect(self):
        if self.before_connect is not None:
            self.before_connect()
        self.sock = _Socket()

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _attempt(wire, results):
    try:
        wire.call("POST", "/_latet/engine/v1/launches", {"folder": "/registered"})
    except LaunchControlUnavailable as error:
        results.append(error.code)
    else:
        results.append("returned")


def test_wire_old_exchange_stays_owned_until_actual_read_returns(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    connections = []

    def delayed():
        entered.set()
        assert release.wait(2)

    def connect(*_args, **_kwargs):
        response = _Response(before_read=delayed if not connections else None)
        one = _Connection(response)
        connections.append(one)
        return one

    monkeypatch.setattr("latet.launch_transport.http.client.HTTPConnection", connect)
    wire = LaunchWire("http://127.0.0.1:5283", "synthetic-control-token", None, lambda: None)
    results = []
    worker = threading.Thread(target=_attempt, args=(wire, results))
    worker.start()
    try:
        assert entered.wait(2)
        original = wire._active
        assert original is not None
        wire.interrupt()
        assert wire._active is original and not original.settled.is_set()
        with pytest.raises(LaunchControlUnavailable):
            wire.call("GET", "/_latet/engine/v1/launches/prepare", None)
        assert len(connections[0].requests) == 1 and connections[1].requests == []
        assert not connections[0].response.closed and results == []
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and results == ["launch-unavailable"]
    assert connections[0].closed and connections[0].response.closed and original.settled.is_set()
    assert wire._active is None
    assert wire.call("GET", "/_latet/engine/v1/launches/prepare", None) == {}
    assert len(connections[-1].requests) == 1


def test_wire_post_503_is_one_exchange_without_retry(monkeypatch):
    connections = []

    def connect(*_args, **_kwargs):
        one = _Connection(_Response(b'{"error":"launch-unavailable"}', status=503))
        connections.append(one)
        return one

    monkeypatch.setattr("latet.launch_transport.http.client.HTTPConnection", connect)
    wire = LaunchWire("http://127.0.0.1:5283", "synthetic-control-token", None, lambda: None)
    with pytest.raises(LaunchControlUnavailable, match=r"^launch-unavailable$"):
        wire.call("POST", "/_latet/engine/v1/launches/recover", {"folder": "/registered"})
    assert len(connections) == 1 and len(connections[0].requests) == 1
    assert connections[0].closed and wire._active is None


def test_wire_cancel_during_connect_refuses_before_request_bytes(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def delayed():
        entered.set()
        assert release.wait(2)

    connection = _Connection(_Response(), before_connect=delayed)
    monkeypatch.setattr("latet.launch_transport.http.client.HTTPConnection", lambda *_args, **_kwargs: connection)
    selected = []
    wire = LaunchWire("http://127.0.0.1:5283", "synthetic-control-token", None, lambda: selected.append(True))
    results = []
    worker = threading.Thread(target=_attempt, args=(wire, results))
    worker.start()
    try:
        assert entered.wait(2)
        original = wire._active
        assert original is not None
        wire.interrupt()
        assert wire._active is original and not original.settled.is_set()
        assert connection.requests == []
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and results == ["launch-unavailable"]
    assert connection.requests == [] and connection.closed
    assert connection.sock is not None and connection.sock.shutdowns == 1
    assert original.settled.is_set() and wire._active is None
    assert selected == [True]


def test_wire_absolute_deadline_interrupts_python_header_wait(monkeypatch):
    stopped = threading.Event()
    response = _Response()

    class DeadlineSocket(_Socket):
        def shutdown(self, how):
            super().shutdown(how)
            stopped.set()

    class DeadlineConnection(_Connection):
        def connect(self):
            self.sock = DeadlineSocket()

        def getresponse(self):
            # No idle-timeout exception: only the actual exchange watchdog can
            # release this synthetic Python header wait.
            assert stopped.wait(2)
            return response

    connection = DeadlineConnection(response)
    monkeypatch.setattr("latet.launch_transport.http.client.HTTPConnection", lambda *_args, **_kwargs: connection)
    wire = LaunchWire("http://127.0.0.1:5283", "synthetic-control-token", None, lambda: None, _deadline=0.05)
    with pytest.raises(LaunchControlUnavailable):
        wire.call("POST", "/_latet/engine/v1/launches", {"folder": "/registered"})
    assert stopped.is_set() and len(connection.requests) == 1
    assert connection.closed and response.closed and wire._active is None


def test_wire_valid_ack_joins_deadline_before_indefinite_hold(monkeypatch):
    response = _Response(b'{"held":true,"generation":1}\n', content_type="application/x-ndjson")
    connection = _Connection(response)
    monkeypatch.setattr("latet.launch_transport.http.client.HTTPConnection", lambda *_args, **_kwargs: connection)
    wire = LaunchWire("http://127.0.0.1:5283", "synthetic-control-token", None, lambda: None)
    held = wire.hold(ID, 0)
    exchange = wire._active
    assert exchange is not None and not exchange._timed
    assert exchange._watcher is not None and not exchange._watcher.is_alive()
    assert connection.sock is not None and connection.sock.timeouts == [None]
    assert held.generation == 1 and not exchange.settled.is_set()
    held.wait()  # actual synthetic EOF, then actual reader-owned release
    held.close()
    held.drain()
    assert exchange.settled.is_set() and wire._active is None and connection.closed


def test_run_owned_defers_repeated_join_interrupts_until_monitor_settles(monkeypatch):
    from latet import launch_session

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    controls = Controls()
    threads = []
    attempts = []
    outer_cleanup = []

    class BlockingHold(Hold):
        def wait(self):
            entered.set()
            try:
                assert release.wait(2)
            finally:
                finished.set()

    held = BlockingHold(1)
    owner = LaunchSession.open(controls.call, lambda *_args: held, {"folder": "/registered"})
    assert owner is not None

    class CompletedChild(Child):
        def wait(self):
            assert entered.wait(2)
            self.code = 0
            return 0

    child = CompletedChild()
    real_thread = threading.Thread

    class InterruptedJoin(real_thread):
        def join(self, timeout=None):
            assert owner._terminal
            attempts.append(None)
            if len(attempts) <= 2:
                assert not finished.is_set() and not owner._disposed
                raise KeyboardInterrupt
            release.set()
            super().join(2)
            assert not self.is_alive()

    def thread_factory(*args, **kwargs):
        thread = InterruptedJoin(*args, **kwargs)
        threads.append(thread)
        return thread

    def before(method, path, body):
        if path.endswith(("/prepare", "/exit")):
            assert finished.is_set() and not threads[0].is_alive()

    controls.before = before
    monkeypatch.setattr(launch_session.threading, "Thread", thread_factory)
    monkeypatch.setattr(launch_session.subprocess, "Popen", lambda *_args, **_kwargs: child)

    def call_with_cleanup() -> None:
        try:
            launch_session.run_owned(owner, ["synthetic-child"], {})
        finally:
            # This is the caller's private-file cleanup position.
            outer_cleanup.append(finished.is_set() and not threads[0].is_alive())

    try:
        with pytest.raises(KeyboardInterrupt):
            call_with_cleanup()
        assert len(attempts) == 3 and outer_cleanup == [True]
        assert owner._disposed
        assert sum(path.endswith("/exit") for _, path, _ in controls.calls) == 1
        assert not any(path.endswith("/recover") for _, path, _ in controls.calls)
    finally:
        release.set()
        for thread in threads:
            real_thread.join(thread, 2)
            assert not thread.is_alive()
