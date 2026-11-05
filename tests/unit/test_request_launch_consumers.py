"""Finite request confidentiality consumer witnesses, with actual transport proofs."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse

from latet.bridge.api import Bridge
from latet.config import Config, ProjectEntry
from latet.engine import vault as vault_module
from latet.engine.vault import Vault
from latet.gateway import forward, ws
from latet.gateway.body_coverage import PROTECTED_LAUNCH
from latet.gateway.capture import Capture
from latet.gateway.forward import Forwarder
from latet.gateway.launch_keys import Binding, LocalLaunchKeys
from latet.gateway.local_launch import REQUEST_LAUNCH, local_request, take_local_launch
from latet.gateway.outcome import Block, Forward, Pass
from latet.gateway.recorder import Outcome
from latet.gateway.routes import RouteTable, Upstream
from latet.projects import Projects

from .test_request_launch_confidentiality import _ANSWER, _SHAPE
from .test_request_leaks import _Answer, _request
from .test_request_outcomes import OPENAI, _envelope, _prepared, _strict
from .test_response_sink_failures import _TIMEOUT, _sinks, _wire
from .test_response_ws_lifecycle import _Downstream, _Fixture, _Profile, _Wire

_SHORT = "latet_lk_notes"
_ESCAPED = "%6C" + _SHAPE[1:]
_BOUND = 16 * 1024
_REASON = PROTECTED_LAUNCH


def _contains_shape(text: str) -> bool:
    return _SHAPE in text or _ESCAPED in text


def _off(mediator, project: Path) -> None:
    project.mkdir()
    config = Config(ner="off", projects=[ProjectEntry(path=str(project), enabled=False)])
    mediator.engine.scopes = Projects(config, mediator.engine.vault, files=False)


def _capture_text(directory: Path) -> str:
    paths = list(directory.glob("*.jsonl"))
    assert len(paths) <= 1, "at most one capture"
    return "".join(path.read_text("utf-8") for path in paths)


def _history_text(recorder, store) -> str:
    assert recorder.drain(_TIMEOUT), "bounded history drain"
    public = store.list()[0]
    assert len(public) <= 1, "at most one history entry"
    return json.dumps((public, [store.detail(row["id"]) for row in public]), ensure_ascii=False)


async def _http_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, protected: bool) -> None:
    assert len(body) <= _BOUND and len(_ANSWER) <= _BOUND
    original = _request(body)
    project = tmp_path / "off-project"
    scope = dict(original.scope)
    scope["headers"] = [*original.scope["headers"], (b"x-latet-project", str(project).encode())]
    request = Request(scope, original.receive)
    directory = tmp_path / "capture"
    answer = _Answer(_ANSWER)
    sent: list[bool] = []
    passes: list[bool] = []
    finalize = forward.finalize

    def observed(arena, *args, **kwargs):
        decision = finalize(arena, *args, **kwargs)
        passes.append(isinstance(decision, Pass | Forward))
        return decision

    def provider(incoming: httpx.Request) -> httpx.Response:
        assert not sent and len(incoming.content) <= _BOUND, "one bounded provider attempt"
        sent.append(incoming.content == body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=answer)

    monkeypatch.setattr(forward, "finalize", observed)
    with _sinks(tmp_path) as (mediator, recorder, store):
        _off(mediator, project)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            forwarder = Forwarder(RouteTable([OPENAI]), Capture(directory), client, mediator, recorder)
            response = await asyncio.wait_for(forwarder.handle(request), _TIMEOUT)
            if isinstance(response, StreamingResponse):
                status, wire = await asyncio.wait_for(_wire(response, request), _TIMEOUT)
            else:
                status, wire = response.status_code, bytes(response.body)
        history = _history_text(recorder, store)

    capture = _capture_text(directory)
    # D5: an off project's request is refused, never sent unread; a protected value is refused before that.
    checks = {
        "no sendable decision for an off project": not any(passes),
        "no attempt": sent == [],
        "actual provider closure matches attempt": answer.closed == bool(sent),
    }
    if protected:
        checks["fixed refusal without value"] = (
            400 <= status < 500 and _REASON in wire.decode() and not _contains_shape(wire.decode())
        )
        checks["protected value absent from actual sinks"] = not _contains_shape(capture) and not _contains_shape(
            history
        )
    else:
        checks["refused as off, saying how to go on"] = status == 400 and "Latet is off" in wire.decode()
    assert all(checks.values()), checks


@pytest.mark.parametrize("case", ["ordinary", "schema-key", "metadata-key"])
async def test_http_protects_declared_definition_and_metadata_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    schema_key = _SHAPE if case == "schema-key" else _SHORT
    metadata_key = _SHAPE if case == "metadata-key" else _SHORT
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "ordinary request"}],
        "metadata": {metadata_key: "ordinary note"},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "query",
                    "parameters": {"type": "object", "properties": {schema_key: {"type": "string"}}},
                },
            }
        ],
    }
    await _http_off(tmp_path, monkeypatch, json.dumps(body).encode(), case != "ordinary")


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-duplicate", "protected-losing-value"])
async def test_http_off_screens_losing_raw_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    first = _ESCAPED if protected else _SHORT
    body = (
        '{"model":"m","messages":[{"role":"user","content":' + json.dumps(first) + ',"content":"ordinary winner"}]}'
    ).encode()
    assert json.loads(body)["messages"][0]["content"] == "ordinary winner", "the parser keeps the benign winner"
    await _http_off(tmp_path, monkeypatch, body, protected)


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-final", "protected-final"])
def test_amended_final_covered_text_is_checked_before_proof(tmp_path: Path, protected: bool) -> None:
    body = b'{"model":"m","messages":[{"role":"user","content":"ordinary original"}]}'
    with _sinks(tmp_path) as (mediator, recorder, _):
        prepared = _prepared(mediator, body)
        try:
            initial_proof = prepared.body_proof
            assert initial_proof is not None and not prepared.block_reason and not prepared.off
            [(slot, _, _)] = prepared.pending
            slot.put(_SHAPE if protected else _SHORT)
            prepared.amended = True
            amended_bytes = mediator._serialize_body(prepared.document, prepared.encoding)
            prepared.body = amended_bytes
            mediator._check(prepared)
            prepared.refresh_recording(logical=bool(prepared.block_reason), serialized=prepared.body)
            envelope = _envelope(body)
            decision = _strict(prepared, envelope)
            proof = prepared.body_proof
            snapshot = recorder.freeze_request(
                Outcome({}, "openai", "/v1/chat/completions", "http", time.time(), prepared)
            )
            assert snapshot is not None, "expected an actual Recorder request snapshot"
            if protected:
                recorded = snapshot.sent
                recorded_text = recorded.decode() if isinstance(recorded, bytes) else recorded
                checks = {
                    "fixed refusal": isinstance(decision, Block)
                    and decision.code == "refused"
                    and decision.reason == _REASON,
                    "no usable proof for amended bytes": proof is None
                    or proof.payload is not amended_bytes
                    or not proof.proves(envelope),
                    "actual categorical recorder handoff": snapshot.block_reason == _REASON,
                    "protected original absent from recorder handoff": (
                        all(not _contains_shape(block.text) for block in snapshot.blocks)
                        and (recorded_text is None or not _contains_shape(recorded_text))
                    ),
                }
            else:
                checks = {
                    "exact Forward with refreshed proof": (
                        isinstance(decision, Forward)
                        and decision.payload is amended_bytes
                        and proof is not None
                        and proof is not initial_proof
                        and decision.proof is proof
                        and proof.payload is amended_bytes
                        and proof.proves(envelope)
                    ),
                    "actual recorder handoff of amended block and bytes": (
                        snapshot.block_reason is None
                        and any(block.text == _SHORT for block in snapshot.blocks)
                        and snapshot.sent == amended_bytes
                    ),
                }
            assert all(checks.values()), checks
        finally:
            if prepared.lease is not None:
                prepared.lease.release()


@pytest.mark.parametrize("case", ["ordinary", "protected-handshake", "protected-frame"])
async def test_ws_admitted_metadata_and_frame_precede_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    profile = _Profile(client_frames=1, provider_frames=3, asgi_sends=6, owners=1, engine_jobs=10)
    directory = tmp_path / "capture"
    handshake = asyncio.Event()
    frame_result = asyncio.Event()
    off_frame: bool | None = None
    downstream_send, provider_send = _Downstream._send, _Wire.send

    async def observe_downstream(client, message):
        await downstream_send(client, message)
        kind = message["type"]
        if kind in ("websocket.accept", "websocket.close"):
            handshake.set()
        if kind == "websocket.send" and json.loads(message.get("text", "{}")).get("type") == "error":
            frame_result.set()

    async def observe_provider(wire, frame):
        await provider_send(wire, frame)
        frame_result.set()

    monkeypatch.setattr(_Downstream, "_send", observe_downstream)
    monkeypatch.setattr(_Wire, "send", observe_provider)
    fixture = _Fixture(profile)
    agent = _ESCAPED if case == "protected-handshake" else _SHORT
    fixture.client.socket.scope["headers"].append((b"user-agent", agent.encode()))
    route = Upstream(
        "relay",
        "/relay",
        "http://127.0.0.1:1",
        envelope_expected=frozenset({("model", "m")}),
    )
    with _sinks(tmp_path) as (mediator, recorder, store), closing(LocalLaunchKeys(b"r" * 32)) as owner:
        project = tmp_path / "off-project"
        _off(mediator, project)
        mediator.engine.attach(mediator.engine.session("session-id:5f4a3e00-0000-4000-8000-00000000f4a3"), str(project))
        scopes = mediator.engine.scopes
        assert isinstance(scopes, Projects)
        configured = scopes.resolve(str(project))
        opened = owner.commit_open(
            owner.begin_open("computer"),
            Binding(str(project.resolve()).encode(), configured.root.encode(), configured.id),
        )
        hold = owner.reserve_hold(opened["launch_id"], "computer", 0)
        assert hold.acknowledge() == 1
        fixture.client.socket.scope["headers"].append((b"x-latet-launch-key", opened["key"].encode()))
        fixture.client.socket.scope[REQUEST_LAUNCH] = local_request(
            take_local_launch(fixture.client.socket.scope, owner, computer=True)
        )
        fixture.patch(monkeypatch, mediator, recorder)
        decide = ws.decide

        def observe_decide(arena, *args, **kwargs):
            nonlocal off_frame
            decision = decide(arena, *args, **kwargs)
            off_frame = isinstance(decision, Pass | Forward)
            return decision

        monkeypatch.setattr(ws, "decide", observe_decide)
        fixture.task = asyncio.create_task(
            ws.relay(fixture.client.socket, RouteTable([route]), Capture(directory), mediator, recorder)
        )
        try:
            await asyncio.wait_for(handshake.wait(), _TIMEOUT)
            # D5: an off project's socket is refused at its handshake, so no frame is ever read or sent.
            await fixture.join()
            history = _history_text(recorder, store)
        finally:
            try:
                await fixture.cleanup()
            finally:
                hold.stop()
                await hold.drain()
                hold.close()
    capture = _capture_text(directory)
    # D5: Latet off in the launch's project refuses the socket before it connects, whatever it would carry.
    checks = {
        "no guarded connect for an off project": fixture.wire.opens == 0,
        "no frame decided": off_frame is None,
        "nothing reaches the provider": fixture.wire.sent_types == [],
        "protected spelling absent from actual sinks": not _contains_shape(capture) and not _contains_shape(history),
        "fixed handshake refusal": fixture.client.close_codes() == [1008],
    }
    assert all(checks.values()), checks


@pytest.mark.parametrize("protected", [False, True], ids=["ordinary-mask", "protected-mask"])
@pytest.mark.parametrize("store", [False, True], ids=["no-store", "store-reading"])
async def test_the_vaults_prefetch_filters_only_where_a_store_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected: bool, store: bool
) -> None:
    """Root's ruling on the cost of P2-6a's re-review R-2: ``Vault.prefetch`` (and ``prefetch_fakes``) is the one point
    that keeps a text holding a live launch key from a store's lookup, and a vault whose prefetch reads no store
    (``reads_store`` False: a computer's) returns before it, so the filter costs nothing where nothing is looked up. A
    property of the vault, never a machine branch in the request path. Through the plugin's mask call."""
    token = "bridge-fixture-token"
    project = tmp_path / "off-project"
    text = _ESCAPED if protected else _SHORT
    body = json.dumps(
        {"session": "request-confidentiality", "cwd": str(project), "items": [{"text": text, "kind": "user_text"}]}
    ).encode()
    assert len(body) <= _BOUND
    screened: list[str] = []
    asked: list[str] = []
    screen = vault_module.safe_text

    def counted(value: str) -> bool:
        screened.append(value)
        return screen(value)

    monkeypatch.setattr(vault_module, "safe_text", counted)
    monkeypatch.setattr(Vault, "reads_store", store, raising=False)  # as a store-reading vault (a server's) says
    monkeypatch.setattr(Vault, "_prefetch", lambda _vault, values: asked.extend(values))
    monkeypatch.setattr(Vault, "_prefetch_fakes", lambda _vault, values: asked.extend(values))
    with _sinks(tmp_path) as (mediator, recorder, record):
        _off(mediator, project)
        bridge = Bridge(mediator, token, recorder)
        transport = httpx.ASGITransport(app=Starlette(routes=bridge.routes()))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:5283", trust_env=False) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/_latet/engine/v1/mask",
                    content=body,
                    headers={"content-type": "application/json", "x-latet-token": token},
                ),
                _TIMEOUT,
            )
        history = _history_text(recorder, record)

    checks = {
        "no store ever sees a key's shape": not any(_contains_shape(value) for value in asked),
        "protected original absent from bridge result and history": (
            not _contains_shape(response.text) and not _contains_shape(history)
        ),
    }
    if store:
        checks["a store-reading vault screens what it is handed"] = text in screened
        checks["it asks its store for the ordinary text alone"] = (text in asked) is not protected
    else:
        checks["a vault that reads no store does no filter work"] = screened == []
        checks["and asks nothing"] = asked == []
    if protected:
        checks["fixed authenticated content refusal"] = 400 <= response.status_code < 500 and _REASON in response.text
    else:
        items = response.json().get("items") or []
        checks["ordinary actual mask result"] = (
            response.status_code == 200 and len(items) == 1 and items[0]["text"] == text
        )
    assert all(checks.values()), checks
