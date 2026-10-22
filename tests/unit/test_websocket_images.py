"""An image withheld or covered over a WebSocket leaves as it was decided, as over HTTP.

The relay wrote a turn anew only when the reviewers masked more text in it: a turn whose image you withheld or covered
in the Live Reviewer, or the local reviewer withheld, with no text masked in the same step, left as it was masked before
the decision, the image as it came (the re-review's probe). On a computer and on a server alike."""

import asyncio
import base64
import json
import threading

import httpx
import pytest
from starlette.websockets import WebSocket
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.config import Config, IdentityConfig, LiveReview, LlmReview
from latet.engine.build import build
from latet.engine.reviewer import ReviewClient, Reviewer
from latet.engine.vault import Vault
from latet.gateway import ws
from latet.gateway.capture import Capture
from latet.gateway.mediate import Mediator
from latet.gateway.routes import RouteTable, Upstream
from latet.vision.masking import ImageMasking

from ..conftest import ROOT_KEY
from ..fake_ocr import FakeOcr

WITHHELD = "[image withheld by Latet: "


class _Answering:
    """A WebSocket upstream on 127.0.0.1 that keeps every frame it is sent and answers each with a turn's end."""

    def __init__(self) -> None:
        self.got: list[str | bytes] = []
        self.server = serve(self._answer, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _answer(self, conn) -> None:
        done = json.dumps({"type": "response.completed", "response": {"status": "completed"}})
        try:
            for message in conn:
                self.got.append(message)
                conn.send(done)
        except ConnectionClosed:
            pass

    def close(self) -> None:
        self.server.shutdown()


class _Client:
    """The client's side of a WebSocket the relay serves: what it sends, queued; what it is sent, kept."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})
        self.sent: list[dict] = []
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1",
            "server": ("127.0.0.1", 5283), "client": ("127.0.0.1", 50000), "root_path": "",
            "path": "/relay/v1/responses", "raw_path": b"/relay/v1/responses", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:5283"), (b"session-id", b"s-1")], "subprotocols": [],
        }  # fmt: skip
        self.socket = WebSocket(scope, self._receive, self._send)

    async def _receive(self) -> dict:
        return await self.incoming.get()

    async def _send(self, message: dict) -> None:
        self.sent.append(message)

    def texts(self) -> list[str]:
        return [message["text"] for message in self.sent if message["type"] == "websocket.send" and "text" in message]


class _Vision:
    """A local reviewer's server whose model, shown an image, sees a name nowhere in the text read in it."""

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 1, "modalities": {"vision": True}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        content = json.loads(request.content)["messages"][1]["content"]
        found = [{"text": "Paolo Neri", "category": "person"}] if isinstance(content, list) else []
        chunk = {"choices": [{"delta": {"content": json.dumps({"findings": found, "block": False})}}]}
        stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode())


def _mediator(way: str, on_server: bool, reader: FakeOcr) -> Mediator:
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.vision = ImageMasking(reader)
    mediator.on_server = on_server
    if way == "the local reviewer":
        review = LlmReview(enabled=True, model="fake-model", images=True)
        client = ReviewClient(review.url, "fake-model", transport=httpx.MockTransport(_Vision()))
        mediator.llm_review, mediator.reviewer = review, Reviewer(review, b"r" * 32, client=client)
    else:
        mediator.live_review = LiveReview(enabled=True)
    return mediator


async def _until(predicate, seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not in time")


@pytest.mark.parametrize("on_server", [False, True], ids=["a computer", "a server"])
@pytest.mark.parametrize("way", ["withheld by you", "covered by you", "the local reviewer"])
async def test_an_image_withheld_or_covered_over_a_websocket_leaves_as_it_was_decided(way, on_server):
    reader = FakeOcr()
    png = reader.screenshot(["Customer since 2019, VIP"])  # nothing in it any detector masks
    part = {"type": "input_image", "image_url": f"data:image/png;base64,{png}"}
    frame = {"type": "response.create", "model": "m", "input": [{"role": "user", "content": [part]}]}
    upstream, client, mediator = _Answering(), _Client(), _mediator(way, on_server, reader)
    feed = await mediator.approvals.watch()  # as a dashboard watches the queue
    routes = RouteTable([Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}")])
    try:
        relaying = asyncio.create_task(ws.relay(client.socket, routes, Capture(None), mediator))
        client.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})
        if way != "the local reviewer":
            while (told := await asyncio.wait_for(feed.get(), 10))[0] != "pending":
                pass
            decided = (
                {"item": 0, "withhold": True} if way == "withheld by you" else {"item": 0, "rects": [[0, 0, 12, 12]]}
            )
            assert await mediator.approvals.decide(told[1]["id"], True, images=[decided])
        await _until(lambda: client.texts())
        client.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(relaying, 10)
    finally:
        upstream.close()
    assert [json.loads(text)["type"] for text in client.texts()] == ["response.completed"]
    [sent] = upstream.got
    assert isinstance(sent, str) and png not in sent  # the image as it came never left
    [left] = json.loads(sent)["input"][0]["content"]
    if way == "covered by you":
        kind, data = left["image_url"].split(",", 1)
        assert kind == "data:image/png;base64" and data != png and base64.b64decode(data)  # painted with your bar
    else:
        assert left["type"] == "input_text" and left["text"].startswith(WITHHELD)
