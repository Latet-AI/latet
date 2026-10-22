"""The AWS credentials of an `latet claude` session end with that session, even when it is killed, and at
their own expiry: a real server on a real port, real sockets, and a real process that is killed."""

import base64
import http.client
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import uvicorn

from latet import wrappers
from latet.bridge import api
from latet.config import Config, IdentityConfig
from latet.gateway.app import create_app
from latet.gateway.aws import AwsCredentialStore, AwsError
from latet.gateway.sigv4 import AwsCredentials
from latet.runtime import make_runtime

from ..conftest import ROOT_KEY

pytestmark = pytest.mark.e2e

BEAT = 0.1  # seconds between the bytes a hold gets here (15 in the daemon)
THEN = 1_790_000_000.0  # the daemon's clock, which a test moves
WITHIN = 20.0  # the daemon notices from its own thread: poll this long, never sleep this long


class Daemon:
    """The gateway as `latet serve` runs it, with the store's clock in the test's hands."""

    def __init__(
        self,
        server: uvicorn.Server,
        thread: threading.Thread,
        port: int,
        token: str,
        aws: AwsCredentialStore,
        now: list[float],
    ) -> None:
        self.server, self.thread, self.port, self.token, self.aws, self.now = server, thread, port, token, aws, now
        self.config = Config(port=port)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def add(self, **more) -> str:
        """A session handed over as `latet claude` does it: through the wrapper's own call."""
        return wrappers.aws_session(AwsCredentials("AKIDSESSION", "s3cr3t", "token", **more), self.config)

    def hold(self, session: str, headers: dict[str, str] | None = None):
        """The hold, as the wrapper asks for it: the connection and its answer, still open."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"x-latet-token": self.token} if headers is None else headers
        conn.request("GET", f"/_latet/engine/v1/aws-sessions/{session}/hold", headers=headers)
        return conn, conn.getresponse()

    def raw_hold(self, session: str) -> socket.socket:
        """The hold's request written on a bare socket, with no one reading the answer."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        path = f"/_latet/engine/v1/aws-sessions/{session}/hold"
        host, token = f"127.0.0.1:{self.port}", self.token
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nx-latet-token: {token}\r\n\r\n".encode())
        return sock

    def holds(self, session: str) -> bool:
        """Whether the daemon still has the session's credentials: `session()` answers or raises."""
        try:
            self.aws.session(session)
        except AwsError:  # aws-session-gone
            return False
        return True


def _soon(condition, within: float = WITHIN) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "SESSION_BEAT", BEAT)
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))  # where the wrapper reads the bridge token
    runtime = make_runtime(
        Config(identity=IdentityConfig(enabled=False)), data=tmp_path / "data", config_file=tmp_path / "config.yaml"
    )
    now = [THEN]
    runtime.gateway.aws = AwsCredentialStore(clock=lambda: now[0])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        runtime.gateway.port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(runtime.gateway),
            host="127.0.0.1",
            port=runtime.gateway.port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    running = Daemon(server, thread, runtime.gateway.port, runtime.gateway.bridge_token or "", runtime.gateway.aws, now)
    yield running
    running.stop()
    runtime.close()


def test_a_session_lasts_as_long_as_its_hold_is_open(daemon):
    session = daemon.add()
    conn, answer = daemon.hold(session)
    try:
        assert answer.status == 200 and answer.read(1) == b"\n"
        assert [answer.read(1) for _ in range(5)] == [b"\n"] * 5  # beat after beat, still held
        assert daemon.aws.session(session).access_key == "AKIDSESSION"
    finally:
        conn.close()
    assert _soon(lambda: not daemon.holds(session))


@pytest.mark.parametrize("abruptly", [False, True], ids=["closed", "reset"])
def test_a_hold_that_goes_away_ends_the_session(daemon, abruptly):
    for _ in range(5):
        session = daemon.add()
        sock = daemon.raw_hold(session)
        try:
            data = b""
            while b"\r\n\r\n1\r\n\n\r\n" not in data:  # the answer's head, then its first beat: it is held
                chunk = sock.recv(1024)
                assert chunk, f"the daemon closed the hold before its first beat: {data!r}"
                data += chunk
            assert daemon.holds(session)
            if abruptly:  # what a killed process can leave behind: a reset, not a goodbye
                linger = struct.pack("HH", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        finally:
            sock.close()
        assert _soon(lambda s=session: not daemon.holds(s))


def test_a_hold_closed_right_after_the_request_still_ends_the_session(daemon):
    # A process that ends as it asks: the daemon may read the request after the connection closed. (A reset
    # that early can destroy the request before the daemon reads it: nothing is held then, and the wrapper
    # opens the hold and waits for its answer before it starts Claude Code.)
    sessions = [daemon.add() for _ in range(10)]
    for session in sessions:
        daemon.raw_hold(session).close()
    assert _soon(lambda: not any(daemon.holds(s) for s in sessions))


def test_a_hold_that_is_refused_ends_nothing(daemon):
    session = daemon.add()
    for headers in ({}, {"x-latet-token": "not the token"}):
        conn, answer = daemon.hold(session, headers)
        conn.close()
        assert answer.status == 401
    conn, answer = daemon.hold("0" * 32)
    conn.close()
    assert answer.status == 404
    time.sleep(5 * BEAT)
    assert daemon.holds(session)


def test_a_held_session_still_ends_when_its_credentials_do(daemon):
    session = daemon.add(expires=THEN + 60)
    conn, answer = daemon.hold(session)
    try:
        assert answer.status == 200 and answer.read(1) == b"\n"
        daemon.now[0] = THEN + 61
        deadline = time.monotonic() + WITHIN
        while answer.read(1):  # the daemon ends its answer at the next beat
            assert time.monotonic() < deadline, "the hold never ended"
        assert not daemon.holds(session)
    finally:
        conn.close()


def test_an_expiry_at_the_epoch_is_a_time_long_past_not_no_time(daemon):
    assert not daemon.holds(daemon.add(expires=0.0))


def test_the_wrappers_hold_lasts_until_its_session_is_dropped(daemon):
    session = daemon.add()
    thread = wrappers.hold_aws_session(session, daemon.config)
    assert thread is not None and thread.is_alive()
    time.sleep(5 * BEAT)
    assert daemon.holds(session) and thread.is_alive()
    wrappers.drop_aws_session(session, daemon.config)  # the wrapper's own end
    thread.join(timeout=WITHIN)
    assert not thread.is_alive()


def test_a_daemon_with_no_such_hold_leaves_the_wrapper_going(daemon):
    # An older daemon answers 404 to a request it has no route for, as this one does for a session it lacks.
    assert wrappers.hold_aws_session("0" * 32, daemon.config) is None


AGENT = """
import time

for _ in range(300):  # it ends by itself in a minute, or when the test stops reading what it prints
    time.sleep(0.2)
    print("AGENT", flush=True)
"""


def test_a_wrapper_that_is_killed_loses_its_session_though_its_agent_lives_on(daemon):
    script = textwrap.dedent(
        f"""
        import subprocess
        import sys
        from latet import wrappers
        from latet.config import Config
        from latet.gateway.sigv4 import AwsCredentials

        config = Config(port={daemon.port})
        session = wrappers.aws_session(AwsCredentials("AKIDSESSION", "s3cr3t", "token"), config)
        wrappers.hold_aws_session(session, config)
        subprocess.Popen([sys.executable, "-c", {AGENT!r}], stderr=subprocess.DEVNULL)  # Claude Code
        print("HELD", session, flush=True)
        sys.stdin.read()  # until it is killed
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        seen = []
        for line in child.stdout or []:  # until it says it holds a session, or ends
            if line.startswith("HELD "):
                break
            seen.append(line)
        else:
            pytest.fail("the wrapper never held a session: " + "".join(seen))
        session = line.split()[1]
        assert daemon.holds(session)
        time.sleep(5 * BEAT)
        assert daemon.holds(session)  # held for as long as it lives
        child.kill()
        child.wait(timeout=10)
        assert _soon(lambda: not daemon.holds(session))  # its agent still runs: the connection is not its to keep
        assert [child.stdout.readline().strip() for _ in range(4)] == ["AGENT"] * 4 if child.stdout else False
    finally:
        child.kill()
        child.wait(timeout=10)
        for pipe in (child.stdin, child.stdout):
            if pipe:
                pipe.close()


def test_a_daemon_that_stops_does_not_wait_for_its_holds(daemon):
    session = daemon.add()
    conn, answer = daemon.hold(session)
    try:
        assert answer.status == 200 and answer.read(1) == b"\n"
        started = time.monotonic()
        daemon.stop()
        assert not daemon.thread.is_alive() and time.monotonic() - started < 8
        assert not daemon.holds(session)  # a daemon that restarts keeps nothing, and neither does this one
    finally:
        conn.close()
