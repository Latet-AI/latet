"""Local gateway launch authority through actual HTTP preparation and send."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from latet.config import Config, Profile, ProjectEntry
from latet.engine.pipeline import request_scope
from latet.gateway import forward, request_transport
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.launch_keys import LocalLaunchKeys
from latet.gateway.outcome import Forward, Pass
from latet.projects import Projects

from .test_request_launch_confidentiality import _ANSWER
from .test_request_leaks import _Answer
from .test_request_outcomes import OPENAI
from .test_response_sink_failures import _TIMEOUT, _sinks

_SESSION = "7e57a000-0000-4000-8000-000000000001"  # a Claude Code session id: a UUID
_BRIDGE = "/_latet/engine/v1"
_TOKEN = "synthetic-local-bridge-token"
_CARRIER = "x-latet-launch-key"
_BODY = b'{"model":"m","messages":[{"role":"user","content":"ordinary request"}]}'


@pytest.mark.parametrize("case", ["active", "absent", "unknown", "claimed-off", "ended", "end-at-gate"])
async def test_local_launch_authority_at_actual_http_send(tmp_path: Path, monkeypatch, case: str) -> None:
    project = tmp_path / "registered-launch"
    project.mkdir()
    project = project.resolve(strict=True)
    capture = tmp_path / "capture"
    auth = {"x-latet-token": _TOKEN}
    owner = LocalLaunchKeys(b"r" * 32)
    held = None
    sends: list[bool] = []
    scope_checks: list[bool] = []
    scope_at_send: list[bool] = []
    expected_scope = None
    stale: list[bool] = []
    answer = _Answer(_ANSWER)
    finalize = forward.finalize
    real_send = request_transport.send

    def observed(arena, *args, **kwargs):
        decision = finalize(arena, *args, **kwargs)
        selection = request_scope.get()
        if case != "claimed-off":
            scope_checks.append(
                selection is not None
                and selection.session is arena.session
                and selection.scope is expected_scope
                and not arena.off
                and isinstance(decision, (Forward, Pass))
            )
        return decision

    def provider(request: httpx.Request) -> httpx.Response:
        assert not sends and len(request.content) <= 16 * 1024, "one bounded provider send"
        sends.append(_CARRIER not in request.headers)
        scope_at_send.append(request_scope.get() is not None)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    monkeypatch.setattr(forward, "finalize", observed)
    try:
        with _sinks(tmp_path) as (mediator, recorder, store):
            scopes = Projects(
                Config(
                    ner="off",
                    projects=[ProjectEntry(path=str(project), enabled=case != "claimed-off", profile=Profile.SECRETS)],
                ),
                mediator.engine.vault,
                files=False,
            )
            mediator.engine.scopes = scopes
            selected_project = scopes.resolve(str(project))
            assert selected_project.lowers and selected_project.scope is not None
            expected_scope = selected_project.scope if case in ("active", "end-at-gate") else selected_project.safe
            route = replace(
                OPENAI,
                envelope_expected=OPENAI.envelope_expected,
            )
            app = create_app(
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
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283") as client:
                    opened = await asyncio.wait_for(
                        client.post(_BRIDGE + "/launches", headers=auth, json={"folder": str(project)}), _TIMEOUT
                    )
                    assert opened.status_code == 200
                    opened_fields = opened.json()
                    launch_id, key = opened_fields["launch_id"], opened_fields["key"]
                    assert owner.verify(key, "computer").code == "pending"
                    held = owner.reserve_hold(launch_id, "computer", 0)
                    assert held.acknowledge() == 1
                    assert owner.verify(key, "computer").code == "active"
                    marker = await asyncio.wait_for(
                        client.post(
                            _BRIDGE + "/scope",
                            headers=auth,
                            json={
                                "session": _SESSION,
                                "cwd": str(project),
                                "items": [],
                                **({"launch_id": launch_id} if case in ("absent", "claimed-off") else {}),
                            },
                        ),
                        _TIMEOUT,
                    )
                    assert marker.status_code == 200
                    if case == "ended":
                        ended = await asyncio.wait_for(
                            client.post(_BRIDGE + f"/launches/{launch_id}/end", headers=auth, json={}), _TIMEOUT
                        )
                        assert ended.status_code == 200 and ended.json() == {"ended": True}
                    if case == "end-at-gate":

                        async def end_before_actual_gate(client, built):
                            assert not stale and scope_checks == [True]
                            assert owner.end(launch_id, "computer")
                            result = await real_send(client, built)
                            stale.append(isinstance(result, request_transport.SendRefusal) and result.code == "stale")
                            return result

                        monkeypatch.setattr(request_transport, "send", end_before_actual_gate)

                    headers = {"x-claude-code-session-id": _SESSION, "content-type": "application/json"}
                    supplied = key
                    if case == "unknown":
                        supplied = "latet_lk_" + base64.urlsafe_b64encode(b"u" * 32).rstrip(b"=").decode("ascii")
                        assert owner.verify(supplied, "computer").code == "unknown"
                    if case not in ("absent", "claimed-off"):
                        headers[_CARRIER] = supplied
                    response = await asyncio.wait_for(
                        client.post("/openai/v1/chat/completions", headers=headers, content=_BODY), _TIMEOUT
                    )
                assert recorder.drain(_TIMEOUT)
                summary = store.list()[0]
                assert len(summary) <= 1
                history = json.dumps((summary, [store.detail(row["id"]) for row in summary]))
            paths = list(capture.glob("*.jsonl"))
            assert len(paths) <= 1
            raw = "".join(path.read_text("utf-8") for path in paths)
            secret_clean = all(secret not in raw and secret not in history for secret in (key, supplied))
    finally:
        try:
            if held is not None:
                held.stop()
                try:
                    await held.drain()
                finally:
                    held.close()
        finally:
            owner.close()

    expected_send = case in ("active", "absent", "unknown")
    checks = {
        "actual send with carrier removed": sends == ([True] if expected_send else []),
        "actual provider response and close": (
            response.status_code == 200 and response.content == _ANSWER
            if expected_send
            else response.status_code >= 400
        )
        and answer.closed == expected_send,
        "request-local verified or tightening-only scope": (
            scope_checks == ([] if case in ("ended", "claimed-off") else [True])
        ),
        "claimed off fixed refusal": case != "claimed-off"
        or (response.status_code == 400 and "request claimed a project where Latet is off" in response.text),
        "actual provider runs within request context": scope_at_send == ([True] if expected_send else []),
        "real late freshness refusal": stale == ([True] if case == "end-at-gate" else []),
        "no launch credential in actual sinks": secret_clean,
    }
    assert all(checks.values()), checks


@pytest.mark.parametrize(
    ("path", "carrier", "status"),
    [
        ("/", "latet_lk_" + base64.urlsafe_b64encode(b"u" * 32).rstrip(b"=").decode("ascii"), 503),
        ("/_k/not-a-key", None, 400),
    ],
    ids=["valid-key-no-owner", "malformed-reserved-path"],
)
async def test_local_carrier_denial_precedes_any_route(path: str, carrier: str | None, status: int) -> None:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    handled: list[bool] = []

    async def dashboard(_request: Request) -> JSONResponse:
        handled.append(True)
        return JSONResponse({"handler": "reached"})

    app = create_app(GatewayConfig(fixed_routes=[]))
    app.router.routes.insert(0, Route("/", dashboard))
    headers = {_CARRIER: carrier} if carrier is not None else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client:
        rejected = await asyncio.wait_for(client.get(path, headers=headers), _TIMEOUT)
        wrong_host = await asyncio.wait_for(
            client.get(path, headers={**headers, "host": "untrusted.invalid"}), _TIMEOUT
        )
    checks = {
        "fixed carrier refusal": rejected.status_code == status
        and rejected.headers.get("x-should-retry") == "false"
        and rejected.json().get("error")
        == ("request launch authority unavailable" if status == 503 else "request launch authority refused"),
        "LocalOnly denies before carrier dispatch": wrong_host.status_code == 403
        and "host not allowed" in wrong_host.text,
        "no dashboard or handler reached": not handled,
    }
    assert all(checks.values()), checks


@pytest.mark.parametrize("case", ["unregistered-non-cc", "registered-anthropic", "registered-wrong-profile"])
async def test_local_absent_carrier_requires_registered_claude_messages(tmp_path: Path, monkeypatch, case: str) -> None:
    project = tmp_path / "registered-legacy"
    project.mkdir()
    project = project.resolve(strict=True)
    positive = case == "registered-anthropic"
    registered = case != "unregistered-non-cc"
    route = replace(
        OPENAI,
        name="anthropic" if positive else "openai",
        prefix="/anthropic" if positive else "/openai",
        envelope_expected=OPENAI.envelope_expected,
    )
    sent: list[bool] = []
    scope_seen: list[bool] = []
    answer = _Answer(b'{"id":"msg_test","type":"message","role":"assistant","model":"m","content":[]}')
    real_send = request_transport.send

    async def observed(
        client: httpx.AsyncClient, built: request_transport.BuiltRequest
    ) -> httpx.Response | request_transport.SendRefusal:
        assert not scope_seen
        arena = built.authorized.arena
        selected = request_scope.get()
        scope_seen.append(
            arena.off
            and selected is not None
            and selected.session is arena.session
            and selected.launch == "trusted"  # the plugin's registered project decided it (one launch authority)
        )
        return await real_send(client, built)

    def provider(request: httpx.Request) -> httpx.Response:
        assert not sent and len(request.content) <= 16 * 1024
        sent.append(_CARRIER not in request.headers)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    monkeypatch.setattr(request_transport, "send", observed)
    with _sinks(tmp_path) as (mediator, recorder, store):
        scopes = Projects(
            Config(ner="off", projects=[ProjectEntry(path=str(project), enabled=False)]),
            mediator.engine.vault,
            files=False,
        )
        mediator.engine.scopes = scopes
        configured = scopes.resolve(str(project))
        project_id = configured.id
        assert configured.scope is not None and not configured.scope.enabled
        app = create_app(
            GatewayConfig(
                mediator=mediator,
                recorder=recorder,
                bridge_token=_TOKEN,
                fixed_routes=[route],
                client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False),
            )
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:5283") as client,
        ):
            headers = {"x-latet-project": str(project), "content-type": "application/json"}
            if registered:
                acknowledged = await asyncio.wait_for(
                    client.post(
                        _BRIDGE + "/scope",
                        headers={"x-latet-token": _TOKEN},
                        json={"session": _SESSION, "cwd": str(project), "items": []},
                    ),
                    _TIMEOUT,
                )
                assert acknowledged.status_code == 200
                session = mediator.engine.session(f"cc:{_SESSION}")
                assert session.local_plugin_project == project_id and not session.launch_required
                headers["x-claude-code-session-id"] = _SESSION
            path = "/anthropic/v1/messages" if positive else "/openai/v1/chat/completions"
            response = await asyncio.wait_for(client.post(path, headers=headers, content=_BODY), _TIMEOUT)
        assert recorder.drain(_TIMEOUT)
        [row] = store.list()[0]

    # What its launch was decided as (root's ruling I-5): the plugin's binding, else a claim.
    assert row["launch"] == ("trusted" if positive else "claimed"), row
    if positive:  # D5: trusted through the plugin's binding, a project where Latet is off still sends nothing
        assert response.status_code == 400 and "Latet is off in this project" in response.text
        assert not sent and not scope_seen and not answer.closed
    else:
        assert response.status_code == 400 and "request claimed a project where Latet is off" in response.text
        assert not sent and not scope_seen and not answer.closed
