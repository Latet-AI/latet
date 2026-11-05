"""P1-5 of ``notes/server-request-coverage-plan.md``: the plugin's bridge records what it read of a request it never
sends (a route check, a tool call stopped, its final check) as an ``Observation``, which nothing makes a decision of, so
that none of it reaches S1-S4; and the recorder is fed an immutable snapshot of each exchange, taken as it ends, so that
its record is what the exchange was then, whatever becomes of its arena afterwards, and a record that cannot be made,
queued or stored leaves nothing behind it, the exchange's hold released once all the same. What the history is given is
unchanged (P1-1's characterization judges that, ``phase_1_judge``)."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import enum
import gc
import json
import logging
import sys
import threading
import time
import types
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from latet.config import Config, IdentityConfig
from latet.gateway import app as gateway_app
from latet.gateway import outcome, request_transport
from latet.gateway import recorder as recorder_module
from latet.gateway.app import OnServer, create_app
from latet.gateway.capture import Capture, Exchange
from latet.gateway.coverage import envelope_of
from latet.gateway.forward import Forwarder
from latet.gateway.mediate import Prepared
from latet.gateway.outcome import NotSendable, Observation, ResponsePlan
from latet.gateway.recorder import Outcome, Recorder, RecordSnapshot
from latet.gateway.request_policy import current
from latet.gateway.request_transport import SendRefusal
from latet.gateway.routes import RouteTable
from latet.runtime import Runtime, make_runtime
from latet.server.grants import bridge_needs
from latet.state import LocalJobLock, LocalSentBlocks
from latet.storage.exchanges import ExchangeStore

from ..conftest import ROOT_KEY
from .test_request_leaks import _Answer, _Provider, _request
from .test_request_lifetimes import PEOPLE, _drained, _held, _mediator, _streaming
from .test_request_outcomes import EMAIL, OPENAI, _chat, _envelope, _prepared, _strict
from .test_request_outcomes import _mediator as _plain_mediator

ENGINE = "/_latet/engine/v1"
_CALL_ID = "call_Q7mZ2rT9xK4vB1nWcD8eF3hJ"
SECRET = "ghp_" + "Zq3vR8kL2mN5pT7wX9yB4cD6fH1jK0sA3eG5"
HTTP_SCOPE = {"type": "http", "method": "POST", "path": "/anthropic/v1/messages", "headers": []}


class _Sent:
    """Every request the gateway's client would have sent to a provider (none should leave)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(500, headers=[(b"content-type", b"application/json")], stream=_Answer(b'{"error": "x"}'))


@contextlib.contextmanager
def _computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Runtime, TestClient, _Sent, list]]:
    """A computer's runtime and app, recording; what its bridge hands the recorder (``Outcome``s, in order) kept."""
    sent = _Sent()
    monkeypatch.setattr(gateway_app, "make_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(sent)))
    config = Config(identity=IdentityConfig(enabled=False))
    rt = make_runtime(config, data=tmp_path / "data", config_file=tmp_path / "config.yaml", root_key=ROOT_KEY)
    recorder = rt.gateway.recorder
    assert recorder is not None and recorder.enabled
    given: list[Outcome] = []
    finish = recorder.finish

    def finishing(o: Outcome) -> None:
        given.append(o)
        finish(o)

    monkeypatch.setattr(recorder, "finish", finishing)
    try:
        with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:
            yield rt, client, sent, given
    finally:
        rt.close()


def _fake(client: TestClient, token: str) -> str:
    """The fake the plugin's session was shown in place of ``EMAIL`` (``mask``)."""
    body = {"session": "rc-plugin", "items": [{"text": f"write to {EMAIL}"}]}
    masked = client.post(f"{ENGINE}/mask", headers={"x-latet-token": token}, json=body)
    fake = masked.json()["items"][0]["text"].removeprefix("write to ")
    assert fake and fake != EMAIL
    return fake


def _three_records(client: TestClient, token: str, fake: str) -> list[int]:
    """The route check, a tool call stopped and the final check, each one the bridge records; their statuses."""
    headers = {"x-latet-token": token}
    calls = [
        ("route-check", {"session": "rc-plugin", "env": {}, "sources": {}, "entrypoint": "cli"}),
        ("outbound", {"session": "rc-plugin", "tool": "web_fetch", "values": [fake]}),
        ("verify", {"session": "rc-plugin", "slots": [{"text": f"key {SECRET}"}]}),
    ]
    return [client.post(f"{ENGINE}/{path}", headers=headers, json=body).status_code for path, body in calls]


def _no_send_capability(item: object) -> None:
    """``item`` (what the bridge recorded) is no arena a decision is finalized from, nor a decision: nothing is minted
    of it, and every step of the guarded transport refuses it, at S1/S2 (``validate_send``), S3 (``validate_connect``)
    and S4 (``validate_frame``); nor does a decision of another request pass with it as its arena."""
    http = envelope_of(HTTP_SCOPE, "http", "anthropic")
    handshake = envelope_of({"type": "websocket", "path": "/chatgpt/v1/responses", "headers": []}, "ws-handshake", "x")
    minted = len(outcome._MINTED)
    with pytest.raises(TypeError):
        outcome.decide(item, http)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        outcome.finalize(item, http, current(), ResponsePlan())  # type: ignore[arg-type]
    assert len(outcome._MINTED) == minted  # nothing minted of it
    with pytest.raises(NotSendable) as refused:
        outcome.sendable(item, None, http)
    assert refused.value.code == "not-a-decision"
    assert request_transport.validate_send(item, None, http, current(), 1) == SendRefusal("not-a-decision")
    assert request_transport.validate_connect(item, handshake, current(), []) == SendRefusal("not-a-decision")
    assert request_transport.validate_frame(item, None, http, current(), object()) == SendRefusal("unbuilt")
    # No arena is no worker authority: the current strict finalizer blocks it.
    blocked = outcome.decide(None, http)
    assert type(blocked) is outcome.Block
    with pytest.raises(NotSendable) as refusal:
        outcome.sendable(blocked, item, http)  # type: ignore[arg-type]
    assert refusal.value.code == "block"
    assert request_transport.validate_send(blocked, item, http, current(), 1) == SendRefusal("block")  # type: ignore[arg-type]
    body = _chat("hello")
    arena = _prepared(_plain_mediator(), body)
    theirs = _envelope(body)
    other = _strict(arena, theirs)
    assert type(other) is outcome.Forward
    assert outcome.sendable(other, arena, theirs).payload is arena.body
    # The SAME genuinely sendable strict decision cannot borrow an Observation.
    with pytest.raises(NotSendable) as foreign:
        outcome.sendable(other, item, theirs)  # type: ignore[arg-type]
    assert foreign.value.code == "foreign"
    refused_send = request_transport.validate_send(other, item, theirs, current(), 1)  # type: ignore[arg-type]
    assert refused_send == SendRefusal("foreign")


def test_bridge_observation_has_no_send_capability(tmp_path, monkeypatch):
    """Through the bridge's actual route-check, outbound and final-check endpoints, its token required: each records an
    ``Observation`` of what it read, never an arena (a ``Prepared``) a decision could be made of; none of it can be
    finalized, authorized or sent, and nothing is sent to any provider."""
    with _computer(tmp_path, monkeypatch) as (rt, client, sent, given):
        token = rt.gateway.bridge_token or ""
        fake = _fake(client, token)
        assert _three_records(client, "not the token", fake) == [401, 401, 401]
        assert client.post(f"{ENGINE}/verify", json={"session": "s", "slots": [{"text": SECRET}]}).status_code == 401
        assert given == []  # nothing recorded of a call refused its token
        assert _three_records(client, token, fake) == [200, 200, 200]
        assert rt.gateway.recorder is not None and rt.gateway.recorder.drain()
        assert [o.path for o in given] == ["/plugin/route-check", "/plugin/tool-call", "/plugin/final-check"]
        for o in given:
            item = o.prepared if o.prepared is not None else getattr(o, "observed", None)
            _no_send_capability(item)
            assert o.prepared is None and type(item) is Observation and o.transport == "plugin"
        assert [o.observed.source for o in given] == ["route-check", "outbound", "final-check"]
        assert sent.requests == []
    # A server's gate asks of a token what it asked before: nothing for the route check, ``mask`` for the two others.
    assert bridge_needs(f"{ENGINE}/route-check") == frozenset()
    assert bridge_needs(f"{ENGINE}/outbound") == bridge_needs(f"{ENGINE}/verify") == frozenset({"mask"})


def test_a_bridge_observation_is_recorded_as_before(tmp_path, monkeypatch):
    """Bridge records keep categorical summaries and private history groups, while authorized FULL detail retains
    original path/reason/tool and every original block, with no send capability."""
    with _computer(tmp_path, monkeypatch) as (rt, client, _, _given):
        token = rt.gateway.bridge_token or ""
        assert _three_records(client, token, _fake(client, token)) == [200, 200, 200]
        assert rt.gateway.recorder is not None and rt.gateway.recorder.drain()
        assert isinstance(rt.exchanges, ExchangeStore)
        summaries = rt.exchanges.list()[0]
        assert len(summaries) == 3
        group = rt.exchanges.history_group("cc:rc-plugin")
        retained = [rt.exchanges.detail(s["id"]) for s in summaries]
        assert all(d is not None and d["sensitive"] is not None for d in retained)
        details = {d["sensitive"]["path"]: d for d in retained}
    by_path = {path: d["summary"] for path, d in details.items()}
    assert set(by_path) == {"/plugin/route-check", "/plugin/tool-call", "/plugin/final-check"}
    for s in summaries:
        assert s["mode"] == "verify" and s["protocol"] == "anthropic" and s["transport"] == "plugin"
        assert s["agent"] == "claude-code" and "blocked" in s["flags"] and s["session"] == group
        assert s["path"] in by_path and s["block_reason"] == "refused"  # the check's fixed path, its category
        assert s["coverage"] == "unknown" and s["verified"] is False
    assert "route-bypass" in by_path["/plugin/route-check"]["flags"]
    assert "outbound:web-fetch" in by_path["/plugin/tool-call"]["flags"]
    assert "web_fetch" in details["/plugin/tool-call"]["sensitive"]["outbound"]
    assert "leak" in by_path["/plugin/final-check"]["flags"]
    assert (
        details["/plugin/final-check"]["sensitive"]["block_reason"]
        == "stopped by the Claude Code plugin before it was sent"
    )
    # Each text a block of its own, where it was in the list the plugin sent, its kind and who wrote it as before.
    blocks = {}
    for path, kept in details.items():
        assert kept is not None
        blocks[path] = [(b["path"], b["kind"], b["from_model"]) for b in kept["request"]["slots"]]
    assert blocks == {
        "/plugin/route-check": [],
        "/plugin/tool-call": [("$[0]", "outbound_tool", True)],
        "/plugin/final-check": [("$[0]", "other", False)],
    }


def _immutable(value: object, seen: set[int] | None = None) -> bool:
    """Whether ``value`` holds only what cannot change: texts, numbers, tuples, frozen sets and mappings shown read-only,
    frozen dataclasses of those, all the way down."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return True
    seen.add(id(value))
    if value is None or isinstance(value, str | bytes | int | float | bool | enum.Enum):
        return True
    if isinstance(value, tuple | frozenset):
        return all(_immutable(v, seen) for v in value)
    if isinstance(value, types.MappingProxyType):
        return all(_immutable(k, seen) and _immutable(v, seen) for k, v in value.items())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not params.frozen:
            return False
        return all(_immutable(getattr(value, f.name), seen) for f in dataclasses.fields(value))
    return False


def _tool_request() -> bytes:
    """An Anthropic request: a prompt with a value masked, a tool's output and the file it read (where it came from)."""
    use = {"type": "tool_use", "id": "toolu_t0", "name": "Read", "input": {"file_path": "/work/notes.txt"}}
    result = {"type": "tool_result", "tool_use_id": "toolu_t0", "content": f"call {EMAIL}"}
    messages = [
        {"role": "user", "content": f"write to {EMAIL}"},
        {"role": "assistant", "content": [use]},
        {"role": "user", "content": [result]},
    ]
    return json.dumps({"model": "claude-sonnet-4-20250514", "max_tokens": 10, "messages": messages}).encode()


def test_record_snapshot_survives_arena_disposal_without_mutation(tmp_path, monkeypatch):
    """The recorder's snapshot of an exchange is taken as it ends, from its arena and its outcome, without changing
    either: its decision still holds. It is immutable, holds nothing of the arena, and is what the history is given,
    whatever becomes of the arena and the outcome before the recorder's thread gets to it (here: every text written
    over, the document emptied, every list cleared)."""
    with _computer(tmp_path, monkeypatch) as (rt, client, _, _given):
        recorder = rt.gateway.recorder
        assert recorder is not None and recorder._executor is not None
        records: list[Any] = []
        store = recorder.store
        assert store is not None
        record = store.record
        monkeypatch.setattr(store, "record", lambda item: (records.append(item), record(item))[1])
        queued: list[tuple[Any, ...]] = []
        recording = recorder._record
        monkeypatch.setattr(recorder, "_record", lambda *args: (queued.append(args), recording(*args))[1])
        seen: dict[str, Any] = {}
        freeze = recorder.freeze_request

        # Where the exchange ends its request is frozen (the response's finalizer seals it), the answer screened after.
        def freezing(o: Outcome) -> Any:  # the arena as it ended, read before the snapshot is taken, and after
            arena = o.prepared
            assert isinstance(arena, Prepared) and arena.sealed is not None
            texts = [slot.get() for slot, _, _ in arena.pending]
            frozen = freeze(o)
            seen.update(arena=arena, texts=texts, flags=(arena.mode, arena.protocol, arena.masked_count))
            seen["unchanged"] = [slot.get() for slot, _, _ in arena.pending] == texts and outcome._unchanged(
                arena,
                arena.sealed,  # type: ignore[arg-type]
            )
            seen["headers"] = [name for name, _ in o.request_headers]
            seen["outcome"] = o
            return frozen

        monkeypatch.setattr(recorder, "freeze_request", freezing)
        gate = threading.Event()
        recorder._executor.submit(gate.wait, 10)  # the recorder's thread busy: the snapshot waits in its queue
        answered = client.post("/anthropic/v1/messages", content=_tool_request(), headers={"x-api-key": "sk-test"})
        assert answered.status_code == 500  # the provider's answer (500) handed on: the exchange is over
        assert seen["unchanged"]  # taking the snapshot changed nothing of the arena: its decision still holds
        arena: Prepared = seen.pop("arena")
        texts = seen["texts"]
        assert any(t.startswith("write to ") and EMAIL not in t for t in texts)
        # The arena and the outcome disposed of before the recorder's thread reads anything.
        for slot, _, _ in arena.pending:
            slot.set("written over")
        arena.document.clear()
        for listed in (arena.pending, arena.leaks, arena.uninspected, arena.outbound, arena.images):
            listed.clear()
        arena.block_reason, arena.mode, arena.protocol, arena.masked_count = "disposed", "passthrough", None, 0
        ended: Outcome = seen.pop("outcome")
        ended.request_headers.clear()
        ended.headers.clear()
        ended.prepared = None
        del arena, ended
        gate.set()
        assert recorder.drain()
    [kept] = records
    assert [b.masked for b in kept.blocks] == [t for t in texts if t]  # each text as it left, none written over
    assert any(b.source == "/work/notes.txt" for b in kept.blocks)  # where the tool's output came from
    assert (kept.summary["mode"], kept.summary["protocol"]) == seen["flags"][:2] and kept.summary["masked"] > 0
    assert kept.summary["block_reason"] is None and kept.summary["agent"] == "other"
    assert [name for name, _ in kept.request_headers] == seen["headers"]
    [(taken,)] = queued  # what was queued: the snapshot alone, immutable all the way down, nothing of the arena
    assert type(taken) is RecordSnapshot
    assert all(_immutable(getattr(taken, f.name)) for f in dataclasses.fields(taken))
    # The answer as the model wrote it, screened where the exchange ended and handed over frozen: nothing feeds it now.
    assert type(taken.capture) is recorder_module._ScreenedResponse
    with pytest.raises(dataclasses.FrozenInstanceError):
        taken.blocks = ()  # type: ignore[misc]


def _texts(store: ExchangeStore, exchange_id: str) -> list[str]:
    """The texts the history keeps of an exchange, block by block."""
    kept = store.detail(exchange_id)
    assert kept is not None
    return [slot["masked"] for slot in kept["request"]["slots"]]


def test_where_each_text_was_is_found_off_the_loop(tmp_path, monkeypatch):
    """Root's ruling on P1-5: where each text was in the request (its JSON path) is found in the recorder's thread, from
    the bytes the decision left with, never where the exchange ends: a paths function that takes long delays no answer,
    the next request's included, and the record keeps each text's path."""
    with _computer(tmp_path, monkeypatch) as (rt, client, _, _given):
        recorder = rt.gateway.recorder
        assert recorder is not None and isinstance(rt.exchanges, ExchangeStore)
        for name in ("json_paths", "string_paths", "occurrence_paths"):  # whichever finds the paths
            real = getattr(recorder_module, name, None)
            if real is None:
                continue

            def slow(*args: Any, real: Any = real, **kwargs: Any) -> Any:
                time.sleep(1.5)
                return real(*args, **kwargs)

            monkeypatch.setattr(recorder_module, name, slow)
        started = time.perf_counter()
        for _ in range(2):
            answered = client.post("/anthropic/v1/messages", content=_tool_request(), headers={"x-api-key": "sk-test"})
            assert answered.status_code == 500
        assert time.perf_counter() - started < 1.0  # neither answer waited for the paths
        assert recorder.drain(15)
        for summary in rt.exchanges.list()[0]:
            kept = rt.exchanges.detail(summary["id"])
            assert kept is not None
            paths = [slot["path"] for slot in kept["request"]["slots"]]
            tool_input = "$.messages[1].content[0].input.file_path"
            assert paths == ["$.messages[0].content", tool_input, "$.messages[2].content[0].content"]


def test_each_texts_path_found_in_what_was_sent_is_where_it_was_in_the_arena(tmp_path):
    """The paths the recorder's thread finds in the bytes that left are those the arena's own document gives
    (``json_paths``, as the recorder read them before): the same words twice in order, an object read as a JSON text (a
    format's schema), a value masked."""
    format_ = {"type": "json_schema", "json_schema": {"name": "x", "schema": {"x": {"y": 1}}}}
    messages = [
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "same"},
        {"role": "user", "content": f"write to {EMAIL}"},
    ]
    body = json.dumps({"model": "m", "messages": messages, "response_format": format_}).encode()
    arena = _prepared(_plain_mediator(), body)
    where = recorder_module.json_paths(arena.document)
    expected = [where.get((id(slot.container), slot.key), "$") for slot, _, _ in arena.pending if slot.get()]
    recorder, store = _bare_recorder(tmp_path)
    try:
        recorder.finish(Outcome({"user-agent": "x"}, "openai", "/v1/chat/completions", "http", 1.0, arena))
        assert recorder.drain()
        [summary] = store.list()[0]
        kept = store.detail(summary["id"])
        assert kept is not None
        assert [slot["path"] for slot in kept["request"]["slots"]] == expected
        assert "$.response_format.json_schema.schema" in expected and expected.count("$") == 0 and len(expected) >= 4
    finally:
        recorder.close()
        store.close()


def _bare_recorder(tmp_path: Path) -> tuple[Recorder, ExchangeStore]:
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    return Recorder(store, sent=LocalSentBlocks(), jobs=LocalJobLock()), store


def _kept_request(tmp_path: Path, arena: Prepared, *, payload: str | None = None) -> dict[str, Any]:
    recorder, store = _bare_recorder(tmp_path)
    try:
        recorder.finish(
            Outcome(
                {},
                "openai",
                "/v1/responses" if payload else "/v1/chat/completions",
                "ws" if payload else "http",
                1.0,
                arena,
                payload=payload,
            )
        )
        assert recorder.drain()
        [summary] = store.list()[0]
        kept = store.detail(summary["id"])
        assert kept is not None
        return kept["request"]
    finally:
        recorder.close()
        store.close()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            {"model": "m", "messages": [{"role": "user", "content": "same"}], "metadata": {"content": "same"}},
            ["$.metadata.content", "$.messages[0].content"],
        ),
        (
            {"model": "m", "metadata": {"content": "same"}, "messages": [{"role": "user", "content": "same"}]},
            ["$.metadata.content", "$.messages[0].content"],
        ),
        (
            # A stop sequence is checked text the scan never selects (metadata holds strings alone: an object there is
            # refused).
            {"model": "m", "stop": ["same"], "messages": [{"role": "user", "content": "same"}]},
            ["$.messages[0].content"],
        ),
    ],
    ids=["scan-order", "document-order-control", "unscanned-duplicate"],
)
def test_equal_texts_keep_their_protocol_occurrence_paths(tmp_path, document, expected):
    """Looking up paths by key and text swaps scanned occurrences and admits unscanned duplicates."""
    arena = _prepared(_plain_mediator(), json.dumps(document).encode())
    assert [slot["path"] for slot in _kept_request(tmp_path, arena)["slots"]] == expected


def test_repeated_outbound_references_keep_the_same_slot_path(tmp_path):
    """Recording an outbound reference must reuse its occurrence, not consume the next equal string's path."""
    arena = _prepared(_plain_mediator(), _chat("same"))
    slot = arena.pending[0][0]
    arena.outbound_slots.extend([slot, slot])
    arena.refresh_recording()  # mirror the production _mark_outbound worker after its additions
    assert [slot["path"] for slot in _kept_request(tmp_path, arena)["slots"]] == [
        "$.messages[0].content",
        "$.messages[0].content",
        "$.messages[0].content",
    ]


def test_equal_json_objects_do_not_steal_a_schema_slot_path(tmp_path):
    # Before the schema slot in the document, an equal object under the same key that is no slot of its own: a property
    # inside a tool's parameters, which are one slot whole (metadata holds strings alone: an object there is refused).
    parameters = {"type": "object", "properties": {"schema": {"type": "object"}}}
    document = {
        "model": "m",
        "tools": [{"type": "function", "function": {"name": "f", "parameters": parameters}}],
        "messages": [{"role": "user", "content": "hello"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": {"type": "object"}}},
    }
    arena = _prepared(_plain_mediator(), json.dumps(document).encode())
    assert [slot["path"] for slot in _kept_request(tmp_path, arena)["slots"]] == [
        "$.tools[0].function.parameters",
        "$.response_format.json_schema.schema",
        "$.messages[0].content",
    ]


def test_websocket_top_and_response_occurrences_keep_their_paths(tmp_path):
    frame = {
        "type": "response.create",
        "input": [{"role": "user", "content": "same"}],
        "metadata": {"content": "same"},
        "response": {"input": [{"role": "user", "content": "same"}]},
    }
    mediator = _plain_mediator()
    payload, arena = mediator.mask_ws_frame(json.dumps(frame), mediator.engine.session("ws-paths"))
    assert arena is not None
    assert [slot["path"] for slot in _kept_request(tmp_path, arena, payload=payload)["slots"]] == [
        "$.metadata.content",
        "$.input[0].content",
        "$.response.input[0].content",
    ]


def test_bedrock_count_paths_are_relative_to_the_decoded_request(tmp_path):
    from latet.gateway.cloud import BedrockUpstream

    inner = {"max_tokens": 10, "messages": [{"role": "user", "content": "hello"}]}
    body = {"input": {"invokeModel": {"body": base64.b64encode(json.dumps(inner).encode()).decode()}}}
    path = "/bedrock/us-east-1/model/anthropic.claude-x/count-tokens"
    arena = _plain_mediator().prepare(BedrockUpstream(), "POST", path, {}, json.dumps(body).encode())
    assert [slot["path"] for slot in _kept_request(tmp_path, arena)["slots"]] == ["$.messages[0].content"]


def test_equal_images_ignore_unscanned_duplicates_and_keep_their_text_paths(tmp_path):
    from ..fake_ocr import FakeOcr

    reader = FakeOcr()
    png = reader.screenshot(["same"])
    url = f"data:image/png;base64,{png}"
    document = {
        "model": "m",
        "metadata": {"unscanned": url},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
    }
    arena = _prepared(_plain_mediator(reader), json.dumps(document).encode())
    assert arena.block_reason is None and len(arena.images) == 2
    kept = _kept_request(tmp_path, arena)
    paths = ["$.messages[0].content[0].image_url.url", "$.messages[0].content[1].image_url.url"]
    assert [image["path"] for image in kept["images"]] == paths
    assert [slot["path"] for slot in kept["slots"]] == ["$.metadata.unscanned", *[path + "#text" for path in paths]]


def test_an_image_withheld_by_review_keeps_its_orphan_path(tmp_path):
    from latet.engine.types import SlotKind
    from latet.gateway.approvals import Decision, ImageDecision
    from latet.gateway.mediate import LiveItem

    from ..fake_ocr import FakeOcr

    reader = FakeOcr()
    mediator = _plain_mediator(reader)
    arena = _prepared(mediator, _chat("same", image=reader.screenshot(["same"])))
    work = arena.images[0]
    item = LiveItem(1, "same", SlotKind.IMAGE_TEXT, None, None, False, "same", image=work)
    mediator._apply_live_prepared(arena, [item], Decision(True, images=[ImageDecision(0, withhold=True)]))
    kept = _kept_request(tmp_path, arena)
    assert [(image["path"], image["state"]) for image in kept["images"]] == [("$", "withheld")]
    assert [slot["path"] for slot in kept["slots"]] == ["$.messages[0].content[0].text", "$#text"]


def test_a_tool_source_is_read_in_the_recorder_thread(tmp_path, monkeypatch):
    mediator = _plain_mediator()
    arena = mediator.prepare(OPENAI, "POST", "/v1/messages", {}, _tool_request())
    threads: list[str] = []
    source = recorder_module.tool_source

    def reading(value: Any) -> str | None:
        threads.append(threading.current_thread().name)
        return source(value)

    monkeypatch.setattr(recorder_module, "tool_source", reading)
    kept = _kept_request(tmp_path, arena)
    assert kept["slots"][-1]["source"] == "/work/notes.txt"
    assert threads and all(name.startswith("latet-recorder") for name in threads)


def test_a_refused_websocket_records_the_edited_tool_source(tmp_path):
    """The final check can refuse a masked turn; its unsent original frame does not describe the edited history."""
    mediator = _plain_mediator()
    frame = {
        "type": "response.create",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_c",
                "name": "Read",
                "arguments": json.dumps({"file_path": f"/work/{EMAIL}"}),
            },
            {"type": "function_call_output", "call_id": "call_c", "output": '{"card":4111111111111111}'},
        ],
    }
    payload, arena = mediator.mask_ws_frame(json.dumps(frame), mediator.engine.session("ws-refused-paths"))
    assert arena is not None and arena.block_reason and arena.masked_count > 0
    kept = _kept_request(tmp_path, arena, payload=payload)
    arguments, result = kept["slots"]
    assert EMAIL not in arguments["masked"]
    assert result["source"] == json.loads(arguments["masked"])["file_path"]


@pytest.mark.parametrize("refused", [False, True], ids=["sent", "refused-after-key-masking"])
def test_websocket_paths_use_the_final_masked_keys(tmp_path, refused):
    mediator = _plain_mediator()
    frame = {
        "type": "response.create",
        "input": [
            {"type": "local_shell_call", "call_id": "call_c", "action": {EMAIL: "same"}},
            {
                "type": "local_shell_call_output",
                "call_id": "call_c",
                "output": '{"card":4111111111111111}' if refused else "hello",
            },
        ],
    }
    payload, arena = mediator.mask_ws_frame(json.dumps(frame), mediator.engine.session("ws-key-paths"))
    assert arena is not None and bool(arena.block_reason) == refused
    [masked_key] = arena.document["input"][0]["action"]
    assert masked_key != EMAIL
    assert [slot["path"] for slot in _kept_request(tmp_path, arena, payload=payload)["slots"]] == [
        f"$.input[0].action.{masked_key}",
        "$.input[1].output",
    ]


@PEOPLE
async def test_worker_record_index_failure_drops_only_the_record(person, tmp_path, monkeypatch, caplog):
    """Recording allocation in Prepared must not turn a send into a failure or change ownership of its real hold."""
    from latet.gateway import mediate as mediate_module

    mediator, vault = _mediator(person)
    recorder, store = _bare_recorder(tmp_path)
    provider = _Provider()
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    forwarder = Forwarder(RouteTable([OPENAI]), Capture(None), client, mediator, recorder)
    releases: list[object] = []
    release = mediator.engine.release

    def released(session: Any, held: object) -> None:
        releases.append(held)
        release(session, held)

    def unavailable(*args: Any) -> Any:
        assert _held(vault) == 1  # fail the new recording allocation after masking acquired its hold
        raise MemoryError(SECRET)

    monkeypatch.setattr(mediator.engine, "release", released)
    caplog.set_level(logging.ERROR, logger="latet")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(mediate_module, "record_positions", unavailable)
            answered = await forwarder.handle(_request(_chat(f"write to {EMAIL}")))
        assert answered.status_code == 200 and len(provider.sent) == 1
        assert _held(vault) == 0 and len(releases) == 1
        assert recorder.drain() and store.list()[0] == []
        assert "could not queue an exchange for the dashboard: MemoryError" in caplog.text
        assert SECRET not in caplog.text
        answered = await forwarder.handle(_request(_chat("again")))
        if isinstance(answered, StreamingResponse):
            await _drained(answered)
        assert answered.status_code == 200 and recorder.drain() and len(store.list()[0]) == 1
    finally:
        await client.aclose()
        recorder.close()
        store.close()


@PEOPLE
async def test_worker_logical_record_failure_keeps_the_original_refusal(person, tmp_path, monkeypatch, caplog):
    """An edited WS refusal needs logical recording JSON; failure to allocate it must lose only that record."""
    from latet.gateway import mediate as mediate_module

    mediator, vault = _mediator(person)
    recorder, store = _bare_recorder(tmp_path)
    frame = {
        "type": "response.create",
        "input": [
            # A call id as the provider issues it: a free one ("c") is refused before anything is masked.
            {"type": "local_shell_call", "call_id": _CALL_ID, "action": {EMAIL: "same"}},
            {"type": "local_shell_call_output", "call_id": _CALL_ID, "output": '{"card":4111111111111111}'},
        ],
    }
    dumps = mediate_module.dumps_compact

    def unavailable(value: Any) -> str:
        if sys._getframe(1).f_code.co_name in {"refresh_recording", "_refresh_recording"}:
            assert _held(vault) == 1
            raise MemoryError(SECRET)
        return dumps(value)

    caplog.set_level(logging.ERROR, logger="latet")
    arena = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(mediate_module, "dumps_compact", unavailable)
            session = await mediator.run(mediator.engine.session, "ws-record-failure")
            payload, arena = await mediator.run_held(mediator.mask_ws_frame, json.dumps(frame), session)
        assert arena is not None and arena.block_reason and arena.lease is not None
        assert _held(vault) == 1  # still owned by the exchange, not released by the recording allocation
        recorder.finish(Outcome({}, "openai", "/v1/responses", "ws", 1.0, arena, payload=payload))
        assert recorder.drain() and store.list()[0] == []
        assert "could not queue an exchange for the dashboard: MemoryError" in caplog.text
        assert SECRET not in caplog.text
    finally:
        if arena is not None and arena.lease is not None:
            arena.lease.release()
        recorder.close()
        store.close()
    assert _held(vault) == 0


def _verify_outcome(text: str) -> Outcome:
    from latet.engine.types import SlotKind
    from latet.gateway.mediate import VERIFY
    from latet.protocols.base import SlotRef

    texts = [text]
    arena = Prepared(
        VERIFY,
        "anthropic",
        json.dumps(texts).encode(),
        None,
        document=texts,
        pending=[(SlotRef(texts, 0, SlotKind.USER_TEXT), [], False)],
    )
    return Outcome({"user-agent": "claude-cli/2"}, "anthropic", "/v1/messages", "http", 1.0, arena)


def test_a_record_that_cannot_be_made_queued_or_stored_leaves_nothing_behind(tmp_path, monkeypatch, caplog):
    """A snapshot that cannot be made (its memory refused) fails where the exchange ends, before anything is queued; a
    queue that is full drops it; a store that fails drops it in the recorder's thread. Each said by its type alone (its
    words could quote the exchange), nothing pending afterwards, nothing told to the dashboard's viewers, nothing of it
    kept; and the recorder goes on recording."""
    recorder, store = _bare_recorder(tmp_path)
    viewer = recorder.broadcaster.subscribe()  # type: ignore[attr-defined]
    ran: list[Any] = []
    recording = recorder._record
    monkeypatch.setattr(recorder, "_record", lambda *args: (ran.append(weakref.ref(args[0])), recording(*args))[1])
    caplog.set_level(logging.INFO, logger="latet")
    try:
        # A snapshot whose memory is refused: made as the exchange ends, so nothing is queued.
        def refused(*args: Any) -> Any:
            raise MemoryError

        with monkeypatch.context() as patch:
            patch.setattr(recorder_module, "_frozen_blocks", refused)  # the snapshot made where it ends
            recorder.finish(_verify_outcome(f"note {SECRET}"))
        assert recorder.drain() and recorder._pending == 0 and ran == []
        assert "could not freeze an exchange: MemoryError" in caplog.text
        # A full queue: the second exchange dropped, the first recorded once the thread is free again.
        assert recorder._executor is not None
        gate = threading.Event()
        recorder._executor.submit(gate.wait, 10)
        with monkeypatch.context() as patch:
            patch.setattr(recorder_module, "_MAX_QUEUED", 1)
            recorder.finish(_verify_outcome("first"))
            recorder.finish(_verify_outcome("second"))
            assert recorder._pending == 1
        gate.set()
        assert recorder.drain() and recorder._pending == 0
        assert "dashboard recording is behind: an exchange was not recorded" in caplog.text
        assert _texts(store, store.list()[0][0]["id"]) == ["first"]
        # A store that fails: said by its type, dropped, and nothing of it kept.
        record = store.record

        def broken(item: Any) -> None:
            raise RuntimeError(f"the store failed with {SECRET}")

        monkeypatch.setattr(store, "record", broken)
        recorder.finish(_verify_outcome(f"key {SECRET}"))
        assert recorder.drain() and recorder._pending == 0
        assert "could not record an exchange: RuntimeError" in caplog.text and SECRET not in caplog.text
        gc.collect()
        assert all(ref() is None for ref in ran)  # nothing of a dropped exchange kept anywhere
        monkeypatch.setattr(store, "record", record)
        recorder.finish(_verify_outcome("again"))
        assert recorder.drain() and len(store.list()[0]) == 2
        told = []
        while not viewer.empty():
            told.append(viewer.get_nowait())
        assert len(told) == 2  # the two recorded, never one that failed
    finally:
        recorder.close()
        store.close()


@PEOPLE
async def test_a_capture_or_recorder_failure_releases_the_hold_exactly_once(person, tmp_path, monkeypatch):
    """P1-4's ownership row carried here: a capture that fails before the send or while the answer streams, a record
    whose snapshot cannot be made, a store that fails: the client is answered as before (or the error goes on), nothing
    is sent twice, and the exchange's hold is released once, over a real ``StoredVault``."""
    mediator, vault = _mediator(person)
    releases: list[object] = []
    release = mediator.engine.release

    def counted(session: Any, held: object) -> None:
        releases.append(held)
        release(session, held)

    monkeypatch.setattr(mediator.engine, "release", counted)
    recorder, store = _bare_recorder(tmp_path)
    provider = _Provider()
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    forwarder = Forwarder(RouteTable([OPENAI]), Capture(tmp_path / "capture"), client, mediator, recorder)
    try:
        # The capture fails before the send: nothing sent, the hold released by ``handle``.
        def failing(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the capture's disk is full")

        with monkeypatch.context() as patch:
            patch.setattr(Exchange, "request", failing)
            with pytest.raises(RuntimeError, match="disk is full"):
                await forwarder.handle(_request(_chat(f"write to {EMAIL}")))
        assert provider.sent == [] and _held(vault) == 0 and len(releases) == 1
        # The capture fails as the answer is written down, past the client's answer: the response's finalizer loses
        # the capture alone, the client gets its answer whole, the relay's ``finally`` releases the hold, the answer
        # closed.
        with monkeypatch.context() as patch:
            patch.setattr(Exchange, "response_chunk", failing)
            patch.setattr(Exchange, "response_text", failing)
            patch.setattr(Exchange, "response_end", failing)  # and again as it ends
            streamed = await forwarder.handle(_request(_streaming(f"write to {EMAIL}")))
            assert isinstance(streamed, StreamingResponse) and _held(vault) == 1
            await _drained(streamed)
        assert _held(vault) == 0 and len(releases) == 2 and provider.answers[-1].closed

        # The record's snapshot cannot be made, then the store fails: the client answered all the same.
        def refused(*args: Any) -> Any:
            raise MemoryError

        with monkeypatch.context() as patch:
            patch.setattr(recorder_module, "_frozen_blocks", refused)  # the snapshot made where it ends
            whole = await forwarder.handle(_request(_chat(f"write to {EMAIL}")))
        assert whole.status_code == 200 and _held(vault) == 0 and len(releases) == 3

        def broken(item: Any) -> None:
            raise RuntimeError("the store failed")

        with monkeypatch.context() as patch:
            patch.setattr(store, "record", broken)
            streamed = await forwarder.handle(_request(_streaming(f"write to {EMAIL}")))
            await _drained(streamed)
            assert recorder.drain() and recorder._pending == 0
        assert _held(vault) == 0 and len(releases) == 4 and len(provider.sent) == 3
        assert len(set(map(id, releases))) == 4  # each its own hold, once
    finally:
        await client.aclose()
        recorder.close()
        store.close()


def _space(tmp_path: Path, identity: str) -> tuple[Runtime, Any]:
    rt = make_runtime(
        Config(identity=IdentityConfig(enabled=False)),
        data=tmp_path / identity,
        config_file=tmp_path / f"{identity}.yaml",
    )
    rt.gateway.on_server = OnServer("https://latet.acme.lan", identity)
    app = create_app(rt.gateway)

    def gated(as_identity: str) -> Any:
        async def scoped(scope: Any, receive: Any, send: Any) -> None:
            await app({**scope, "latet.identity": as_identity}, receive, send)

        return scoped

    return rt, gated


def test_no_identitys_observation_is_visible_to_another(tmp_path):
    """Two identities' spaces, their plugins naming the same Claude Code session: each space's history holds its own
    bridge's observations alone, and a call made in one identity's name to the other's space is refused, recording
    nothing there."""
    spaces = {who: _space(tmp_path, who) for who in ("u_first", "u_second")}
    try:
        for who, (_, gated) in spaces.items():
            with TestClient(gated(who), base_url="https://latet.acme.lan") as client:
                said = client.post(
                    f"{ENGINE}/verify", json={"session": "shared", "slots": [{"text": f"{who} {SECRET}"}]}
                )
                assert said.status_code == 200 and said.json()["blocking"]
        rt, gated = spaces["u_second"]
        with TestClient(
            gated("u_first"), base_url="https://latet.acme.lan"
        ) as client:  # u_first's call, u_second's space
            assert (
                client.post(f"{ENGINE}/verify", json={"session": "shared", "slots": [{"text": SECRET}]}).status_code
                == 401
            )
        for who, (rt, _) in spaces.items():
            assert rt.gateway.recorder is not None and rt.gateway.recorder.drain()
            assert rt.exchanges is not None
            [summary] = rt.exchanges.list()[0]
            assert isinstance(rt.exchanges, ExchangeStore)
            assert _texts(rt.exchanges, summary["id"]) == [f"{who} {SECRET}"]
    finally:
        for rt, _ in spaces.values():
            rt.close()
