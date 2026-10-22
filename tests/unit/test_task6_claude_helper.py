"""Deferred M14-20 helper unit consumers: declared test installation, real public setup/private state.

Direct module-entry execution and bounded HTTP transport doubles are unit evidence only.
They do not establish native startup, supported installed process, ACL isolation, redirects or hard cleanup.
"""

import builtins
import importlib.util
import io
import json
import math
import os
import runpy
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import keyring
import keyring.core
import pytest

from latet.config import data_dir
from latet.integrations import agents
from latet.server import connection
from latet.server.tokens import route_key
from latet.storage import folders, keys

from . import test_agents_connected as existing
from .test_task6_connection_admission import _forbid_content, _published_helper

_agents_here = existing._agents_here
connected = existing.connected
claude_home = existing.claude_home
trusted_helper_install = existing.trusted_helper_install

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="frozen Windows CLI candidate unit contract")

_SESSION = {"session": "owned-unit-session", "cwd": "C:\\owned-context-data-never-opened"}
_CASES = (
    ("ping", None, {"ok": True, "version": "unit"}),
    (
        "route-check",
        {
            **_SESSION,
            "agent": "claude",
            "surface": "cli",
            "scope": "server",
            "client_bridge": existing.SPACE,
            "client_agent_base": existing.SPACE + "/anthropic",
            "anthropic_base_url": existing.SPACE + "/anthropic",
        },
        {"ok": True},
    ),
    (
        "mask",
        {**_SESSION, "items": [{"text": "owned-input", "kind": "user_text"}]},
        {"items": [{"text": "owned-masked", "count": 1}]},
    ),
    ("rehydrate", {**_SESSION, "items": ["owned-masked"], "json": False}, {"items": ["owned-original"], "restored": 1}),
    ("scope", {**_SESSION, "items": ["owned-masked"]}, {"added": 1}),
    (
        "verify",
        {**_SESSION, "slots": [{"text": "owned-masked", "kind": "user_text"}], "history": 0},
        {"blocking": [], "flagged": [], "inherited": False},
    ),
    ("instructions", _SESSION, {"findings": []}),
    ("approvals/wait", {"id": "a" * 32}, {"items": [{"text": "owned-masked", "count": 1}]}),
    ("outbound", {**_SESSION, "tool": "WebFetch", "values": ["owned-masked"]}, {"found": [], "reason": None}),
)
_RESPONSES = {operation: response for operation, _, response in _CASES}


@pytest.fixture
def helper_bound(connected, computer, claude_home, trusted_helper_install, monkeypatch):
    agents.ClaudeCode().apply()
    env = existing._settings_env(claude_home)
    checked = connection.load_connection()
    assert checked is not None and env["LATET_BRIDGE_URL"] == existing.SPACE
    assert env["LATET_BRIDGE_TOKEN"] == existing.TOKEN
    descriptor, metadata = _published_helper(data_dir(), connection.connection_path(), checked, env)
    launch = json.loads(env["LATET_BRIDGE_HELPER"])
    sent, created = [], []
    real_client = httpx.Client

    def answer(request):
        prefix = f"/u/{route_key(existing.TOKEN)}/_latet/engine/v1/"
        assert request.url.scheme == "https" and request.url.host == "latet.acme.lan"
        assert request.url.path.startswith(prefix)
        operation = request.url.path.removeprefix(prefix)
        assert operation in _RESPONSES
        assert request.method == ("GET" if operation == "ping" else "POST")
        assert request.headers["x-latet-token"] == existing.TOKEN
        assert "origin" not in request.headers and not any(key.startswith("sec-fetch-") for key in request.headers)
        body = json.loads(request.content) if request.content else None
        sent.append((operation, body))
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(_RESPONSES[operation]).encode("utf-8")),
            headers={"content-type": "application/json"},
        )

    def owned_client(*args, **kwargs):
        assert kwargs.get("trust_env") is False and kwargs.get("follow_redirects") is False
        assert isinstance(kwargs.get("verify"), ssl.SSLContext)
        assert "transport" not in kwargs
        result = real_client(*args, transport=httpx.MockTransport(answer), **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(httpx, "Client", owned_client)

    def forbidden(*args, **kwargs):
        pytest.fail("helper attempted credential lookup")

    credential_checks = pytest.MonkeyPatch()

    def guard_credentials():
        for name in ("load_server_token", "_prepare_token", "_copies", "_keyring", "_read_private"):
            credential_checks.setattr(connection, name, forbidden)
        for name in ("load_bridge_token", "load_root_key", "load_reviewer_key", "_from_keyring", "_keyring_on"):
            credential_checks.setattr(keys, name, forbidden)
        for name in ("get_keyring", "get_password", "set_password", "delete_password"):
            credential_checks.setattr(keyring, name, forbidden)
        credential_checks.setattr(keyring.core, "get_keyring", forbidden)
        _forbid_content(credential_checks, data_dir() / "server.token")

    guard_credentials()

    @contextmanager
    def diagnostic_credentials():
        credential_checks.undo()  # Public disconnect's reviewed diagnostic/revocation exception, never the helper.
        try:
            yield
        finally:
            guard_credentials()

    preserved = {path: path.read_bytes() for path in (connection.connection_path(), descriptor)}
    # Token-file reads are now forbidden, including fd-based paths; public setup happened before these traps.

    def invoke(envelope, *, input_buffer=None, env_overrides=None):
        if importlib.util.find_spec("latet.claude_bridge_helper") is None:
            pytest.fail("helper feature entry unavailable; this is feature availability, never a security guard RED")
        raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        incoming = io.TextIOWrapper(io.BytesIO(raw) if input_buffer is None else input_buffer, encoding="utf-8")
        output, errors = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
        stderr = io.TextIOWrapper(errors, encoding="utf-8", write_through=True)
        with monkeypatch.context() as process:
            for key, value in launch["env"].items():
                process.setenv(key, value)
            for key, value in (env_overrides or {}).items():
                if value is None:
                    process.delenv(key, raising=False)
                else:
                    process.setenv(key, value)
            process.chdir(launch["cwd"])
            process.setattr(sys, "argv", ["latet.claude_bridge_helper", "--descriptor", str(descriptor)])
            process.setattr(sys, "stdin", incoming)
            process.setattr(sys, "stdout", stdout)
            process.setattr(sys, "stderr", stderr)
            try:
                runpy.run_module("latet.claude_bridge_helper", run_name="__main__")
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        stdout.flush()
        stderr.flush()
        assert errors.getvalue() == b""
        written = output.getvalue()
        assert len(written) <= 4 * 1024 * 1024 - 1
        result = json.loads(written)
        assert all(client.is_closed for client in created)
        return code, result

    def envelope(operation="ping", body=None):
        return {"v": 1, "op": operation, "bridge": existing.SPACE, "token": existing.TOKEN, "body": body}

    try:
        yield SimpleNamespace(
            invoke=invoke,
            envelope=envelope,
            descriptor=descriptor,
            metadata=metadata,
            checked=checked,
            sent=sent,
            created=created,
            preserved=preserved,
            data=data_dir(),
            diagnostic_credentials=diagnostic_credentials,
        )
    finally:
        credential_checks.undo()


@pytest.mark.parametrize(("operation", "body", "response"), _CASES, ids=[case[0] for case in _CASES])
def test_helper_public_bound_descriptor_reaches_each_fixed_operation_without_credential_lookup(
    helper_bound, operation, body, response
):
    code, result = helper_bound.invoke(helper_bound.envelope(operation, body))
    assert code == 0 and result == {"v": 1, "ok": True, "response": response}
    assert helper_bound.sent == [(operation, body)]
    assert len(helper_bound.created) == 1
    assert all(path.read_bytes() == raw for path, raw in helper_bound.preserved.items())


def test_helper_consumed_venv_launcher_reaches_real_binding_and_one_shot_transport(helper_bound):
    assert helper_bound.metadata["launch"]["env"]["__PYVENV_LAUNCHER__"] == ""
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()

    # CPython's Windows venv startup consumes this one selector before helper entry.
    code, result = helper_bound.invoke(helper_bound.envelope(), env_overrides={"__PYVENV_LAUNCHER__": None})
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 2
    assert all(path.read_bytes() == raw for path, raw in helper_bound.preserved.items())


@pytest.mark.parametrize(
    "override",
    [{"__PYVENV_LAUNCHER__": "owned-hostile-launcher"}, {"PYTHONPATH": None}],
    ids=["nonempty-launcher", "missing-nonlauncher"],
)
def test_helper_consumed_launcher_exception_does_not_relax_other_binding_environment(helper_bound, override):
    assert helper_bound.metadata["launch"]["env"]["__PYVENV_LAUNCHER__"] == ""
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()

    code, result = helper_bound.invoke(helper_bound.envelope(), env_overrides=override)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "binding"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1
    assert all(path.read_bytes() == raw for path, raw in helper_bound.preserved.items())


def test_helper_real_tls_requires_hostname_as_well_as_the_exact_pin(computer):
    """T6.19: the same pinned leaf works for its name, but sends no HTTP to a mismatching origin."""
    from starlette.middleware.base import BaseHTTPMiddleware

    from latet.engine.reviewer import certificate as certificates
    from latet.server import client

    from .fake_latet_server import FakeLatetServer

    crt, key, fingerprint = existing.make_certificate("https://localhost", computer / "hostname-tls")
    server = FakeLatetServer(computer / "hostname-tls", certificate=(crt, key))
    assert server.fingerprint == fingerprint
    assert certificates.save(data_dir(), server.pem) == fingerprint
    assert client._pinned(fingerprint, _data=data_dir()) == (server.pem, None)
    server.tokens.add(existing.TOKEN)
    reached = []

    async def record_http(request, call_next):
        reached.append((request.method, request.url.path))
        return await call_next(request)

    server.app.add_middleware(BaseHTTPMiddleware, dispatch=record_http)

    def ping(origin):
        checked = connection.Connection(
            url=origin, user="u_emilycarte", user_name="Emily Carter", fingerprint=fingerprint
        )
        prepared = connection._PreparedConnection(connection._selected_connection(), checked, existing.TOKEN)
        return client._bridge_once(prepared, "ping", None, time.monotonic() + 5.0, client.ReceiveBudget(4096))

    try:
        server.start()
        assert server._server is not None and server._server.started
        matching = f"https://localhost:{server.port}"
        assert json.loads(ping(matching)) == {"ok": True, "version": "test"}
        expected = [("GET", f"/u/{route_key(existing.TOKEN)}/_latet/engine/v1/ping")]
        assert reached == expected
        assert server.calls == [("GET", "_latet/engine/v1/ping")]

        # Same listener, certificate, trusted pin, route and token; only the origin's hostname changes.
        assert server.url == f"https://127.0.0.1:{server.port}"
        assert client._pinned(fingerprint, _data=data_dir()) == (server.pem, None)
        failure = None
        try:
            ping(server.url)
        except client.ServerError as exc:
            failure = exc
        assert reached == expected, "a pinned wrong-host certificate reached HTTP"
        assert server.calls == [("GET", "_latet/engine/v1/ping")]
        assert failure is not None, "the helper accepted its exact pin for the wrong hostname"
        assert failure.status == 0 and failure.message == "the helper exchange is unavailable"
    finally:
        server.stop()  # Existing fixture's bounded five-second join, including assertion failures.
        assert server._thread is None or not server._thread.is_alive(), "owned TLS listener did not stop"


@pytest.mark.parametrize("spelling", ["case", "slashes"])
def test_helper_systemroot_startup_spelling_reaches_real_binding_and_transport(helper_bound, spelling):
    expected = helper_bound.metadata["launch"]["env"]["SystemRoot"]
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()

    represented = expected.swapcase() if spelling == "case" else expected.replace("\\", "/")
    assert represented != expected  # This exercises an actual changed startup representation.
    code, result = helper_bound.invoke(
        helper_bound.envelope(),
        env_overrides={"SystemRoot": represented, "__PYVENV_LAUNCHER__": None},
    )
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 2
    assert helper_bound.metadata["launch"]["env"]["SystemRoot"] == expected
    assert all(path.read_bytes() == raw for path, raw in helper_bound.preserved.items())


@pytest.mark.parametrize("changed", ["different-path", "missing", "empty", "traversal", "extended", "unc", "other-key"])
def test_helper_systemroot_spelling_exception_keeps_exact_authority_and_other_environment(helper_bound, changed):
    expected = helper_bound.metadata["launch"]["env"]["SystemRoot"]
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()

    represented = {
        "different-path": expected + "-owned-other",
        "missing": None,
        "empty": "",
        "traversal": expected + "\\System32\\..",
        "extended": "\\\\?\\" + expected,
        "unc": "\\\\localhost\\" + expected.replace(":", "$"),
        "other-key": expected,
    }[changed]
    override = {"SystemRoot": represented, "__PYVENV_LAUNCHER__": None}
    if changed == "other-key":
        override["WINDIR"] = helper_bound.metadata["launch"]["env"]["WINDIR"].swapcase()
    code, result = helper_bound.invoke(helper_bound.envelope(), env_overrides=override)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "binding"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1
    assert helper_bound.metadata["launch"]["env"]["SystemRoot"] == expected
    assert all(path.read_bytes() == raw for path, raw in helper_bound.preserved.items())


@pytest.mark.parametrize("changed", ["descriptor-map", "trailing-separator", "dot-segment"])
def test_helper_systemroot_startup_exception_does_not_normalize_descriptor_or_aliases(helper_bound, changed):
    expected = helper_bound.metadata["launch"]["env"]["SystemRoot"]
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()
    override = {"__PYVENV_LAUNCHER__": None}
    preserved = dict(helper_bound.preserved)
    if changed == "descriptor-map":
        descriptor = json.loads(helper_bound.descriptor.read_bytes())
        descriptor["launch"]["env"]["SystemRoot"] = expected.swapcase()
        helper_bound.descriptor.write_bytes(json.dumps(descriptor, separators=(",", ":")).encode("utf-8"))
        preserved[helper_bound.descriptor] = helper_bound.descriptor.read_bytes()
    else:
        override["SystemRoot"] = expected + ("\\" if changed == "trailing-separator" else "\\.")
    code, result = helper_bound.invoke(helper_bound.envelope(), env_overrides=override)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "binding"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1
    assert all(
        path.read_bytes() == raw for path, raw in preserved.items()
    )  # No repair of the deliberately changed map.


@pytest.mark.parametrize(
    "changed",
    [
        "missing-descriptor",
        "unsafe-parent",
        "origin",
        "identity",
        "pin",
        "digest",
        "other-token-space",
        "other-space",
        "absent-token",
    ],
)
def test_helper_valid_positive_precedes_invalid_binding_zero_network_and_zero_credential_lookup(
    helper_bound, monkeypatch, changed
):
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    envelope = helper_bound.envelope()
    if changed == "missing-descriptor":
        helper_bound.descriptor.unlink()
    elif changed == "unsafe-parent":
        grants = folders.folder_grants

        def unsafe(path):
            owner, allowed = grants(path)
            return (
                (owner, [*allowed, folders.Grant("S-1-1-0", 0x100, False)])
                if Path(path) == helper_bound.descriptor.parent
                else (owner, allowed)
            )

        monkeypatch.setattr(folders, "folder_grants", unsafe)
    elif changed in ("origin", "identity", "pin"):
        field, value = {
            "origin": ("url", "https://other.latet.lan"),
            "identity": ("user", "u_abcdefghij"),
            "pin": ("fingerprint", "sha256:" + "f" * 64),
        }[changed]
        connection.save_connection(helper_bound.checked.model_copy(update={field: value}))
    elif changed == "digest":
        value = {**helper_bound.metadata, "space_sha256": "0" * 64}
        helper_bound.descriptor.write_text(json.dumps(value), "utf-8")
    elif changed == "other-token-space":
        envelope["token"] = "latet_pat_" + "b" * 43
        envelope["bridge"] = f"{helper_bound.checked.url}/u/{route_key(envelope['token'])}"
    elif changed == "other-space":
        envelope["bridge"] = f"{helper_bound.checked.url}/u/{route_key('latet_pat_' + 'b' * 43)}"
    else:
        envelope.pop("token")
    code, result = helper_bound.invoke(envelope)
    assert code == 2 and set(result) == {"v", "ok", "code"} and result["v"] == 1 and result["ok"] is False
    assert result["code"] == ("bad_input" if changed == "absent-token" else "binding")
    assert helper_bound.sent == []
    assert len(helper_bound.created) == 1  # Invalid binding did not construct another TLS/HTTP transport.


def test_helper_complete_stdout_depth_includes_the_protocol_envelope(helper_bound, monkeypatch):
    def reply(nesting):
        extra = None
        for _ in range(nesting):
            extra = [extra]
        return {"version": "unit", "extra": extra}

    # Network object depth63 plus the success envelope is exactly64: usable through the real consumer.
    monkeypatch.setitem(_RESPONSES, "ping", reply(62))
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    # The body alone is depth64, but successful stdout would be depth65 and the plugin must refuse it.
    monkeypatch.setitem(_RESPONSES, "ping", reply(63))
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "response"}
    assert helper_bound.sent == [("ping", None)]


def test_helper_original_deadline_terminates_while_stdin_is_blocked(helper_bound, monkeypatch):
    """Actual module-entry guard with a bounded OS-exit observation, not native process-death proof."""
    entered, release, terminated = threading.Event(), threading.Event(), threading.Event()
    original_clock = time.monotonic
    first, exits, answers, failures = True, [], [], []

    def clock():
        nonlocal first
        if first:
            first = False
            return original_clock()
        return original_clock() + 29.95  # Keep the original30s end; only50ms remains after bootstrap.

    class WaitingInput(io.BytesIO):
        def read(self, size: int | None = -1):
            entered.set()
            assert release.wait(2), "test-owned stdin was never released"
            return super().read(size)

    def exit_process(code):
        exits.append(code)
        terminated.set()
        release.set()  # Unit-only OS termination observation releases this test's blocked stream.

    raw = json.dumps(helper_bound.envelope()).encode("utf-8")

    def invoke():
        try:
            answers.append(helper_bound.invoke(helper_bound.envelope(), input_buffer=WaitingInput(raw)))
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(os, "_exit", exit_process)
    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        assert entered.wait(1), "real helper never reached the controlled stdin boundary"
        stopped = terminated.wait(1)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []
    assert stopped and exits == [2], "original deadline did not invoke process termination while stdin was blocked"
    assert answers == [(2, {"v": 1, "ok": False, "code": "deadline"})]
    assert helper_bound.sent == [] and helper_bound.created == []


@pytest.mark.parametrize("module", ["hashlib", "hmac"])
def test_helper_original_deadline_terminates_while_crypto_import_is_blocked(helper_bound, monkeypatch, module):
    """Exercise real module entry; observed OS exit is unit evidence, not native process-death proof."""
    from latet import claude_bridge

    entered, release, terminated = threading.Event(), threading.Event(), threading.Event()
    original_clock, original_import = time.monotonic, builtins.__import__
    original_binding = claude_bridge._read_binding
    first, exits, answers, failures, binding_reads = True, [], [], [], []

    def coarse():  # Windows' clock moves in steps of about 16 ms
        return math.floor(original_clock() / 0.016) * 0.016

    def clock():
        nonlocal first
        if first:
            first = False
            return coarse()
        # Past the end once the watchdog has fired: the wait that fired may end on a step before the end it waited for.
        return coarse() + (30.05 if terminated.is_set() else 29.95)

    def load(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module and globals is not None and globals.get("__name__") == "__main__":
            entered.set()
            assert release.wait(2), "test-owned import was never released"
        return original_import(name, globals, locals, fromlist, level)

    def exit_process(code):
        exits.append(code)
        terminated.set()
        release.set()

    def read_binding(*args, **kwargs):
        binding_reads.append(True)
        return original_binding(*args, **kwargs)

    def invoke():
        try:
            answers.append(helper_bound.invoke(helper_bound.envelope()))
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(builtins, "__import__", load)
    monkeypatch.setattr(os, "_exit", exit_process)
    monkeypatch.setattr(claude_bridge, "_read_binding", read_binding)
    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        assert entered.wait(1), "real helper never reached the controlled import boundary"
        stopped = terminated.wait(1)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and failures == []
    assert stopped and exits == [2], "original deadline did not invoke process termination while import was blocked"
    assert answers == [(2, {"v": 1, "ok": False, "code": "deadline"})]
    assert binding_reads == [] and helper_bound.sent == [] and helper_bound.created == []


def test_helper_one_shot_503_is_never_retried(helper_bound, monkeypatch):
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    exchange = httpx.MockTransport.handle_request

    def refusal(self, request):
        response = exchange(self, request)
        response.status_code = 503
        response.headers["retry-after"] = "0"
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_request", refusal)
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "unavailable"}
    assert helper_bound.sent == [("ping", None)]
    assert len(helper_bound.created) == 2  # One client per invocation; none replaced/retried after503.


def test_helper_says_a_refused_token_apart_from_a_server_that_did_not_answer(helper_bound, monkeypatch):
    """R6/R7: a 401 (revoked, or the identity disabled) is its own code, so the plugin says what to do."""
    exchange = httpx.MockTransport.handle_request

    def refusal(self, request):
        response = exchange(self, request)
        response.status_code = 401
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_request", refusal)
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "refused"}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1


def test_helper_input_byte_limit_uses_complete_utf8_envelope(helper_bound):
    envelope = helper_bound.envelope("mask", {"items": [{"text": ""}]})
    baseline = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    envelope["body"]["items"][0]["text"] = " " * (4 * 1024 * 1024 - len(baseline))
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    assert len(raw) == 4 * 1024 * 1024
    code, result = helper_bound.invoke(envelope, input_buffer=io.BytesIO(raw))
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["mask"]}
    helper_bound.sent.clear()
    envelope["body"]["items"][0]["text"] += " "
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    assert len(raw) == 4 * 1024 * 1024 + 1
    code, result = helper_bound.invoke(envelope, input_buffer=io.BytesIO(raw))
    assert code == 2 and result == {"v": 1, "ok": False, "code": "bad_input"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1


def test_helper_input_node_limit_counts_values_with_root_one(helper_bound):
    # Root1 + five envelope values + the items array = seven nodes before its strings; keys add bytes only.
    envelope = helper_bound.envelope("scope", {"items": [""] * 99_993})
    code, result = helper_bound.invoke(envelope)
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["scope"]}
    helper_bound.sent.clear()
    envelope["body"]["items"].append("")
    code, result = helper_bound.invoke(envelope)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "bad_input"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1


@pytest.mark.parametrize("orphan", [False, True], ids=["public-apply", "orphan-no-record-and-stale-session"])
def test_helper_disconnect_invalidates_only_fixed_owned_bindings(quiet, helper_bound, monkeypatch, orphan):
    from latet import claude_bridge
    from latet.cli import connect
    from latet.integrations.base import load_records

    from .conftest import run_cli

    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    paths = [helper_bound.descriptor]
    root = helper_bound.descriptor.parent
    unrelated = root / "person-keeps.txt"
    unrelated.write_bytes(b"owned unrelated data stays")
    revoked = []

    def revoke(url, token, fingerprint):
        revoked.append((url, token, fingerprint))
        return True, None  # Revocation transport alone is doubled; real public cleanup/record/state readers run.

    monkeypatch.setattr(connect, "_revoked", revoke)
    with helper_bound.diagnostic_credentials():
        if orphan:
            agents.ClaudeCode().revert()
            assert "claude" not in load_records() and not helper_bound.descriptor.exists()
            prepared = connection._prepare_token(connection._prepare_connection())
            decode = runpy.run_module("latet.claude_bridge_helper", run_name="owned_helper_parser_witness")["_json"]
            paths.append(claude_bridge._binding_path(helper_bound.data, "session-" + "a" * 32 + ".json"))
            for path in paths:
                plan = claude_bridge._plan_binding(prepared, path)
                assert plan is not None
                claude_bridge._publish_binding(plan)  # Real trusted setup creates an orphan/stale attempt.
                launch = json.loads(plan.setting)
                with monkeypatch.context() as selected:
                    for key, value in launch["env"].items():
                        selected.setenv(key, value)
                    selected.chdir(launch["cwd"])
                    incoming = {key: os.environ.get(key) for key in launch["env"]}
                    checked, _ = claude_bridge._read_binding(path, decode, incoming)
                    assert checked.connection == helper_bound.checked
        done = run_cli("disconnect", "--yes")
    assert done.exit_code == 0, done.output
    assert revoked == [(helper_bound.checked.url, existing.TOKEN, helper_bound.checked.fingerprint)]
    assert connection.load_connection() is None
    assert all(not path.exists() for path in paths), "disconnect retained an owned usable attempt descriptor"
    assert unrelated.read_bytes() == b"owned unrelated data stays"
    cwd = root / "cwd"
    assert not cwd.exists()  # C-M6: the helper's empty folders go with the connection; its plugin starts nothing there
    cwd.mkdir()  # even so, a helper started in a folder made again refuses the stale descriptor
    helper_bound.sent.clear()
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "binding"}
    assert helper_bound.sent == []


def test_helper_unsafe_cleanup_root_is_retained_without_traversal(quiet, helper_bound, monkeypatch):
    from latet.cli import connect

    from .conftest import run_cli

    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    root = helper_bound.descriptor.parent
    before = helper_bound.descriptor.read_bytes()
    grants, entries, scan, recursive = folders.folder_grants, Path.iterdir, os.scandir, Path.rglob

    def unsafe(path):
        owner, allowed = grants(path)
        return (owner, [*allowed, folders.Grant("S-1-1-0", 0x100, False)]) if Path(path) == root else (owner, allowed)

    def no_traversal(path):
        if path == root:
            pytest.fail("unsafe helper root was traversed during disconnect")
        return entries(path)

    def no_scan(path):
        if not isinstance(path, int) and Path(path) == root:
            pytest.fail("unsafe helper root was scanned during disconnect")
        return scan(path)

    def no_recursive_scan(path, *args, **kwargs):
        if path in (root, root.parent):
            pytest.fail("recursive disconnect scan started before rejecting the unsafe helper child")
        return recursive(path, *args, **kwargs)

    monkeypatch.setattr(folders, "folder_grants", unsafe)
    monkeypatch.setattr(Path, "iterdir", no_traversal)
    monkeypatch.setattr(os, "scandir", no_scan)
    monkeypatch.setattr(Path, "rglob", no_recursive_scan)
    monkeypatch.setattr(connect, "_revoked", lambda *_args: (True, None))
    with helper_bound.diagnostic_credentials():
        done = run_cli("disconnect", "--yes")
    assert done.exit_code == 1 and "retained" in done.output.lower(), done.output
    assert helper_bound.descriptor.read_bytes() == before
    assert connection.load_connection() is not None  # Selected roots remain safe; only the helper child is unsafe.
    helper_bound.sent.clear()
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "binding"}
    assert helper_bound.sent == []


@pytest.mark.parametrize(
    ("label", "token"),
    [
        ("empty", ""),
        ("api-key", "latet_key_" + "z" * 43),
        ("admin", "latet_adm_" + "z" * 43),
        ("local", "owned-local-bridge-token"),
        ("short", "latet_pat_" + "z" * 42),
        ("long", "latet_pat_" + "z" * 44),
        ("non-ascii", "latet_pat_" + "z" * 42 + "\u00e9"),
        ("leading-space", " " + existing.TOKEN),
        ("trailing-space", existing.TOKEN + " "),
    ],
    ids=lambda value: (
        value
        if value
        in {"empty", "api-key", "admin", "local", "short", "long", "non-ascii", "leading-space", "trailing-space"}
        else "token"
    ),
)
def test_helper_rejects_saved_ineligible_pat_variants_before_transport(helper_bound, label, token):
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    envelope = helper_bound.envelope()
    envelope["token"] = token
    code, result = helper_bound.invoke(envelope)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "bad_input"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1


@pytest.mark.parametrize(
    "authority",
    ["aws-sessions", "admin", "machines", "provider", "raw-path", "method", "headers", "url", "fingerprint", "extra"],
)
def test_helper_rejects_saved_request_authority_expansion(helper_bound, authority):
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    envelope = helper_bound.envelope()
    if authority in {"aws-sessions", "admin", "machines", "provider", "raw-path"}:
        envelope["op"] = "../../admin" if authority == "raw-path" else authority
    else:
        envelope[authority] = {"x-owned": "forbidden"} if authority == "headers" else "forbidden"
    code, result = helper_bound.invoke(envelope)
    assert code == 2 and result == {"v": 1, "ok": False, "code": "bad_input"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1


@pytest.mark.parametrize("syntax", ["duplicate", "NaN", "Infinity", "-Infinity", "malformed", "trailing"])
def test_helper_rejects_saved_json_syntax_before_transport(helper_bound, syntax):
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    helper_bound.sent.clear()
    raw = json.dumps(helper_bound.envelope(), separators=(",", ":")).encode("utf-8")
    if syntax == "duplicate":
        raw = raw.replace(b'"v":1', b'"v":1,"v":1', 1)
    elif syntax in {"NaN", "Infinity", "-Infinity"}:
        raw = raw.replace(b'"v":1', b'"v":' + syntax.encode("ascii"), 1)
    elif syntax == "malformed":
        raw = raw[:-1]
    else:
        raw += b"{}"
    code, result = helper_bound.invoke(helper_bound.envelope(), input_buffer=io.BytesIO(raw))
    assert code == 2 and result == {"v": 1, "ok": False, "code": "bad_input"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1


def test_helper_complete_stdout_node_budget_includes_envelope_values(helper_bound, monkeypatch):
    # Network object count99997 plus the success envelope's three added nodes =100000, keys count bytes only.
    items = [""] * 99_994
    response = {"version": "unit", "extra": items}
    monkeypatch.setitem(_RESPONSES, "ping", response)
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 0 and result == {"v": 1, "ok": True, "response": response}
    helper_bound.sent.clear()
    items.append("")
    code, result = helper_bound.invoke(helper_bound.envelope())
    assert code == 2 and result == {"v": 1, "ok": False, "code": "response"}
    assert helper_bound.sent == [("ping", None)]


@pytest.mark.parametrize("variation", ["default-port", "one-trailing-slash"])
def test_helper_canonical_space_variations_keep_same_bound_route(helper_bound, variation):
    envelope = helper_bound.envelope()
    if variation == "default-port":
        envelope["bridge"] = envelope["bridge"].replace("https://latet.acme.lan/", "https://latet.acme.lan:443/", 1)
    else:
        envelope["bridge"] += "/"
    code, result = helper_bound.invoke(envelope)
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["ping"]}
    assert helper_bound.sent == [("ping", None)] and len(helper_bound.created) == 1


def test_helper_refuses_approval_without_its_original_twenty_second_remainder(helper_bound, monkeypatch):
    from latet import claude_bridge

    body = {"id": "a" * 32}
    code, result = helper_bound.invoke(helper_bound.envelope("approvals/wait", body))
    assert code == 0 and result == {"v": 1, "ok": True, "response": _RESPONSES["approvals/wait"]}
    assert helper_bound.sent == [("approvals/wait", body)] and len(helper_bound.created) == 1
    helper_bound.sent.clear()
    original_clock, read_binding = time.monotonic, claude_bridge._read_binding
    elapsed = [0.0]

    def bound_then_elapsed(*args, **kwargs):
        bound = read_binding(*args, **kwargs)  # Real successful descriptor/connection admission, no wrapper bypass.
        elapsed[0] = 15.0  # Original30s budget now has <20s, the server's actual fixed HOLD.
        return bound

    monkeypatch.setattr(time, "monotonic", lambda: original_clock() + elapsed[0])
    monkeypatch.setattr(claude_bridge, "_read_binding", bound_then_elapsed)
    code, result = helper_bound.invoke(helper_bound.envelope("approvals/wait", body))
    assert code == 2 and result == {"v": 1, "ok": False, "code": "deadline"}
    assert helper_bound.sent == [] and len(helper_bound.created) == 1
