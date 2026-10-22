"""A stream its provider sends with no Content-Type is read as the stream its request asked for. ChatGPT's Codex backend
answers so a request that streams and accepts JSON (Hermes Agent's, signed in with ChatGPT): its events are restored like
any other stream's, and recorded. An answer with no Content-Type to a request that does not stream goes on as it came."""

import json
from collections.abc import Callable

import httpx
from starlette.testclient import TestClient

from latet.config import Config
from latet.engine.build import build
from latet.engine.pipeline import Engine
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import Mediator
from latet.gateway.recorder import Recorder
from latet.gateway.routes import Upstream
from latet.storage.exchanges import ExchangeStore

from ..conftest import ROOT_KEY

EMAIL = "emily.carter@acme.com"
HEADERS = {"host": "127.0.0.1:5283", "accept": "application/json", "session_id": "s-hermes"}


def _request(stream: bool) -> dict:
    text = {"type": "input_text", "text": f"write to {EMAIL}"}
    return {"model": "m", "stream": stream, "input": [{"type": "message", "role": "user", "content": [text]}]}


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class _Codex:
    """ChatGPT's Codex backend as Hermes reads it: a turn's events, or its whole answer to a request that does not
    stream, with no Content-Type. ``sent``: what the e-mail was sent as; ``body``: the whole answer it gave."""

    def __init__(self) -> None:
        self.sent = ""
        self.body = b""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)
        self.sent = asked["input"][0]["content"][0]["text"].removeprefix("write to ")
        if not asked["stream"]:
            text = {"type": "output_text", "text": f"to {self.sent}"}
            self.body = json.dumps({"id": "resp_1", "output": [{"type": "message", "content": [text]}]}).encode()
            return httpx.Response(200, stream=httpx.ByteStream(self.body))
        where = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_text.delta", **where, "delta": f"to {self.sent}"},
            {"type": "response.output_text.done", **where, "text": f"to {self.sent}"},
            {"type": "response.completed", "response": {"id": "resp_1"}},
        ]
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(_sse(e) for e in events)))


def _gateway(codex: _Codex, recorder: Callable[[Engine], Recorder] | None = None) -> TestClient:
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("chatgpt", "/backend-api/codex", "http://127.0.0.1:1", keep_prefix=True)],
        mediator=Mediator(built.engine, built.verifier),
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(codex)),
        recorder=recorder(built.engine) if recorder is not None else None,
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1:5283")


def test_a_stream_with_no_content_type_is_restored():
    codex = _Codex()
    with _gateway(codex) as client:
        answered = client.post("/backend-api/codex/responses", json=_request(stream=True), headers=HEADERS)
    assert answered.status_code == 200
    assert codex.sent and codex.sent != EMAIL
    events = [json.loads(line[6:]) for line in answered.text.splitlines() if line.startswith("data: ")]
    assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == f"to {EMAIL}"
    assert [e["text"] for e in events if e["type"] == "response.output_text.done"] == [f"to {EMAIL}"]
    assert codex.sent not in answered.text


def test_a_stream_with_no_content_type_is_recorded(tmp_path):
    codex = _Codex()
    store = ExchangeStore(tmp_path / "exchanges.db", ROOT_KEY)
    recorders: list[Recorder] = []

    def recording(engine: Engine) -> Recorder:
        recorders.append(Recorder(store, engine))
        return recorders[-1]

    try:
        with _gateway(codex, recording) as client:
            client.post("/backend-api/codex/responses", json=_request(stream=True), headers=HEADERS)
        assert recorders[0].drain()
        [summary] = store.list()[0]
        detail = store.detail(summary["id"])
    finally:
        for recorder in recorders:
            recorder.close()
        store.close()
    assert detail is not None
    response = detail["response"]
    assert response["text"] == f"to {codex.sent}"  # as the model wrote it, shown in the dashboard
    assert len(response["text_spans"]) == 1


def test_an_answer_with_no_content_type_to_a_request_that_does_not_stream_goes_on_as_it_came():
    codex = _Codex()
    with _gateway(codex) as client:
        answered = client.post("/backend-api/codex/responses", json=_request(stream=False), headers=HEADERS)
    assert answered.status_code == 200
    assert codex.body and answered.content == codex.body
