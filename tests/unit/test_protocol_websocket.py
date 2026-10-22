"""The shared Responses JSON channel also obeys its scheduling and terminal-state contracts over WebSocket."""

import json
import threading

import pytest
from starlette.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import WSResponseRewriter
from latet.gateway.routes import Upstream
from latet.protocols.rewrite import RestoreContext

from .test_protocol_buffering import ORIGINAL, _delta, _start
from .test_what_leaves import _mediator


class _ArgumentsProvider:
    def __init__(self, finish, whole=False):
        self.finish = finish
        self.whole = whole
        self.release = threading.Event()
        self.server = serve(self.answer, "127.0.0.1", 0, close_timeout=2)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def answer(self, connection):
        try:
            turn = json.loads(connection.recv(timeout=5))
            masked = turn["input"]
            raw = json.dumps({"pad": "x" * (70 * 1024), "owner": masked})
            events = [{"type": "response.created", "response": {"id": "resp-1"}}, _start("responses")]
            events += [_delta("responses", raw[start : start + 1024]) for start in range(0, len(raw), 1024)]
            events += [
                {"type": "response.output_text.delta", "item_id": "text-1", "content_index": 0, "delta": "ready "},
                {"type": "response.output_text.delta", "item_id": "text-1", "content_index": 0, "delta": "waiting "},
            ]
            for event in events:
                connection.send(json.dumps(event))
            if not self.release.wait(5):
                return
            if self.finish:
                # The existing rewriter accepts a completion marker without a repeated whole argument field.
                done = {"type": "response.function_call_arguments.done", "item_id": "call-0", "output_index": 0}
                if self.whole:
                    done["arguments"] = raw
                connection.send(json.dumps(done))
                connection.send(json.dumps({"type": "response.completed", "response": {"status": "completed"}}))
            connection.recv(timeout=5)
        except (ConnectionClosed, TimeoutError):
            pass

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


@pytest.mark.parametrize(
    ("finish", "interrupt"),
    [(False, False), (True, False), (True, True)],
    ids=["disconnect-cleans-state", "tiny-completion-off-loop", "cancel-unfinished-engine-work"],
)
def test_real_websocket_releases_pending_json_or_finalizes_it_on_the_engine(monkeypatch, finish, interrupt):
    provider = _ArgumentsProvider(finish, whole=interrupt)
    mediator = _mediator()
    rewriters = []
    loop_threads = []
    finalized = []
    local = threading.local()
    entered = threading.Event()
    released = threading.Event()
    remembered = []
    worker_errors = []
    working_maps = []
    original_init = WSResponseRewriter.__init__
    original_restored = WSResponseRewriter.restored
    original_frame = WSResponseRewriter.on_frame
    original_json = RestoreContext.json_text
    original_context = mediator.restore_context

    def context(session):
        ctx = original_context(session)
        remember = ctx.remember

        def completed(masked, restored):
            remembered.append((masked, restored))
            remember(masked, restored)

        ctx.remember = completed
        return ctx

    def capture(self, *args):
        original_init(self, *args)
        rewriters.append(self)

    async def on_loop(self, text):
        loop_threads.append(threading.get_ident())
        return await original_restored(self, text)

    def frame(self, text):
        local.frame_bytes = len(text.encode())
        try:
            return original_frame(self, text)
        except BaseException as exc:
            worker_errors.append(type(exc).__name__)
            raise

    def json_text(self, value):
        if len(value) > 64 * 1024:
            finalized.append((threading.get_ident(), local.frame_bytes))
            if interrupt:
                working_maps.append(rewriters[0].stream.channels)
                entered.set()
                assert released.wait(5), "owned WebSocket engine work was not released"
        return original_json(self, value)

    monkeypatch.setattr(WSResponseRewriter, "__init__", capture)
    monkeypatch.setattr(WSResponseRewriter, "restored", on_loop)
    monkeypatch.setattr(WSResponseRewriter, "on_frame", frame)
    monkeypatch.setattr(RestoreContext, "json_text", json_text)
    monkeypatch.setattr(mediator, "restore_context", context)
    app = create_app(
        GatewayConfig(mediator=mediator, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{provider.port}")])
    )
    try:
        with (
            TestClient(app, base_url="http://127.0.0.1:5283") as client,
            client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            socket.send_json({"type": "response.create", "model": "m", "input": ORIGINAL})
            events = []
            for _ in range(100):
                event = socket.receive_json()
                events.append(event)
                if event.get("type") == "response.output_text.delta" and event.get("delta") == "ready ":
                    break
            else:
                pytest.fail("the real gateway did not consume the provider's held argument deltas")
            assert len(rewriters) == 1 and rewriters[0].stream is not None
            assert rewriters[0].stream.channels
            if finish:
                provider.release.set()
                if interrupt:
                    assert entered.wait(5), "the real engine did not start the provider's finalization"
                else:
                    for _ in range(10):
                        event = socket.receive_json()
                        events.append(event)
                        if event.get("type") == "response.completed":
                            break
                    else:
                        pytest.fail("the real gateway did not complete the provider's tool call")
                    arguments = "".join(
                        event.get("delta", "")
                        for event in events
                        if event.get("type") == "response.function_call_arguments.delta"
                    )
                    assert json.loads(arguments) == {"pad": "x" * (70 * 1024), "owner": ORIGINAL}
        stream = rewriters[0].stream
        assert stream is not None and not stream.channels and stream.held is None and stream.budget.held == 0
        if interrupt:
            assert working_maps and working_maps[0], "disconnect mutated a paused worker's owned dictionary"
            assert remembered == []
            released.set()
            mediator.executor.submit(lambda: None).result(timeout=5)
            assert worker_errors == [] and remembered == []
            assert not stream.channels and stream.budget.held == 0
        elif finish:
            tiny = [thread for thread, size in finalized if size < 1024]
            assert tiny and all(thread not in loop_threads for thread in tiny)
    finally:
        released.set()
        mediator.executor.submit(lambda: None).result(timeout=5)
        provider.close()
        mediator.close()
