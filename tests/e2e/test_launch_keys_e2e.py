"""One composed in-process launch witness: the real `latet copilot`, LaunchSession, run_owned and its monitor, the
bridge's OPEN, HOLD, prepare and EXIT routes, the full local gateway and its sinks, with an inert child and a
MockTransport provider. No native client, external process, network listener or real provider.

What it proves: the key of the actual OPEN reaches the child's COPILOT_PROVIDER_HEADERS exactly once and never its
argv or another variable; the gateway verifies that exact key and selects the binding's lowering scope; a value the
vault masks reaches the provider masked and comes back to the child restored; the provider sees no carrier; neither
the key nor the original value reaches the CLI output, the log (DEBUG, every thread) or the metrics reports; and the key
reaches neither History nor the capture.

What it does not prove: the send-time recheck of the key (the currentness observed here happens during mediation; the
"end-at-gate" row of tests/unit/test_local_request_launch_authority.py covers the gate); the real LaunchWire's sockets
and interrupt (the two thin adapters below carry the same JSON and ACK through the ASGI app, with its checks); the native
Copilot CLI's header parsing; or an OS process list. The e2e send guard wraps httpx's AsyncHTTPTransport, not the
MockTransport provider used here, so it adds no evidence to this node."""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import json
import logging
import subprocess
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import latet.integrations.routes
import latet.server.client
import latet.server.connection
from latet import launch_session, observe, wrappers
from latet.cli.main import app as cli_app
from latet.config import Config, Profile, ProjectEntry
from latet.engine.pipeline import request_scope
from latet.engine.types import EntityClass
from latet.gateway import forward
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.launch_keys import LocalLaunchKeys
from latet.gateway.local_launch import LocalRequestLaunch
from latet.gateway.outcome import Forward, Pass
from latet.launch_session import LaunchControlUnavailable
from latet.launch_transport import _ACK_MOST, _ANSWER_MOST, _object
from latet.projects import Projects

from ..unit.test_request_launch_confidentiality import _ANSWER
from ..unit.test_request_leaks import _Answer
from ..unit.test_request_outcomes import OPENAI
from ..unit.test_response_sink_failures import _TIMEOUT, _sinks
from ..unit.test_wrapper_routes import REAL_RUN, launched  # noqa: F401 - fixture

pytestmark = pytest.mark.e2e

# The CLI imports these lazily; loaded here, their first import is outside the 5 s observation of the composition.
_PRELOADED = (latet.integrations.routes, latet.server.client, latet.server.connection)

_BASE = "http://127.0.0.1:5283"
_HOST = b"127.0.0.1:5283"
_BRIDGE = "/_latet/engine/v1/launches"
_TOKEN = "synthetic-composed-launch-bridge-token"
_HEADER = "x-latet-launch-key"
_SESSION = "0d5e6f70-8192-4a3b-9c4d-5e6f708192a3"  # a session id within its UUID grammar (request_profiles.ID_HEADERS)
# Accept-Language is a header every request profile names (request_profiles._FREE_HEADERS, openai.chat.1 included):
# checked, screened and forwarded. An unnamed one (X-Company) is refused before the provider (envelope-header-unknown).
_KEPT = ("Accept-Language", "en-US")
_ORIGINAL = "ghp_" + "Q4wE8rT2yU6iO0pA3sD7fG1hJ5kL9zX2cV6b"  # a GitHub token shape: the secrets profile masks it
_ASKED = f"ordinary request with token {_ORIGINAL}"
_BODY = json.dumps({"model": "m", "messages": [{"role": "user", "content": _ASKED}]}, separators=(",", ":")).encode()
_REPLY = b"ordinary provider reply"
# Submissions to the loop: OPEN, HOLD, the child's request, prepare and EXIT, with room for a failure path's END.
_SUBMISSIONS = 8


async def test_launched_copilot_uses_actual_bridge_and_gateway_without_sink_disclosure(
    launched,  # noqa: F811
    monkeypatch,
    caplog,
):
    _, _, tmp_path = launched
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="latet")
    project = tmp_path / "registered-composed-launch"
    project.mkdir()
    project = project.resolve(strict=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
    monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("COPILOT_PROVIDER_HEADERS", f"{_KEPT[0]}: {_KEPT[1]}")
    for name in ("COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers, "_daemon_token", lambda _prepared: _TOKEN)

    def keyless_child(*_args, **_kwargs):
        raise RuntimeError("unexpected keyless child")

    # A failed OPEN must never fall through to a real subprocess. Only these modules' own names are replaced, never
    # the process-wide subprocess module.
    monkeypatch.setattr(
        wrappers,
        "subprocess",
        SimpleNamespace(call=keyless_child, run=subprocess.run, SubprocessError=subprocess.SubprocessError),
    )

    loop = asyncio.get_running_loop()
    disconnect = asyncio.Event()
    acknowledged = asyncio.Event()
    cli_finished = asyncio.Event()
    cli_started = threading.Event()
    hold_done = threading.Event()
    failed = threading.Event()
    timed_out = threading.Event()
    limit_reached = threading.Event()
    lock = threading.Lock()
    futures: list[concurrent.futures.Future] = []
    hold_tasks: list[asyncio.Task] = []
    hold_refusals: list[str] = []
    control_calls: list[tuple[str, str]] = []
    opened: dict[str, Any] = {}
    checks: list[tuple[str, bool]] = []
    order: list[str] = []
    provider_calls: list[int] = []
    sent: list[bool] = []
    fakes: list[str] = []
    answers: list[_Answer] = []
    current_checks: list[bool] = []
    finalize_checks: list[bool] = []
    heard: list[tuple[str, tuple[Any, ...]]] = []
    cli_task: asyncio.Task | None = None
    cli_result = None
    response_bytes = b""
    history = raw = ""
    capture = tmp_path / "capture"
    owner = LocalLaunchKeys(b"r" * 32)
    baseline = owner.counts
    counts_after = None  # read before the registry closes: a closed one counts nothing
    real_current = LocalRequestLaunch.current
    real_finalize = forward.finalize
    expected_scope = None
    restored = _ANSWER.replace(_REPLY, _REPLY + b" " + _ORIGINAL.encode())

    class Metrics:
        """Every metrics report, as a server's Metrics hears it (observe.listen): a computer reports too, though
        nothing listens there in use."""

        def masked(self, found):
            heard.append(("masked", (found,)))

        def exchange(self, agent, stopped):
            heard.append(("exchange", (agent, stopped)))

        def waiting(self, delta):
            heard.append(("waiting", (delta,)))

        def apps(self, operation, texts, values):
            heard.append(("apps", (operation, texts, values)))

    metrics = Metrics()

    def observed_current(selection):
        result = real_current(selection)
        if selection.owner is owner:
            current_checks.append(selection.status == "verified" and result)
            order.append("current")
        return result

    def observed_finalize(arena, *args, **kwargs):
        result = real_finalize(arena, *args, **kwargs)
        selection = request_scope.get()
        finalize_checks.append(
            selection is not None
            and selection.session is arena.session
            and selection.scope is expected_scope
            and not arena.off
            and isinstance(result, (Forward, Pass))
        )
        return result

    monkeypatch.setattr(LocalRequestLaunch, "current", observed_current)
    monkeypatch.setattr(forward, "finalize", observed_finalize)

    def submit(coroutine):
        with lock:
            if len(futures) >= _SUBMISSIONS:
                coroutine.close()
                limit_reached.set()
                failed.set()
                loop.call_soon_threadsafe(disconnect.set)
                # A terminal code, as the bridge would answer: the real monitor stops instead of retrying.
                raise LaunchControlUnavailable("launch-ended")
            actual = asyncio.run_coroutine_threadsafe(coroutine, loop)
            futures.append(actual)
        try:
            return actual.result(_TIMEOUT)
        except TimeoutError:
            if actual.done():
                raise  # the coroutine's own TimeoutError, not this observation's
            # Observation timeout is NOT physical completion/cancellation.
            timed_out.set()
            failed.set()
            loop.call_soon_threadsafe(disconnect.set)
            return actual.result()  # Retained owner; the outer 180 s job is the hard stop.

    def invoke_cli():
        cli_started.set()
        try:
            return CliRunner().invoke(cli_app, ["copilot", "-p", "hello"])
        finally:
            # Only the actual invocation signals this, after invoke and its
            # real run_owned monitor/cleanup return. Task cancellation cannot.
            loop.call_soon_threadsafe(cli_finished.set)

    async def settle(actual, what):
        # Defer cancellation until the specific actual owner is settled. A
        # repeated observation interruption cannot release the surrounding app.
        while True:
            try:
                return await asyncio.shield(actual)
            except asyncio.CancelledError:
                failed.set()
                disconnect.set()
                if actual.cancelled():
                    checks.append((f"{what} settled without cancellation", False))
                    return None
            except Exception:
                checks.append((f"{what} settled without error", False))
                return None

    unlisten = observe.listen(metrics)
    try:
        with _sinks(tmp_path) as (mediator, recorder, store):
            vault = mediator.engine.vault
            scopes = Projects(
                Config(
                    ner="off",
                    projects=[ProjectEntry(path=str(project), profile=Profile.SECRETS)],
                ),
                vault,
                files=False,
            )
            mediator.engine.scopes = scopes
            resolved = scopes.resolve(str(project))
            assert resolved.lowers and resolved.scope is not None
            expected_scope = resolved.scope
            route = OPENAI  # the session id needs no configured value, only its grammar

            def provider(request):
                provider_calls.append(1)  # counted before anything can raise
                if len(provider_calls) > 1:
                    return httpx.Response(500)
                key = opened.get("key", "")
                entity = vault.by_original(_ORIGINAL)
                fake = entity.surrogate if entity is not None else ""
                try:
                    content = json.loads(request.content)["messages"][0]["content"]
                except (ValueError, LookupError, TypeError):
                    content = None
                sent.append(
                    bool(key)
                    and _HEADER not in request.headers
                    and key not in str(request.url)
                    and key.encode() not in request.content
                    and all(key not in value for value in request.headers.values())
                    and request.headers.get(_KEPT[0]) == _KEPT[1]
                    and entity is not None
                    and entity.eclass is EntityClass.SECRET
                    and bool(fake)
                    and fake != _ORIGINAL
                    and content == _ASKED.replace(_ORIGINAL, fake)
                    and _ORIGINAL.encode() not in request.content
                    and bool(current_checks)
                    and all(current_checks)
                    and len(request.content) <= 16 * 1024
                )
                fakes.append(fake)
                order.append("provider")
                answer = _Answer(_ANSWER.replace(_REPLY, _REPLY + b" " + fake.encode()))
                answers.append(answer)
                return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

            gateway = create_app(
                GatewayConfig(
                    mediator=mediator,
                    recorder=recorder,
                    bridge_token=_TOKEN,
                    launch_keys=owner,
                    capture_dir=capture,
                    fixed_routes=[route],
                    client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
                )
            )
            async with gateway.router.lifespan_context(gateway):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway), base_url=_BASE, trust_env=False
                ) as client:

                    async def control(method, path, body):
                        # LaunchWire.call's answer handling, carried through the ASGI app instead of a socket.
                        control_calls.append((method, path))
                        response = await client.request(method, path, headers={"x-latet-token": _TOKEN}, json=body)
                        if (
                            response.headers.get("content-encoding", "identity").lower() != "identity"
                            or len(response.content) > _ANSWER_MOST
                        ):
                            raise LaunchControlUnavailable()
                        value = _object(response.content)
                        if response.status_code != 200:
                            code = value.get("error")
                            raise LaunchControlUnavailable(code if type(code) is str else "launch-unavailable")
                        if path == _BRIDGE:
                            opened.update(value)
                            order.append("open")
                        if path.endswith("/exit"):
                            checks.append(("EXIT retires the launch", value.get("retired") is True))
                            order.append("exit")
                        return value

                    async def start_hold(identifier, generation):
                        if hold_tasks:
                            hold_refusals.append("second HOLD")
                            raise LaunchControlUnavailable()
                        path = f"{_BRIDGE}/{identifier}/hold"
                        ack: dict[str, Any] = {}
                        status: list[int] = []

                        async def receive():
                            await disconnect.wait()
                            return {"type": "http.disconnect"}

                        async def send(message):
                            if message["type"] == "http.response.start":
                                status.append(message["status"])
                                media = dict(message.get("headers", ())).get(b"content-type", b"").split(b";")[0]
                                checks.append(
                                    (
                                        "HOLD answers 200 x-ndjson",
                                        message["status"] == 200 and media == b"application/x-ndjson",
                                    )
                                )
                            elif (
                                message["type"] == "http.response.body"
                                and message.get("more_body")
                                and not acknowledged.is_set()
                            ):
                                # LaunchWire.hold's ACK handling: one bounded, newline-terminated JSON object.
                                raw = message.get("body", b"")
                                checks.append(
                                    (
                                        "HOLD ACK bounded and newline-terminated",
                                        len(raw) <= _ACK_MOST and raw.endswith(b"\n"),
                                    )
                                )
                                ack.update(_object(raw))
                                checks.append(
                                    ("key active at ACK", owner.verify(opened["key"], "computer").code == "active")
                                )
                                order.append("ack")
                                acknowledged.set()

                        scope = {
                            "type": "http",
                            "method": "GET",
                            "scheme": "http",
                            "http_version": "1.1",
                            "path": path,
                            "raw_path": path.encode(),
                            "query_string": f"generation={generation}".encode(),
                            # The full gateway's LocalOnlyMiddleware admits only this machine's Host.
                            "headers": [(b"host", _HOST), (b"x-latet-token", _TOKEN.encode())],
                            "server": ("127.0.0.1", 5283),
                            "client": ("127.0.0.1", 123),
                            "root_path": "",
                            "asgi": {"version": "3.0"},
                        }
                        actual = asyncio.create_task(gateway(scope, receive, send))
                        hold_tasks.append(actual)
                        actual.add_done_callback(lambda _task: hold_done.set())
                        waiter = asyncio.ensure_future(acknowledged.wait())
                        try:
                            await asyncio.wait({waiter, actual}, timeout=_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            waiter.cancel()
                            await asyncio.gather(waiter, return_exceptions=True)
                        if not acknowledged.is_set():
                            # A HOLD that ended or stalled before its ACK, with what it answered.
                            hold_refusals.append(f"no ACK (status {status}, ended {actual.done()})")
                            raise LaunchControlUnavailable()
                        if set(ack) != {"held", "generation"} or ack != {"held": True, "generation": generation + 1}:
                            hold_refusals.append("ACK refused")
                            raise LaunchControlUnavailable()

                        class Held:
                            def __init__(self):
                                self.generation = ack["generation"]

                            def wait(self):
                                hold_done.wait()

                            def close(self):
                                loop.call_soon_threadsafe(disconnect.set)

                            def drain(self):
                                hold_done.wait()

                        return Held()

                    async def request_from_child(env):
                        nonlocal response_bytes
                        pairs = [entry.split(":", 1) for entry in env["COPILOT_PROVIDER_HEADERS"].splitlines()]
                        headers = {name.strip(): value.strip() for name, value in pairs}
                        headers.update({"content-type": "application/json", "x-claude-code-session-id": _SESSION})
                        response = await client.post(
                            env["COPILOT_PROVIDER_BASE_URL"].rstrip("/") + "/chat/completions",
                            headers=headers,
                            content=_BODY,
                        )
                        response_bytes = response.content
                        checks.append(
                            (
                                "child receives the reply restored",
                                response.status_code == 200
                                and response_bytes == restored
                                and bool(fakes)
                                and fakes[0].encode() not in response_bytes,
                            )
                        )
                        return 0

                    class Child:
                        def __init__(self, argv, *, env):
                            self.code = None
                            self.env = env
                            key = opened.get("key", "")
                            values = env.get("COPILOT_PROVIDER_HEADERS", "")
                            checks.append(
                                (
                                    "child carries the acknowledged key once, never in argv",
                                    acknowledged.is_set()
                                    and bool(key)
                                    and key not in json.dumps(argv)
                                    and values.count(_HEADER + ":") == 1
                                    and key in values
                                    and f"{_KEPT[0]}: {_KEPT[1]}" in values
                                    and sum(key in value for value in env.values()) == 1,
                                )
                            )
                            order.append("child")

                        def poll(self):
                            return self.code

                        def wait(self):
                            if self.code is None:
                                if failed.is_set():
                                    self.code = 1
                                else:
                                    self.code = submit(request_from_child(self.env))
                            return self.code

                        def kill(self):
                            failed.set()
                            self.code = 1
                            loop.call_soon_threadsafe(disconnect.set)

                    monkeypatch.setattr(
                        wrappers, "_launch_call", lambda method, path, body, **_kw: submit(control(method, path, body))
                    )
                    monkeypatch.setattr(
                        wrappers,
                        "_launch_hold",
                        lambda identifier, generation, **_kw: submit(start_hold(identifier, generation)),
                    )
                    # launch_session uses only Popen: its own name, never the process-wide module.
                    monkeypatch.setattr(launch_session, "subprocess", SimpleNamespace(Popen=Child))
                    try:
                        cli_task = asyncio.create_task(asyncio.to_thread(invoke_cli))
                        try:
                            cli_result = await asyncio.wait_for(asyncio.shield(cli_task), _TIMEOUT)
                        except TimeoutError:
                            if cli_task.done():
                                raise  # the CLI's own TimeoutError, not this observation's
                            timed_out.set()
                            failed.set()
                            disconnect.set()
                            cli_result = await asyncio.shield(cli_task)
                    finally:
                        failed.set()
                        disconnect.set()
                        if cli_task is not None:
                            # App/client remain alive until physical CLI+monitor
                            # settlement, including every submitted control.
                            await settle(cli_task, "CLI task")
                            if cli_started.is_set():
                                # A cancelled to_thread Task can be done while its
                                # actual invocation still runs. Keep every owner
                                # alive until the independent physical signal.
                                while not cli_finished.is_set():
                                    try:
                                        await cli_finished.wait()
                                    except asyncio.CancelledError:
                                        failed.set()
                                        disconnect.set()
                            else:
                                # No thread ever ran the CLI: nothing can signal, so do not wait for one.
                                checks.append(("the CLI thread started", False))
                        for actual in futures:
                            await settle(asyncio.wrap_future(actual), "control submission")
                        for actual in hold_tasks:
                            await settle(actual, "HOLD task")

                assert recorder.drain(_TIMEOUT), "actual Recorder drain"
                summaries = store.list()[0]
                assert len(summaries) == 1, "positive ordinary History row"
                history = json.dumps((summaries, [store.detail(row["id"]) for row in summaries]))
                paths = list(capture.glob("*.jsonl"))
                assert len(paths) == 1, "positive actual capture"
                raw = paths[0].read_text("utf-8")
                counts_after = owner.counts
    finally:
        unlisten()
        owner.close()

    key = opened.get("key", "")
    told = repr(heard)
    messages = [record.getMessage() for record in caplog.records]
    events = ("open", "ack", "child", "current", "provider", "exit")
    checks.extend(
        [
            ("CLI exits 0", cli_result is not None and cli_result.exit_code == 0),
            ("CLI result carries no exception", cli_result is not None and cli_result.exception is None),
            (
                "CLI output without the key or the original value",
                cli_result is not None
                and bool(key)
                and key not in cli_result.output
                and _ORIGINAL not in cli_result.output,
            ),
            ("History and capture without the key", bool(key) and key not in history and key not in raw),
            ("History positive", "ordinary request" in history and "ordinary provider reply" in history),
            ("capture positive", "ordinary request" in raw and "ordinary provider reply" in raw),
            (
                "log at DEBUG without the key or the original value",
                bool(messages)
                and all(key not in message and _ORIGINAL not in message for message in messages)
                and key not in caplog.text
                and _ORIGINAL not in caplog.text,
            ),
            (
                "metrics heard the masking and the exchange",
                any(name == "masked" and any(kind == "SECRET" for _, kind in told_args[0]) for name, told_args in heard)
                and any(name == "exchange" and told_args[1] is False for name, told_args in heard),
            ),
            ("metrics without the key or the original value", bool(key) and key not in told and _ORIGINAL not in told),
            ("one clean masked provider send", provider_calls == [1] and sent == [True] and len(answers) == 1),
            ("provider answer closed", all(answer.closed for answer in answers) and bool(answers)),
            ("verified binding scope", finalize_checks == [True] and bool(current_checks) and all(current_checks)),
            ("launch retired to the counts baseline", counts_after == baseline),
            (
                "one HOLD, settled",
                len(hold_tasks) == 1 and all(task.done() for task in hold_tasks) and not hold_refusals,
            ),
            ("bounded submissions", not limit_reached.is_set() and all(actual.done() for actual in futures)),
            ("bounded control calls", len(control_calls) <= _SUBMISSIONS),
            ("CLI finished physically", cli_started.is_set() and cli_finished.is_set()),
            ("one OPEN", sum(path == _BRIDGE for _, path in control_calls) == 1),
            ("one EXIT", sum(path.endswith("/exit") for _, path in control_calls) == 1),
            ("no recovery", not any(path.endswith("/recover") for _, path in control_calls)),
            ("no observation timeout", not timed_out.is_set()),
            (
                "OPEN < ACK < child < currentness < provider < EXIT",
                all(event in order for event in events)
                and all(order.index(a) < order.index(b) for a, b in itertools.pairwise(events)),
            ),
        ]
    )
    refused = [name for name, passed in checks if not passed]
    assert checks and not refused, refused
