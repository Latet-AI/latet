"""Nine adopted EP bridges through real server apps/stores; source preparation, UNEXECUTED."""

import asyncio
import json

import pytest
from starlette.responses import StreamingResponse

from latet.engine import wellformed
from latet.protocols.sse import SSEParser
from latet.server.connection import load_connection, load_server_token
from latet.server.tokens import route_key

from ..e2e.fake_upstream import FakeUpstream, _sse
from . import test_e0c_end_to_end as integration

pytestmark = pytest.mark.server
computer = integration.computer
quiet = integration.quiet
cluster = integration.cluster

FIELDS_REFUSED = "Latet reads the content of a model's call, and this request holds "
SAFE_FORMAT = {"type": "json_schema", "schema": {"type": "object", "properties": {"status": {"type": "string"}}}}


class ProtocolUpstream(FakeUpstream):
    """Only the three response forms the adopted integration rows need; normal traffic stays inherited."""

    def __init__(self):
        self.reply = "normal"
        self.original = self.fake = ""
        super().__init__()  # Registers these bound overrides before the provider starts.

    async def chat(self, request):
        if self.reply != "collision":
            return await super().chat(request)
        await self._receive(request)
        return StreamingResponse(self.arguments("chat"), media_type="text/event-stream")

    async def anthropic(self, request):
        if self.reply == "normal":
            return await super().anthropic(request)
        assert self.reply in {"collision", "nonempty", "delta", "second-source"}
        await self._receive(request)
        return StreamingResponse(self.arguments("anthropic"), media_type="text/event-stream")

    def arguments(self, protocol):
        document = {"owners": {self.fake: "owner", self.original: "viewer"}, "value": self.fake}
        if self.reply != "collision":
            document = {"owner": self.fake}
        arguments = json.dumps(document)
        if protocol == "chat":
            call = {"index": 0, "id": "call-0", "type": "function", "function": {"name": "grant", "arguments": ""}}
            yield _sse(None, {"id": "ep-chat", "choices": [{"index": 0, "delta": {"tool_calls": [call]}}]})
            for piece in (arguments[:17], arguments[17:]):
                yield _sse(
                    None,
                    {
                        "id": "ep-chat",
                        "choices": [
                            {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}}
                        ],
                    },
                )
            yield _sse(None, {"id": "ep-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            yield b"data: [DONE]\n\n"
            return
        yield _sse("message_start", {"type": "message_start", "message": {"id": "ep-message", "content": []}})
        initial = document if self.reply in {"nonempty", "second-source"} else {}
        yield _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call-0", "name": "grant", "input": initial},
            },
        )
        pieces = [] if self.reply == "nonempty" else [arguments[:17], arguments[17:]]
        if self.reply == "second-source":
            pieces = ['{"other":1}']
        for piece in pieces:
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": piece},
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        yield _sse("message_stop", {"type": "message_stop"})


def _body(text):
    return {"model": "m", "max_tokens": 10, "stream": True, "messages": [{"role": "user", "content": text}]}


def _headers(protocol, conversation, checked=False):
    if protocol == "chat":
        return {"authorization": "Bearer sk-test", "session-id": conversation}
    if checked:
        return {"user-agent": "claude-cli/2.1.291 (external, cli)", "x-claude-code-session-id": conversation}
    return {"x-api-key": "sk-ant-api-test", "session-id": conversation}


def _post(client, secret, protocol, body, conversation="ep-normal", checked=False):
    endpoint = "chat/completions" if protocol == "chat" else "messages"
    return client.post(
        f"/u/{route_key(secret)}/llm/v1/{endpoint}", json=body, headers=_headers(protocol, conversation, checked)
    )


def _snapshot(provider):
    return tuple(provider.received), json.dumps(provider.requests, sort_keys=True), tuple(provider.ws_received)


def _events(response):
    assert response.status_code == 200
    assert len(response.content) <= 1024 * 1024
    parser = SSEParser(limit=1024 * 1024, events=1000)
    return [doc for event in parser.feed(response.content) if isinstance(doc := event.json(), dict)]


def _text(protocol, events):
    if protocol == "chat":
        return "".join(
            choice.get("delta", {}).get("content") or "" for event in events for choice in event.get("choices", [])
        )
    return "".join(event.get("delta", {}).get("text") or "" for event in events)


def _sources(protocol, events):
    parts, initial = [], []
    for event in events:
        if protocol == "chat":
            for choice in event.get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls", []):
                    parts.append(call.get("function", {}).get("arguments", ""))
        else:
            value = event.get("content_block", {}).get("input")
            if value:
                initial.append(value)
            parts.append(event.get("delta", {}).get("partial_json", ""))
    if "".join(parts):
        parsed = wellformed.read("".join(parts))
        assert not parsed.repeated
        initial.append(parsed.doc)
    return initial


def _runtime(cluster, client, server, secret):
    assert client.portal is not None
    identity = server.identities.by_secret(secret)
    assert identity is not None
    return client.portal.call(server.spaces.runtime, identity)


def _settle(client, runtime):
    async def barrier():
        await asyncio.wait_for(runtime.mediator.run(lambda: None), 5)

    assert client.portal is not None
    client.portal.call(barrier)
    assert runtime.mediator.provenance.pending_batches == runtime.mediator.provenance.held_bytes == 0
    assert runtime.built.engine.conversations.flush(timeout=5)
    assert runtime.recorder is not None and runtime.recorder.drain(timeout=5)


@pytest.fixture
def protocol_cluster(monkeypatch, request):
    # Replace only this test module's provider constructor; registered routes are real subclass endpoints.
    monkeypatch.setattr(integration, "FakeUpstream", ProtocolUpstream)
    found = request.getfixturevalue("cluster")
    added = integration.run_cli("server", "people", "add", "Emily Carter", "--email", "emily.carter@acme.com")
    assert added.exit_code == 0
    lines = added.output.splitlines()
    index = next(i for i, line in enumerate(lines) if line.strip().startswith("latet connect "))
    code = lines[index + 1].strip()
    made = integration.run_cli("server", "keys", "add", "Billing CI")
    assert made.exit_code == 0
    key = next(word for word in made.output.split() if word.startswith("latet_key_"))
    found.route["to"] = found.second
    connected = integration.run_cli("connect", found.url, "--code", "-", "--yes", input=code + "\n")
    assert connected.exit_code == 0 and code not in connected.output
    connection = load_connection()
    assert connection is not None
    token = load_server_token(integration.data_dir(), connection)
    assert token is not None
    found.personal, found.key = token, key
    found.used = {
        token,
        key,
        code,
        found.admin_token,
        found.master_key,
        integration.METRICS_TOKEN,
        integration.REVIEWER_KEY,
        "sk-test",
        "sk-ant-api-test",
        f"/u/{route_key(token)}",
        f"/u/{route_key(key)}",
    }
    try:
        yield found
    finally:
        originals, fakes = set(), set()
        for server, client in ((found.servers[0], found.first), (found.servers[1], found.second)):
            for secret in (token, key):
                runtime = _runtime(found, client, server, secret)
                _settle(client, runtime)
                for entity in runtime.built.vault.entities():
                    originals.add(entity.original)
                    fakes.add(entity.surrogate)
        for text in (
            found.logs.getvalue(),
            integration.dump(found.server.db).decode(errors="replace"),
            integration.valkey_dump(found.server.bus, found.server.bus.prefix).decode(errors="replace"),
        ):
            integration._held_nothing(text, *found.used, *fakes)
        metadata = ("Emily Carter", "emily.carter@acme.com", "Billing CI", "e0c-t16-laptop", "e0c-t16-administrator")
        integration._checked_metadata(found.server.db)
        integration._held_nothing(
            integration._non_metadata_dump(found.server.db).decode(errors="replace"), *metadata, *originals
        )
        for text in (
            found.logs.getvalue(),
            integration.valkey_dump(found.server.bus, found.server.bus.prefix).decode(errors="replace"),
        ):
            integration._held_nothing(text, *metadata, *originals)


def _teach(cluster, secret, protocol, conversation):
    warm = _post(cluster.first, secret, protocol, _body("read " + secret), conversation)
    events = _events(warm)
    assert not any("error" in event for event in events)
    assert secret not in cluster.upstream.all_received()
    runtime = _runtime(cluster, cluster.first, cluster.servers[0], secret)
    _settle(cluster.first, runtime)
    entity = runtime.built.vault.by_original(secret)
    assert entity is not None and entity.surrogate != secret
    assert any(form in cluster.upstream.all_received() for form in integration._forms(entity.surrogate))
    assert secret in _text(protocol, events)
    cluster.upstream.original, cluster.upstream.fake = secret, entity.surrogate
    return runtime, entity.surrogate


@pytest.mark.parametrize("protocol", ["chat", "anthropic"])
def test_real_server_collision_and_replay(protocol_cluster, protocol):
    cluster = protocol_cluster
    secret = cluster.personal if protocol == "chat" else cluster.key
    conversation = "e0c-t16-collision"
    runtime, fake = _teach(cluster, secret, protocol, conversation)
    cluster.upstream.reply = "collision"
    reply = _post(cluster.first, secret, protocol, _body("continue"), conversation)
    events = _events(reply)
    assert not any("error" in event for event in events)
    if protocol == "chat":
        assert any(
            choice.get("finish_reason") == "tool_calls" for event in events for choice in event.get("choices", [])
        )
    else:
        assert any(event.get("type") == "content_block_stop" for event in events)
        assert any(event.get("type") == "message_stop" for event in events)
    [arguments] = _sources(protocol, events)
    assert arguments == {"owners": {fake: "owner", secret: "viewer"}, "value": secret}
    _settle(cluster.first, runtime)  # Actual engine ordering AND Valkey writer completion before cross-instance replay.
    engine = runtime.built.engine
    assert engine.conversations.recall(engine._key("R", secret)) == fake
    other = _runtime(cluster, cluster.second, cluster.servers[1], secret).built.engine
    assert other.conversations.recall(other._key("R", secret)) == fake
    if protocol == "chat":
        history = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-0",
                        "type": "function",
                        "function": {"name": "grant", "arguments": json.dumps(arguments)},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-0", "content": "done"},
        ]
    else:
        history = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-0", "name": "grant", "input": arguments}],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-0", "content": "done"}]},
        ]
    body = _body("continue")
    body["messages"] += history
    before = _snapshot(cluster.upstream)
    refused = _post(cluster.second, secret, protocol, body, conversation)
    assert refused.status_code == 400 and "two keys of one of its JSON objects" in refused.text
    assert _snapshot(cluster.upstream) == before
    integration._held_nothing(refused.text, secret, fake)
    cluster.upstream.reply = "normal"
    clean = _post(cluster.second, secret, protocol, _body("new conversation"), "e0c-t16-new")
    assert "new conversation" in _text(protocol, _events(clean))
    assert len(cluster.upstream.received) == len(before[0]) + 1


@pytest.mark.parametrize("mode", ["rewrite", "checked"])
def test_real_server_fallback_schema(protocol_cluster, mode):
    cluster = protocol_cluster
    checked = mode == "checked"
    body = _body("plain text")
    sensitive = {"type": "json_schema", "schema": {"type": "object", "properties": {cluster.key: {"type": "string"}}}}
    body["fallbacks"] = [
        {"model": "safe"},
        {"model": "fallback-model", "output_config": {"format": sensitive, "effort": "high"}},
    ]
    before = _snapshot(cluster.upstream)
    result = _post(cluster.first, cluster.personal, "anthropic", body, "e0c-t16-fallback", checked)
    if checked:
        assert result.status_code == 400 and "Nothing was sent" in result.text
        assert _snapshot(cluster.upstream) == before
        integration._held_nothing(result.text, cluster.key)
    else:
        assert "plain text" in _text("anthropic", _events(result))
        assert len(cluster.upstream.received) == len(before[0]) + 1
        sent = json.loads(cluster.upstream.received[-1])
        assert cluster.key not in cluster.upstream.received[-1].decode()
        assert sent["fallbacks"][0] == {"model": "safe"}
        assert sent["fallbacks"][1]["output_config"]["effort"] == "high"
        assert sent["fallbacks"][1]["output_config"]["format"] != sensitive
    body["fallbacks"] = [{"model": "safe"}, {"model": "fallback-model", "output_config": {"format": SAFE_FORMAT}}]
    raw = json.dumps(body, indent=2).encode()
    endpoint = f"/u/{route_key(cluster.personal)}/llm/v1/messages"
    count = len(cluster.upstream.received)
    clean = cluster.second.post(
        endpoint,
        content=raw,
        headers={**_headers("anthropic", "e0c-t16-fallback-safe", checked), "content-type": "application/json"},
    )
    assert "plain text" in _text("anthropic", _events(clean))
    assert len(cluster.upstream.received) == count + 1 and cluster.upstream.received[-1] == raw


@pytest.mark.parametrize("kind", ["name", "search_result", "title", "context"])
def test_real_server_forbidden_fields(protocol_cluster, kind):
    cluster = protocol_cluster
    protocol = "chat" if kind == "name" else "anthropic"
    body = _body("done")
    if kind == "name":
        body["messages"] = [{"role": "assistant", "name": cluster.key, "content": "done"}]
    else:
        block = (
            {
                "type": "search_result",
                "source": cluster.key,
                "title": cluster.key,
                "content": [{"type": "text", "text": cluster.key}],
            }
            if kind == "search_result"
            else {
                "type": "document",
                "source": {"type": "url", "url": "https://example.com/report.pdf"},
                kind: cluster.key,
            }
        )
        body["messages"] = [{"role": "user", "content": [block]}]
    before = _snapshot(cluster.upstream)
    refused = _post(cluster.second, cluster.personal, protocol, body)
    assert refused.status_code == 400 and FIELDS_REFUSED in refused.text and "Nothing was sent" in refused.text
    assert _snapshot(cluster.upstream) == before
    integration._held_nothing(refused.text, *cluster.used)
    clean = _post(cluster.first, cluster.personal, protocol, _body("ordinary supported text"))
    assert "ordinary supported text" in _text(protocol, _events(clean))
    assert len(cluster.upstream.received) == len(before[0]) + 1


def test_real_server_anthropic_argument_source_boundary(protocol_cluster, monkeypatch):
    cluster = protocol_cluster
    conversation = "e0c-t16-ep2"
    runtime, fake = _teach(cluster, cluster.personal, "anthropic", conversation)
    published = []
    conversations = runtime.built.engine.conversations
    remember, name = conversations.remember, conversations.name

    def observed_remember(key, masked):
        published.append("remember")
        return remember(key, masked)

    def observed_name(response_id, key):
        published.append("name")
        return name(response_id, key)

    monkeypatch.setattr(conversations, "remember", observed_remember)
    monkeypatch.setattr(conversations, "name", observed_name)
    for mode in ("nonempty", "delta"):
        cluster.upstream.reply = mode
        good = _post(cluster.first, cluster.personal, "anthropic", _body("continue"), conversation)
        events = _events(good)
        assert not any("error" in event for event in events)
        assert _sources("anthropic", events) == [{"owner": cluster.personal}]
        assert any(event.get("type") == "content_block_stop" for event in events)
        _settle(cluster.first, runtime)
        assert "remember" in published  # The delegating store observer is reached by real positive responses.
        published.clear()
    cluster.upstream.reply = "second-source"
    count = len(cluster.upstream.received)
    bad = _post(cluster.first, cluster.personal, "anthropic", _body("continue"), conversation)
    events = _events(bad)
    errors = [event for event in events if event.get("type") == "error" or "error" in event]
    assert len(errors) == 1 and "the provider supplied two sources for one tool's arguments" in json.dumps(errors)
    integration._held_nothing(json.dumps(errors), cluster.personal, fake)
    assert not any(
        event.get("delta", {}).get("partial_json") or event.get("content_block", {}).get("input") for event in events
    )
    assert _sources("anthropic", events) == []
    assert not any(event.get("type") in {"content_block_stop", "message_stop"} for event in events)
    assert not any(event.get("delta", {}).get("stop_reason") for event in events)
    _settle(cluster.first, runtime)
    assert published == []
    assert len(cluster.upstream.received) == count + 1  # Response refusal, never misreported as request no-forward.
