"""`latet server run --workers N` with real workers (the server performance plan's Task 10): the supervisor holds one
port and starts each worker in a process of its own (``spawn``), each an instance of the server on the test services
(on Linux listening on the port with a socket of its own), in the test's own schema and Valkey prefix (given to
``worker_main`` explicitly: never the default ``latet``), with its own data folder. Both answer on the one port and
beat; the status lists each instance alive with its worker's number and its memory; a worker killed is started again
and listed again; and a request a dying worker had in flight fails, never answered with the value it carried."""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import json
import multiprocessing
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from latet.server.settings import ServerSettings
from latet.server.workers import Supervisor, worker_main

from .support import DATABASE, VALKEY, master_key_file

pytestmark = pytest.mark.server

ADMIN = "/_latet/server/v1/admin"
CANARY = "mario.rossi.t10@acme.it"
STARTED = 120.0  # seconds two workers have to start and say they are ready, on a busy machine


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running(server, schema, prefix, tmp_path):
    """Two workers of the test's server under a supervisor running in a thread of its own, on a port of 127.0.0.1:
    the supervisor, the address, and the test's own instance (``server``, in the same schema and prefix, which runs no
    beat: it is listed nowhere). Stopped at the end, every worker with it."""
    port = _free_port()
    settings = ServerSettings(
        url=f"http://127.0.0.1:{port}", database_url=DATABASE, valkey_url=VALKEY, master_key_file=master_key_file(),
        host="127.0.0.1", port=port, workers=2,
    )  # fmt: skip
    (tmp_path / "models").mkdir()
    target = functools.partial(worker_main, schema=schema, prefix=prefix, data=tmp_path / "models")
    supervisor = Supervisor(settings, 2, target=target)
    thread = threading.Thread(target=supervisor.run, name="test-supervisor", daemon=True)
    thread.start()
    try:
        yield supervisor, f"http://127.0.0.1:{port}", server
    finally:
        supervisor.stop()
        thread.join(120)
        assert not thread.is_alive(), "the supervisor did not stop"
        assert multiprocessing.active_children() == []


def _ready(server, count: int, gone: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """The instances alive and ready, as the status reads them, once ``count`` of them are, none of ``gone`` (a worker
    killed: its record lives on until it expires)."""
    deadline = time.monotonic() + STARTED
    while True:
        found = [row for row in server.instances() if row["ready"] is True and row["instance"] not in gone]
        if len(found) >= count:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"{len(found)} workers ready of {count}: {server.instances()}")
        time.sleep(0.25)


def _status(url: str, admin: str) -> dict:
    with httpx.Client(base_url=url, trust_env=False, timeout=30) as http:
        answer = http.get(f"{ADMIN}/status", headers={"x-latet-admin": admin, "connection": "close"})
    answer.raise_for_status()
    return answer.json()


def test_two_workers_answer_on_one_port(running):
    supervisor, url, server = running
    ready = _ready(server, 2)
    assert sorted(row["worker"] for row in ready) == [0, 1]
    admin = server.admin.issue("workers")
    answered = set()
    deadline = time.monotonic() + 60
    while len(answered) < 2 and time.monotonic() < deadline:  # each request on a connection of its own
        status = _status(url, admin)
        answered.add(status["instance"])
    assert answered == {row["instance"] for row in ready}  # both answered on the one port
    listed = {row["instance"]: row for row in status["instances"]}
    assert set(listed) == answered
    for row in listed.values():
        assert row["worker"] in (0, 1) and row["memory"] > 0 and row["ready"] is True
        assert set(row) == {"instance", "worker", "version", "ready", "memory", "spaces", "index"}
    assert {supervisor.said(0)["instance"], supervisor.said(1)["instance"]} == answered


def _listening_here(port: int) -> int:
    """How many sockets listen on ``port`` on this machine (Linux: /proc/net/tcp and tcp6, the state 0A)."""
    found = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError):
            for line in Path(table).read_text("ascii").splitlines()[1:]:
                fields = line.split()
                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    found += 1
    return found


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_REUSEPORT's spread by the kernel is Linux's alone")
def test_each_worker_listens_with_a_socket_of_its_own_and_connections_spread_over_them(running):
    """On Linux each worker listens on the port with a socket of its own (SO_REUSEPORT): two sockets listen there, the
    supervisor's (which holds the port) never, and the system deals new connections among them: 24 connections opened
    at once, as a load balancer's or the measurement's clients open theirs and keep them, reach both workers, four at
    least each (fewer is about three chances in ten thousand). One socket shared by every worker handed connections to
    them unevenly (notes/server-performance-task9-10-results.md). A worker beats ready from its application's start,
    and listens once that start is over: the sockets are counted until both listen."""
    _, url, server = running
    ready = _ready(server, 2)
    port, deadline = int(url.rsplit(":", 1)[1]), time.monotonic() + STARTED
    while _listening_here(port) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _listening_here(port) == 2
    admin = server.admin.issue("workers")
    together = threading.Barrier(24, timeout=60)

    def answered(_: int) -> str:
        with httpx.Client(base_url=url, trust_env=False, timeout=60) as http:
            together.wait()
            first = http.get(f"{ADMIN}/status", headers={"x-latet-admin": admin})
            again = http.get(f"{ADMIN}/status", headers={"x-latet-admin": admin})  # the same connection, kept
        first.raise_for_status()
        assert again.json()["instance"] == first.json()["instance"]
        return first.json()["instance"]

    with concurrent.futures.ThreadPoolExecutor(24) as pool:
        spread = Counter(pool.map(answered, range(24)))
    assert set(spread) == {row["instance"] for row in ready} and min(spread.values()) >= 4, spread


def test_a_killed_worker_is_listed_again_after_its_restart(running):
    supervisor, _, server = running
    before = {row["worker"]: row["instance"] for row in _ready(server, 2)}
    killed = supervisor.process(0)
    killed.kill()
    killed.join(30)
    deadline = time.monotonic() + STARTED + 15  # started again at most once in ten seconds
    while True:
        found = {row["instance"]: row for row in server.instances() if row["ready"] is True}
        again = [row for row in found.values() if row["worker"] == 0 and row["instance"] != before[0]]
        if again and before[1] in found:
            break
        assert time.monotonic() < deadline, server.instances()
        time.sleep(0.25)
    # The beat reaches Valkey and the supervisor's pipe each in its own time: the supervisor's, a moment later at most.
    while (supervisor.said(0) or {}).get("instance") != again[0]["instance"]:
        assert time.monotonic() < deadline, supervisor.said(0)
        time.sleep(0.1)
    assert supervisor.process(0).pid != killed.pid


def test_a_request_cut_by_a_dying_worker_is_never_answered_unmasked(running):
    supervisor, url, server = running
    first = {row["instance"] for row in _ready(server, 2)}
    _, token = server.identities.add_key("Logs")
    texts = [f"line {n}: write to {CANARY} about order {n}" for n in range(400)]
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

    body = json.dumps({"texts": texts, "kind": "data"}).encode()

    sending = threading.Semaphore(0)  # released as each slow body starts: its connection is open

    def slowly() -> Iterator[bytes]:
        """The body in ten parts over three seconds: the call is a worker's while it comes."""
        step = len(body) // 10 + 1
        for start in range(0, len(body), step):
            yield body[start : start + step]
            if start == 0:
                sending.release()
            time.sleep(0.3)

    def mask(slow: bool = False) -> str:
        """One call on a connection of its own: what came back, or why nothing did."""
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=60) as http:
                content = slowly() if slow else body
                answer = http.post("/_latet/apps/v1/mask", content=content, headers=headers)
        except httpx.HTTPError as exc:
            return f"failed: {type(exc).__name__}"
        if answer.status_code != 200:
            return f"refused: {answer.status_code}"
        masked = [result["text"] for result in answer.json()["results"]]
        assert len(masked) == len(texts) and all(CANARY not in text for text in masked), "answered unmasked"
        answers.add(tuple(masked))
        return "masked"

    answers: set[tuple[str, ...]] = set()  # each masked answer: one key, one vault, so one answer for one text

    assert mask().startswith("masked")  # each worker builds the key's space as its first call comes
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        calls = [pool.submit(mask, slow=n % 2 == 0) for n in range(16)]
        for _ in range(8):  # every slow body begun, then a moment for the workers to take their connections
            assert sending.acquire(timeout=60)
        time.sleep(0.5)
        for worker in (0, 1):
            with contextlib.suppress(AttributeError):
                supervisor.process(worker).kill()  # both: whichever holds a call has it cut
        outcomes = [call.result(timeout=180) for call in calls]
    assert all(outcome.startswith(("masked", "failed", "refused")) for outcome in outcomes), outcomes
    assert any(outcome.startswith("failed") for outcome in outcomes), outcomes  # some were cut: the case under test
    _ready(server, 2, gone=first)  # started again, and they mask as before
    for _ in range(6):  # on connections of their own: both workers' answers
        assert mask().startswith("masked")
    assert len(answers) == 1  # T10-S11: every worker, before and after the restart, gave the same fakes


def test_the_status_lists_what_valkey_holds_of_the_instances_checked_and_echoes_nothing(server):
    """M6 (T10-S8): a key an older version wrote (b"1"), one written by hand with a canary and a field of its own, and
    one that expired: listed by its id alone, by what is a beat's alone, or not at all."""
    bus, now = server.bus, int(time.time() * 1000)
    bus.client.zadd(bus.key("instances"), {"0123456789ab": now, "abcdef012345": now, "fedcba987654": now})
    bus.client.set(bus.key("instance:0123456789ab"), b"1", px=30_000)
    forged = {"instance": "abcdef012345", "worker": 2, "version": CANARY + " " * 3, "ready": True, "memory": 5,
              "spaces": 1, "index": 0, "words": CANARY}  # fmt: skip
    bus.client.set(bus.key("instance:abcdef012345"), json.dumps(forged).encode(), px=30_000)
    listed = server.instances()
    assert listed == [
        {"instance": "abcdef012345", "worker": 2, "version": None, "ready": True, "memory": 5, "spaces": 1,
         "index": 0},
        {"instance": "0123456789ab", "worker": None, "version": None, "ready": None, "memory": None, "spaces": None,
         "index": None, "older": True},
    ]  # fmt: skip
    assert CANARY not in json.dumps(listed)


def test_the_status_lists_an_instance_of_latet_0_10_that_says_nothing_of_itself_and_leaves_its_key(server):
    """The final review's I-2, the security review's I-1: 0.10's beat is its key alone (``instance:<id>``, ``1``), never
    a place among the instances the beats list, never a version. A read of the bus's own ``instance:*`` keys finds it:
    listed by its id, as an instance of Latet 0.10 or older, with no version or readiness it did not say, beside a
    0.11 instance; its key is read, never deleted. A key of another prefix's, or one that is no instance's, is none of
    this server's instances."""
    from latet.server import app as app_module
    from latet.server.workers import TELEMETRY

    bus = server.bus
    bus.client.set(bus.key("instance:0a1b2c3d4e5f"), b"1", px=30_000)  # 0.10.4's beat
    bus.client.set(bus.key("instance:not-an-id"), b"1", px=30_000)
    elsewhere = f"other{bus.prefix}instance:0a1b2c3d4e60"
    bus.client.set(elsewhere, b"1", px=30_000)
    try:
        app_module._alive(server)  # a 0.11 instance beating beside it
        listed = server.instances()
        assert [row["instance"] for row in listed] == sorted([server.instance, "0a1b2c3d4e5f"])
        (older,) = [row for row in listed if row["instance"] == "0a1b2c3d4e5f"]
        assert older == dict.fromkeys(TELEMETRY) | {"instance": "0a1b2c3d4e5f", "older": True}
        (mine,) = [row for row in listed if row["instance"] == server.instance]
        assert "older" not in mine and isinstance(mine["version"], str)
        assert bus.client.get(bus.key("instance:0a1b2c3d4e5f")) == b"1"  # read, never deleted
    finally:
        bus.client.delete(elsewhere)


def test_the_list_of_instances_runs_on_valkeys_clock_whatever_this_machines_says(server, monkeypatch):
    """M9: each beat lists and prunes by Valkey's own clock (TIME), so a machine whose clock runs an hour ahead prunes
    no other machine's instance, and lists its own at Valkey's time."""
    from latet.server import app as app_module

    bus, real = server.bus, time.time
    listed = bus.key(app_module.INSTANCES)
    bus.client.zadd(listed, {"0123456789ab": int(real() * 1000)})  # another machine's, beating now
    monkeypatch.setattr(app_module.time, "time", lambda: real() + 3600)  # this one an hour ahead
    app_module._alive(server)
    assert bus.client.zscore(listed, "0123456789ab") is not None
    assert abs(bus.client.zscore(listed, server.instance) - real() * 1000) < 60_000
