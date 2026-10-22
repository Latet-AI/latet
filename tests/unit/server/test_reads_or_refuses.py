"""A Latet server sends what it read, or refuses it: "the server masks; it never trusts a computer".

What a computer lets through as it came (Latet does not rewrite Claude Code's requests, nor read a WebSocket frame or a
body it does not know), a server refuses with an error that says why: an image whose text it could not read on a
request it only checks, a WebSocket frame it does not read, a body it does not read. A computer keeps its behaviour."""

import base64
import io
import json
import logging
import threading
from dataclasses import replace

import httpx
import pytest
from onnxruntime.capi.onnxruntime_pybind11_state import Fail
from PIL import Image
from starlette.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.bridge.api import Bridge, _answered
from latet.config import Config, Destination
from latet.engine.build import build
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, OnServer, create_app
from latet.gateway.mediate import REWRITE, VERIFY, Mediator, _definitions
from latet.gateway.routes import Upstream
from latet.protocols import openai_chat, openai_responses
from latet.protocols.base import ImageRef
from latet.runtime import make_runtime
from latet.state import local_backends
from latet.vision.layout import OcrResult
from latet.vision.masking import WITHHELD, ImageMasking
from latet.vision.ocr import (
    OcrBusy,
    OcrError,
    OcrTooLong,
    OcrTooManyTiles,
    OcrTooWide,
    OnnxOcr,
)
from latet.vision.policy import image_policy
from latet.vision.pool import (
    OcrPool,
    SpaceReads,
)

from ...conftest import ROOT_KEY
from ...fake_ocr import ModelSession, blank_image, lines_map, stand_in_ocr
from .conftest import EMILY, SHARED, URL, MemorySettings, as_identity

HOST = {"host": "latet.acme.lan"}  # a WebSocket's handshake through the test client names the server's host so

ANTHROPIC = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
CLAUDE_CODE = {
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11",
}


class _Unread:
    """A text recognition whose every read fails with ``error``: another identity's images held the one a server's
    spaces share (``OcrBusy``), the read ran past its budget, the image was too long or a line too wide."""

    ready = True
    error = None

    def __init__(self, error: Exception) -> None:
        self._error = error

    def start_loading(self) -> None:
        pass

    def wait_ready(self, timeout: float = 0) -> bool:
        return True

    def read(self, image):
        raise self._error


def _png(width: int = 400, height: int = 200) -> str:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def _screenshot(png: str) -> bytes:
    """A Claude Code request carrying a screenshot: checked, never rewritten."""
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}
    content = [{"type": "text", "text": "what is on screen?"}, image]
    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": content}]}
    return json.dumps(body).encode()


def _mediator(vision: ImageMasking | None, on_server: bool) -> Mediator:
    built = build(Config(), Vault(ROOT_KEY))  # images: allow, as by default
    mediator = Mediator(built.engine, built.verifier)
    mediator.vision = vision
    mediator.on_server = on_server
    return mediator


# --- an image a server could not read, on a request it only checks ----------------------------------------------------

UNREAD = [
    OcrBusy("the text recognition was busy with other images"),
    OcrTooLong("reading the image took longer than a read may take"),
    OcrTooManyTiles("the image would be read in 17 tiles, more than 16"),
    OcrTooWide("a line of text was too long for its height to be read"),
    OcrError("the text recognition failed"),
]


@pytest.mark.parametrize("error", UNREAD, ids=lambda error: type(error).__name__)
def test_an_image_a_server_could_not_read_stops_a_request_it_only_checks(error):
    """review 6's probe: a Claude Code request carrying a screenshot whose read finds the shared text recognition busy
    left as it came, unread, under the default `images: allow`, where a request the server rewrites has it withheld."""
    prepared = _mediator(ImageMasking(_Unread(error)), on_server=True).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(_png())
    )
    assert prepared.mode == VERIFY and "no image it could not read" in (prepared.block_reason or "")


def test_an_image_past_a_servers_pixels_stops_a_request_it_only_checks():
    reads = ImageMasking(_Unread(OcrBusy("never asked")), max_pixels=100)
    prepared = _mediator(reads, on_server=True).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(_png())
    )
    assert "megapixels" in (prepared.block_reason or "")


def test_an_image_a_server_cannot_read_at_all_stops_a_request_it_only_checks():
    failed = _Unread(OcrBusy("never asked"))
    failed.error = RuntimeError("the models could not load")  # type: ignore[assignment]
    prepared = _mediator(ImageMasking(failed), on_server=True).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(_png())
    )
    assert "no image it could not read" in (prepared.block_reason or "")


def test_a_computer_sends_an_image_it_could_not_read_flagged_as_before():
    prepared = _mediator(ImageMasking(_Unread(OcrBusy("busy"))), on_server=False).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(_png())
    )
    assert prepared.block_reason is None and prepared.uninspected == ["image"]


def test_a_server_that_reads_no_image_leaves_it_to_the_images_setting():
    """With no text recognition (`ocr: off`, or its models not installed) a server reads no image: `images` says
    whether one leaves, as on a computer."""
    prepared = _mediator(None, on_server=True).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(_png())
    )
    assert prepared.block_reason is None and prepared.uninspected == ["image"]


# --- a server whose text recognition is not available, on every path (T22.1) --------------------------------------------

NOT_AVAILABLE = "the text recognition is not available"
CHATS = Upstream("openai", "/openai", "http://127.0.0.1:1")
NOT_CLAUDE_CODE = {"content-type": "application/json", "x-api-key": "sk-ant-test", "user-agent": "python-httpx/0.28"}


def _failed() -> _Unread:
    """A text recognition whose load failed: its spaces' ``ImageMasking.available`` is False."""
    failed = _Unread(OcrBusy("never asked"))
    failed.error = RuntimeError("the models could not load")  # type: ignore[assignment]
    return failed


def _chat_with(png: str) -> bytes:
    """An OpenAI chat request carrying a screenshot: rewritten."""
    image = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}
    content = [{"type": "text", "text": "what is on screen?"}, image]
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": content}]}).encode()


@pytest.mark.parametrize(
    ("upstream", "path", "headers", "body"),
    [
        (CHATS, "/openai/v1/chat/completions", {"authorization": "Bearer sk-test"}, _chat_with),
        (ANTHROPIC, "/anthropic/v1/messages", NOT_CLAUDE_CODE, _screenshot),  # Anthropic's, not from Claude Code
    ],
    ids=["chat", "anthropic"],
)
def test_a_request_a_server_rewrites_leaves_without_the_images_it_cannot_read_at_all(upstream, path, headers, body):
    """The pre-read's probe A: with the text recognition not available (its load failed), an image of a request the
    server rewrites left as it came, unread, flagged on the dashboard alone. It is withheld; a computer sends it as it
    did."""
    png = _png()
    prepared = _mediator(ImageMasking(_failed()), on_server=True).prepare(upstream, "POST", path, headers, body(png))
    assert prepared.mode == REWRITE and prepared.block_reason is None
    assert png not in prepared.body.decode() and NOT_AVAILABLE in prepared.body.decode()
    assert [work.state for work in prepared.images] == [WITHHELD]
    computer = _mediator(ImageMasking(_failed()), on_server=False).prepare(upstream, "POST", path, headers, body(png))
    assert computer.uninspected == ["image"] and png in computer.body.decode()


def test_a_websocket_turn_on_a_server_leaves_without_the_images_it_cannot_read_at_all():
    png = _png()
    image = {"type": "input_image", "image_url": f"data:image/png;base64,{png}"}
    content = [{"type": "input_text", "text": "what is on screen?"}, image]
    turn = json.dumps({"type": "response.create", "model": "m", "input": [{"role": "user", "content": content}]})
    server = _mediator(ImageMasking(_failed()), on_server=True)
    sent, prepared = server.mask_ws_frame(turn, server.engine.session("session-id:s-1"))
    assert prepared is not None and prepared.block_reason is None and png not in sent and NOT_AVAILABLE in sent
    computer = _mediator(ImageMasking(_failed()), on_server=False)
    sent, prepared = computer.mask_ws_frame(turn, computer.engine.session("session-id:s-1"))
    assert sent == turn and prepared is not None and prepared.uninspected == ["image"]


def test_the_bridge_on_a_server_answers_an_image_it_cannot_read_at_all_withheld():
    """The plugin's images: withheld, as the bridge answers an image it withholds; a computer's bridge gives it back
    unread, as before."""
    png = _png()
    asked = {"session": "s-1", "items": [{"image": {"data": png, "media_type": "image/png"}}]}
    server = _mediator(ImageMasking(_failed()), on_server=True)
    _, out, *_ = Bridge(server, "token", on_server=OnServer(URL, EMILY.id))._mask(asked)
    assert _answered(out) == [{"count": 0, "image": None, "withheld": NOT_AVAILABLE}]
    _, out, *_ = Bridge(_mediator(ImageMasking(_failed()), on_server=False), "token")._mask(asked)
    assert _answered(out) == [{"image": {"data": png, "media_type": "image/png"}, "count": 0, "unread": True}]


def test_a_pool_whose_reader_fails_to_load_reads_with_the_others_and_makes_it_again_a_minute_later(tmp_path):
    now = [0.0]
    go = threading.Event()
    go.set()
    failing = OnnxOcr(tmp_path / "det", tmp_path / "rec")  # its model folders hold nothing: its load fails
    made: list[OnnxOcr] = []

    def make() -> OnnxOcr:
        made.append(reader := stand_in_ocr(det=ModelSession(lines_map, go=go)))
        return reader

    pool = OcrPool([stand_in_ocr(det=ModelSession(lines_map, go=go)), failing], make=make, clock=lambda: now[0])
    assert not failing.wait_ready(5) and failing.error is not None
    assert pool.ready and pool.error is None and pool.read(blank_image(), wait=1, budget=5).text == ""
    now[0] = 59.0
    assert pool.read(blank_image(), wait=1, budget=5).text == "" and pool.readers[1] is failing and made == []
    now[0] = 60.0
    assert pool.read(blank_image(), wait=1, budget=5).text == "" and pool.readers[1] is made[0]
    go.clear()  # both read at once now
    reads = [threading.Thread(target=pool.read, args=(blank_image(),), kwargs={"wait": 5}) for _ in range(2)]
    for read in reads:
        read.start()
    try:
        assert all(reader._det.started.wait(5) for reader in pool.readers)
    finally:
        go.set()
        for read in reads:
            read.join(10)


def test_a_pool_whose_readers_all_fail_is_unavailable_and_says_so_once_a_minute(tmp_path, caplog):
    """Every reader failed: images are then ones the server could not read (T22.1); a read a minute later makes them
    again, and each failure is said once a minute, by its type."""
    now = [0.0]

    def make() -> OnnxOcr:
        return OnnxOcr(tmp_path / "det", tmp_path / "rec")

    caplog.set_level(logging.ERROR, logger="latet.vision.ocr")
    pool = OcrPool([make()], make=make, clock=lambda: now[0])
    assert not pool.wait_ready(5) and pool.error is not None and not ImageMasking(pool).available
    now[0] = 30.0
    pool.start_loading()
    now[0] = 61.0
    assert pool.error is None  # due to be made again: images are read, and the read makes it
    assert not pool.wait_ready(5) and pool.error is not None
    failures = [r for r in caplog.records if r.getMessage().startswith("text recognition could not load")]
    assert [r.getMessage() for r in failures] == ["text recognition could not load: FileNotFoundError "
                                                  "(No such file or directory)"] * 2  # fmt: skip


def test_a_servers_failed_text_recognition_is_said_by_its_type_never_by_the_folder_it_names(tmp_path):
    """T22.15: the error of a load that failed names the model folder it could not read. On a server, the refusal the
    agent sees and the notice sent in the image's place say the error's type alone; a computer's words are as before."""
    folder = tmp_path / "srv-latet-models"
    now = [0.0]

    def make() -> OnnxOcr:
        return OnnxOcr(folder / "det", folder / "rec")

    pool = OcrPool([make()], make=make, clock=lambda: now[0])
    assert not pool.wait_ready(5) and folder.name in str(pool.readers[0].error)
    said = "the text recognition failed (FileNotFoundError (No such file or directory))"
    png = _png()

    def prepared(at: float, upstream: Upstream, path: str, headers: dict, body: bytes):
        now[0] = at  # due to be made again: the read makes the reader again, and its load fails again
        space = ImageMasking(SpaceReads(pool, wait=1, budget=5), space=True, clock=lambda: now[0])
        return _mediator(space, on_server=True).prepare(upstream, "POST", path, headers, body)

    checked = prepared(61.0, ANTHROPIC, "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(png))
    assert said in (checked.block_reason or "") and folder.name not in checked.block_reason
    headers = {"authorization": "Bearer sk-test"}
    rewritten = prepared(122.0, CHATS, "/openai/v1/chat/completions", headers, _chat_with(png)).body.decode()
    assert said in rewritten and folder.name not in rewritten and png not in rewritten
    computer = ImageMasking(make()).work(
        ImageRef({"data": png}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)
    )
    assert folder.name in (computer.reason or "")  # a computer's reader, on its own machine: its words, as before


# --- what depends on the moment is kept a short while at most (T22.2) ----------------------------------------------------


class _LateOnce(_Unread):
    """A text recognition whose first read runs past its budget, and every later one reads at once."""

    def __init__(self) -> None:
        super().__init__(OcrTooLong("reading the image took longer than a read may take"))
        self.reads = 0

    def read(self, image):
        self.reads += 1
        if self.reads == 1:
            raise self._error
        return OcrResult(image.width, image.height, (), "", ())


def test_an_image_read_past_its_budget_is_read_again_two_minutes_later_and_its_request_sent():
    """The pre-read's probe B: an image whose read ran past its budget once stayed unread for the space's life, and
    Claude Code, which sends the whole conversation every turn, was refused at every turn after."""
    now = [0.0]
    late = _LateOnce()
    mediator = _mediator(ImageMasking(late, space=True, clock=lambda: now[0]), on_server=True)
    body = _screenshot(_png())
    for at in (0.0, 119.0):
        now[0] = at
        stopped = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, body)
        assert "no image it could not read" in (stopped.block_reason or "") and "OcrTooLong" in stopped.block_reason
    assert late.reads == 1  # sent again within two minutes: not read again
    now[0] = 121.0
    sent = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, body)
    assert sent.block_reason is None and late.reads == 2


# --- ONNX Runtime's own errors, and the lines for images not read (T22.11, T22.10) -------------------------------------


def _ort_failing() -> ImageMasking:
    """A space's images read by a reader whose recognizer's run fails with ONNX Runtime's own error."""

    def failed(inputs):
        raise Fail("a run of the recognizer failed")

    return ImageMasking(SpaceReads(stand_in_ocr(rec=ModelSession(failed)), wait=1, budget=5), space=True)


def test_onnx_runtimes_own_error_withholds_the_image_or_stops_the_request_never_an_error_of_the_request():
    png = _png()
    checked = _mediator(_ort_failing(), on_server=True).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _screenshot(png)
    )
    assert "no image it could not read" in (checked.block_reason or "") and "(Fail)" in checked.block_reason
    rewritten = _mediator(_ort_failing(), on_server=True).prepare(
        CHATS, "POST", "/openai/v1/chat/completions", {"authorization": "Bearer sk-test"}, _chat_with(png)
    )
    assert rewritten.block_reason is None and png not in rewritten.body.decode()
    assert "its text could not be read (Fail)" in rewritten.body.decode()


def _image_ref(width: int) -> ImageRef:
    return ImageRef({"data": _png(width, 20)}, "data", "base64", "image/png", SlotKind.TOOL_RESULT, 0)


@pytest.mark.parametrize("error", [OcrTooWide("a line too wide"), OcrBusy("busy")], ids=["too-wide", "busy"])
def test_a_hundred_images_a_server_could_not_read_write_one_line(caplog, error):
    caplog.set_level(logging.WARNING, logger="latet.vision.masking")
    space = ImageMasking(_Unread(error), space=True)
    for n in range(100):
        assert space.work(_image_ref(20 + n)).state == WITHHELD
    assert len([r for r in caplog.records if r.name == "latet.vision.masking"]) == 1
    caplog.clear()
    computer = ImageMasking(_Unread(error))
    for n in range(100):
        computer.work(_image_ref(20 + n))
    assert len([r for r in caplog.records if r.name == "latet.vision.masking"]) == 100  # as before


# --- through a space's app ----------------------------------------------------------------------------------------------


def _client(rt) -> TestClient:
    return TestClient(as_identity(create_app(rt.gateway), EMILY.id), base_url=URL)


def _upstream(rt) -> list[httpx.Request]:
    """The providers the space reaches, a stand-in that records every request it gets: never the network."""
    seen: list[httpx.Request] = []

    def model(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(b"{}"))

    rt.gateway.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(model))
    return seen


def test_a_spaces_app_stops_a_screenshot_it_could_not_read_and_sends_nothing(space):
    rt, _ = space
    rt.mediator.vision = ImageMasking(_Unread(OcrBusy("the text recognition was busy with other images")))
    seen = _upstream(rt)
    with _client(rt) as c:
        stopped = c.post("/llm/v1/messages", content=_screenshot(_png()), headers=CLAUDE_CODE)
    assert stopped.status_code == 400 and "no image it could not read" in stopped.text
    assert "busy with other images" in stopped.text and "Nothing was sent" in stopped.text and seen == []


# --- a WebSocket frame a server does not read ---------------------------------------------------------------------------

CARD = "my card is 4111 1111 1111 1111, mail emily.carter@acme.com"
# review 6's probe: a frame of another type than a turn, holding a card number, which Latet does not read
ITEM = json.dumps(
    {
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": CARD}]},
    }
)
TURN = json.dumps({"type": "response.create", "model": "m", "input": "the status page is up"})
CANCEL = json.dumps({"type": "response.cancel"})


def _session_mediator(on_server: bool) -> tuple[Mediator, object]:
    mediator = _mediator(None, on_server)
    return mediator, mediator.engine.session("session-id:s-1")


@pytest.mark.parametrize(
    ("frame", "what"),
    [
        (f"not JSON: {CARD}", "a text frame that is not JSON"),
        (ITEM, "a frame of another type"),
        (json.dumps([CARD]), "a frame of another type"),
        (json.dumps({"type": "response.cancel", "input": CARD}), "a frame of another type"),  # more than a cancel holds
        # a cancel whose id is given twice, the copy Latet would not read holding the card
        (
            '{"type": "response.cancel", "response_id": ' + json.dumps(CARD) + ', "response_id": "resp_1"}',
            "a frame of another type",
        ),
    ],
)
def test_a_frame_a_server_does_not_read_is_refused(frame, what):
    mediator, session = _session_mediator(on_server=True)
    _, refused = mediator.mask_ws_frame(frame, session)
    assert refused is not None and what in (refused.block_reason or "") and CARD not in (refused.block_reason or "")
    computer, session = _session_mediator(on_server=False)
    assert computer.mask_ws_frame(frame, session) == (frame, None)  # a computer sends it as it came, as it always did


def test_a_binary_frame_is_refused_on_a_server_alone():
    mediator, session = _session_mediator(on_server=True)
    refused = mediator.unread_frame("a binary frame", session)
    assert refused is not None and "a binary frame" in (refused.block_reason or "")
    computer, session = _session_mediator(on_server=False)
    assert computer.unread_frame("a binary frame", session) is None


@pytest.mark.parametrize(
    "frame", [CANCEL, json.dumps({"type": "response.cancel", "response_id": "resp_1", "event_id": "event_1"})]
)
def test_a_cancel_holds_nothing_to_read_and_passes(frame):
    mediator, session = _session_mediator(on_server=True)
    assert mediator.mask_ws_frame(frame, session) == (frame, None)


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


def _send_each(socket) -> list[dict]:
    """A binary frame, a text frame that is not JSON, a frame of another type, a cancel and a turn, each sent and its
    answer read: an error frame for one refused, the upstream's end of a turn for one sent."""
    answers = []
    socket.send_bytes(CARD.encode())
    answers.append(json.loads(socket.receive_text()))
    for frame in (f"not JSON: {CARD}", ITEM, CANCEL, TURN):
        socket.send_text(frame)
        answers.append(json.loads(socket.receive_text()))
    return answers


def test_a_spaces_websocket_refuses_each_frame_it_does_not_read_and_sends_it_nowhere(tmp_path):
    upstream = _Answering()
    relay = Destination(name="relay", url=f"http://127.0.0.1:{upstream.port}")  # the administrator's, to its upstream
    settings = MemorySettings(SHARED.model_copy(update={"upstreams": [relay]}))
    backends = replace(local_backends(tmp_path / "data", None, ROOT_KEY), settings=settings)
    rt = make_runtime(None, data=tmp_path / "models", root_key=ROOT_KEY, backends=backends, identity=EMILY)
    rt.gateway.on_server = OnServer(URL, EMILY.id)
    try:
        with _client(rt) as c, c.websocket_connect("/relay/v1/responses", headers=HOST) as socket:
            answers = _send_each(socket)
    finally:
        rt.close()
        upstream.close()
    refused, sent = answers[:3], answers[3:]
    assert [a["error"]["type"] for a in refused] == ["latet_blocked"] * 3
    assert "binary frame" in refused[0]["error"]["message"] and "not JSON" in refused[1]["error"]["message"]
    assert "another type" in refused[2]["error"]["message"] and not any("4111" in json.dumps(a) for a in answers)
    assert [a["type"] for a in sent] == ["response.completed"] * 2
    assert upstream.got == [CANCEL, TURN]  # the cancel and the turn alone reached the upstream


def test_a_computers_websocket_sends_what_it_does_not_read_as_it_came():
    upstream = _Answering()
    built = build(Config(), Vault(ROOT_KEY))
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}")],
        mediator=Mediator(built.engine, built.verifier),
    )
    try:
        with (
            TestClient(create_app(config), base_url="http://127.0.0.1:5283") as c,
            c.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"}) as socket,
        ):
            answers = _send_each(socket)
    finally:
        upstream.close()
    assert [a["type"] for a in answers] == ["response.completed"] * 5
    assert upstream.got == [CARD.encode(), f"not JSON: {CARD}", ITEM, CANCEL, TURN]


# --- a body a server does not read --------------------------------------------------------------------------------------

OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")
BEARER = {"authorization": "Bearer sk-test", "content-type": "application/json"}
UPLOAD = json.dumps({"purpose": "batch", "note": CARD}).encode()  # what Latet would never read before it left


@pytest.mark.parametrize(
    ("method", "path", "body", "why"),
    [
        ("POST", "/openai/v1/files", UPLOAD, "another endpoint"),  # review 6's probe: an endpoint Latet does not know
        ("POST", "/openai/v1/batches", UPLOAD, "another endpoint"),
        ("PUT", "/openai/v1/chat/completions", UPLOAD, "a PUT request"),
        ("GET", "/openai/v1/models", UPLOAD, "a GET request"),  # a body where none is read
        ("POST", "/openai/v1/chat/completions", json.dumps([CARD]).encode(), "body is not one"),  # not a JSON object
    ],
)
def test_a_body_a_server_does_not_read_is_refused(method, path, body, why):
    prepared = _mediator(None, on_server=True).prepare(OPENAI, method, path, BEARER, body)
    assert why in (prepared.block_reason or "") and CARD not in (prepared.block_reason or "")
    computer = _mediator(None, on_server=False).prepare(OPENAI, method, path, BEARER, body)
    assert computer.block_reason is None and computer.body == body  # a computer sends it as it came, as it did


@pytest.mark.parametrize("method", ["GET", "HEAD", "DELETE"])
def test_a_request_with_no_body_passes_on_a_server_as_on_a_computer(method):
    prepared = _mediator(None, on_server=True).prepare(OPENAI, method, "/openai/v1/models", BEARER, b"")
    assert prepared.block_reason is None and prepared.body == b""


def test_a_spaces_app_refuses_a_body_it_does_not_read_and_passes_a_request_with_none(space):
    rt, _ = space
    seen = _upstream(rt)
    with _client(rt) as c:
        files = c.post("/llm/v1/files", content=UPLOAD, headers=BEARER)
        put = c.put("/llm/v1/chat/completions", content=UPLOAD, headers=BEARER)
        models = c.get("/llm/v1/models", headers=BEARER)
    for refused in (files, put):
        assert refused.status_code == 400 and "a Latet server does not send it" in refused.text
        assert "Nothing was sent" in refused.text and "4111" not in refused.text
    assert models.status_code == 200 and [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models")]


# --- a field of a body or a turn Latet reads ----------------------------------------------------------------------------

EMAIL, PHONE = "emily.carter@acme.com", "+39 347 555 0192"
FIELDS_REFUSED = "Latet reads the content of a model's call, and this request holds "


def _prepare(on_server: bool, path: str, body: dict, upstream: Upstream = OPENAI, headers: dict | None = None):
    mediator = _mediator(None, on_server)
    raw = json.dumps(body).encode()
    return mediator.prepare(upstream, "POST", path, headers or BEARER, raw), raw


def _in_clear(prepared) -> list[str]:
    sent = prepared.body.decode()
    return [value for value in (EMAIL, PHONE) if value in sent or json.dumps(value)[1:-1] in sent]


@pytest.mark.parametrize("on_server", [False, True])
def test_a_legacy_prompt_given_as_a_list_and_its_suffix_are_masked(on_server):
    body = {"model": "m", "prompt": [f"write to {EMAIL}", f"call {PHONE}"], "suffix": f"cc {EMAIL}"}
    prepared, _ = _prepare(on_server, "/openai/v1/completions", body)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    assert len(json.loads(prepared.body)["prompt"]) == 2


@pytest.mark.parametrize("prompt", [[1212, 318], [[1212, 318], [257]], [f"write to {EMAIL}", [1212, 318]], 1212])
def test_a_legacy_prompt_given_as_tokens_is_refused_on_a_server_and_sent_as_it_came_on_a_computer(prompt):
    body = {"model": "m", "prompt": prompt}
    refused, _ = _prepare(True, "/openai/v1/completions", body)
    assert (refused.block_reason or "").startswith(f"{FIELDS_REFUSED}a prompt ")
    assert "1212" not in (refused.block_reason or "") and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/completions", body)
    assert (
        computer.block_reason is None and EMAIL not in computer.body.decode()
    )  # its text read and masked all the same
    if not any(isinstance(part, str) for part in (prompt if isinstance(prompt, list) else [])):
        assert computer.body == raw  # nothing it reads: as it came


@pytest.mark.parametrize("on_server", [False, True])
def test_the_variables_of_a_prompt_are_masked(on_server):
    variables = {"owner": EMAIL, "note": {"type": "input_text", "text": f"call {PHONE}"}, "limit": 3}
    body = {"model": "m", "input": "draft the reply", "prompt": {"id": "pmpt_1", "variables": variables}}
    prepared, _ = _prepare(on_server, "/openai/v1/responses", body)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    assert json.loads(prepared.body)["prompt"]["variables"]["limit"] == 3


@pytest.mark.parametrize("variables", [{"owner": {"type": "a_new_kind", "value": EMAIL}}, ["owner", EMAIL]])
def test_a_prompt_variable_latet_does_not_read_is_refused_on_a_server(variables):
    body = {"model": "m", "input": "hi", "prompt": {"id": "pmpt_1", "variables": variables}}
    refused, _ = _prepare(True, "/openai/v1/responses", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/responses", body)
    assert computer.block_reason is None and computer.body == raw


# A function's definition naming an address and a phone number, in its description and in the schema of its
# parameters (a parameter named by one, the values another takes).
SCHEMA = {"type": "object", "properties": {PHONE: {"type": "string"}, "to": {"type": "string", "enum": [EMAIL]}}}
DEFINITIONS = {
    "chat completions": ("/openai/v1/chat/completions", OPENAI, {
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "notify", "description": f"writes to {EMAIL}",
                                                    "parameters": SCHEMA}}],
        "functions": [{"name": "call", "description": f"calls {PHONE}", "parameters": SCHEMA}],
    }),
    "responses": ("/openai/v1/responses", OPENAI, {
        "model": "m", "input": "hi",
        "tools": [
            {"type": "function", "name": "notify", "description": f"writes to {EMAIL}", "parameters": SCHEMA},
            {"type": "custom", "name": "draft", "description": "a draft",
             "format": {"type": "grammar", "syntax": "lark", "definition": f'start: "to {EMAIL}"'}},
            {"type": "mcp", "server_label": "crm", "server_url": "https://mcp.example.com",
             "server_description": f"the CRM of {EMAIL}"},
        ],
    }),
    "anthropic with an API key": ("/anthropic/v1/messages", ANTHROPIC, {
        "model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "notify", "description": f"writes to {EMAIL}", "input_schema": SCHEMA}],
    }),
}  # fmt: skip


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize("route", list(DEFINITIONS))
def test_the_tools_definitions_are_masked_and_their_names_left_as_they_are(route, on_server):
    path, upstream, body = DEFINITIONS[route]
    headers = {"x-api-key": "sk-ant-api-test"} if upstream is ANTHROPIC else None
    prepared, _ = _prepare(on_server, path, body, upstream, headers)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    sent = json.loads(prepared.body)

    def names(tools: list[dict]) -> list[str]:
        return [t.get("name") or t.get("server_label") or t["function"]["name"] for t in tools]

    assert names(sent["tools"]) == names(body["tools"])
    if route == "responses":
        assert sent["tools"][2]["server_url"] == "https://mcp.example.com"  # what lets the provider reach it


def test_a_requests_tools_are_read_before_its_conversation_each_image_kept_in_its_place():
    """As a model reads a request: its tools, then its conversation. An image's place among the texts (where its text
    goes once read) moves after them, beside the text it came with."""
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    body = {
        "model": "m",
        "tools": [{"type": "function", "function": {"name": "notify", "description": "writes", "parameters": SCHEMA}}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, image]}],
    }
    scan = openai_chat.scan_request(body)
    [found] = scan.images
    assert found.at == 1  # after the user's text
    _definitions("chat", body, scan)
    assert [s.kind.value for s in scan.slots] == ["tool_description", "tool_description", "user_text"]
    assert found.at == 3


def test_claude_codes_tools_leave_a_computer_as_its_plugin_wrote_them():
    """On a computer, a request Latet only checks sends its tools as they came (the plugin masks their descriptions),
    stopped by nothing in them."""
    body = {**DEFINITIONS["anthropic with an API key"][2]}
    prepared, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert prepared.mode == VERIFY and prepared.block_reason is None and prepared.body == raw


@pytest.mark.parametrize(
    "tools",
    [
        [{"type": "function", "function": {"name": "notify", "description": 7}}],
        [{"type": "function", "function": {"name": "notify", "parameters": f"to {EMAIL}"}}],
        [{"type": "function", "function": f"notify {EMAIL}"}],
        [f"notify {EMAIL}"],
        {"notify": EMAIL},
    ],
)
def test_a_tool_definition_latet_cannot_read_is_refused_on_a_server_and_sent_as_it_came_on_a_computer(tools):
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    refused, _ = _prepare(True, "/openai/v1/chat/completions", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/chat/completions", body)
    assert computer.block_reason is None and computer.body == raw


# A turn of the Realtime shape: its instructions, input and tools under ``response``.
REALTIME = {
    "type": "response.create",
    "response": {
        "instructions": f"answer as {EMAIL}",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"call {PHONE}"}]}],
        "tools": [{"type": "function", "name": "notify", "description": f"writes to {EMAIL}", "parameters": SCHEMA}],
    },
}


@pytest.mark.parametrize("on_server", [False, True])
def test_a_turn_of_the_realtime_shape_is_masked_in_its_response(on_server):
    mediator, session = _session_mediator(on_server)
    text, prepared = mediator.mask_ws_frame(json.dumps(REALTIME), session)
    assert prepared is not None and prepared.block_reason is None
    assert EMAIL not in text and PHONE not in text and json.loads(text)["response"]["tools"][0]["name"] == "notify"


def test_an_image_of_a_realtime_turn_keeps_its_place_after_the_frames_own_texts():
    image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    content = [{"type": "input_text", "text": "what is this?"}, image]
    frame = {
        "type": "response.create",
        "instructions": "be brief",
        "response": {"input": [{"role": "user", "content": content}]},
    }
    scan = openai_responses.scan_turn(frame)
    [found] = scan.images
    assert [s.get() for s in scan.slots] == ["be brief", "what is this?"] and found.at == 2


def test_a_turns_response_latet_cannot_read_is_refused_on_a_server_and_sent_as_it_came_on_a_computer():
    frame = json.dumps({"type": "response.create", "response": f"answer as {EMAIL}"})
    mediator, session = _session_mediator(True)
    _, refused = mediator.mask_ws_frame(frame, session)
    assert refused is not None and (refused.block_reason or "").startswith(FIELDS_REFUSED)
    mediator, session = _session_mediator(False)
    text, sent = mediator.mask_ws_frame(frame, session)
    assert sent is not None and sent.block_reason is None and text == frame


def test_a_spaces_app_masks_a_prompt_list_and_refuses_a_prompt_of_tokens(space):
    rt, _ = space
    seen = _upstream(rt)
    with _client(rt) as c:
        listed = c.post("/llm/v1/completions", json={"model": "m", "prompt": [f"write to {EMAIL}"]}, headers=BEARER)
        tokens = c.post("/llm/v1/completions", json={"model": "m", "prompt": [1212, 318]}, headers=BEARER)
    assert listed.status_code == 200 and tokens.status_code == 400 and "a prompt given as tokens" in tokens.text
    assert [r.url.path for r in seen] == ["/v1/completions"] and EMAIL.encode() not in seen[0].content


# --- the kinds of a Responses request's input, and the fields that carry content beside it ------------------------------


def _responses(*items: object, **fields: object) -> dict:
    return {"model": "m", "input": [{"role": "user", "content": "go on"}, *items], **fields}


@pytest.mark.parametrize("on_server", [False, True])
def test_a_shell_calls_output_is_masked_in_each_of_its_chunks(on_server):
    """``shell_call_output``: its output is a list of what each command printed, its standard output and error, and how
    it ended. Each printed text is read and masked; how it ended is left as it is."""
    call = {"type": "shell_call", "call_id": "c1", "action": {"commands": ["cat owners.txt"]}}
    ended = {"type": "exit", "exit_code": 0}
    output = {"type": "shell_call_output", "call_id": "c1",
              "output": [{"stdout": f"owner: {EMAIL}", "stderr": f"call {PHONE}", "outcome": ended}]}  # fmt: skip
    prepared, _ = _prepare(on_server, "/openai/v1/responses", _responses(call, output))
    assert prepared.block_reason is None and _in_clear(prepared) == []
    assert json.loads(prepared.body)["input"][2]["output"][0]["outcome"] == ended


@pytest.mark.parametrize("on_server", [False, True])
def test_an_approval_answers_reason_and_tools_given_as_an_item_are_masked(on_server):
    """An answer to a request for approval (``mcp_approval_response``) gives its reason in the client's words; tools
    given as an item of the input (``additional_tools``) are read as a request's tools are."""
    approval = {"type": "mcp_approval_response", "approval_request_id": "a1", "approve": False,
                "reason": f"do not write to {EMAIL}"}  # fmt: skip
    tools = {"type": "additional_tools", "tools": [{"type": "function", "name": "notify",
                                                   "description": f"calls {PHONE}", "parameters": SCHEMA}]}  # fmt: skip
    prepared, _ = _prepare(on_server, "/openai/v1/responses", _responses(approval, tools))
    assert prepared.block_reason is None and _in_clear(prepared) == []
    sent = json.loads(prepared.body)["input"]
    assert sent[1]["approve"] is False and sent[2]["tools"][0]["name"] == "notify"


# What the provider made, or what holds no content: a server passes them as a computer does.
PROVIDERS = [
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA"},
    {"type": "compaction", "id": "cmp_1", "encrypted_content": "gAAAA"},
    {"type": "item_reference", "id": "msg_1"},
    {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": {"type": "search", "query": "weather"}},
    {"type": "file_search_call", "id": "fs_1", "status": "completed", "queries": ["pricing"]},
    {"type": "image_generation_call", "id": "ig_1", "status": "completed", "result": "AAAA"},
    {"type": "code_interpreter_call", "id": "ci_1", "status": "completed", "code": "print(1)", "container_id": "c"},
    {"type": "computer_call", "id": "cu_1", "call_id": "c2", "action": {"type": "click", "x": 1, "y": 2}},
    {"type": "apply_patch_call", "id": "ap_1", "call_id": "c3", "operation": {"type": "update_file", "path": "a"}},
    {"type": "mcp_list_tools", "id": "ml_1", "server_label": "crm", "tools": []},
    {"type": "mcp_approval_request", "id": "ar_1", "server_label": "crm", "name": "send", "arguments": "{}"},
    {"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "I cannot help with that."}]},
]


@pytest.mark.parametrize("item", PROVIDERS, ids=[str(item["type"]) for item in PROVIDERS])
def test_what_the_provider_made_or_what_holds_no_content_passes_on_a_server(item):
    prepared, _ = _prepare(True, "/openai/v1/responses", _responses(item))
    assert prepared.block_reason is None


UNKNOWN_ITEMS = {
    "a kind Latet does not read": {"type": "a_new_kind", "text": f"write to {EMAIL}"},
    "an item that is not an object": f"write to {EMAIL}",
    "a kind that is not text": {"type": ["message"], "content": f"write to {EMAIL}"},
    "a part of a kind Latet does not read": {"role": "user", "content": [{"type": "a_new_part", "text": EMAIL}]},
    "a part that is not an object": {"role": "user", "content": [f"write to {EMAIL}"]},
    "a part whose kind is not text": {"role": "user", "content": [{"type": {"text": EMAIL}}]},
    "an output's part of a kind Latet does not read": {
        "type": "function_call_output", "call_id": "c1", "output": [{"type": "a_new_part", "text": EMAIL}]},
    "audio given back": {"role": "assistant", "content": [{"type": "output_audio", "transcript": f"mail {EMAIL}"}]},
    "audio of the Realtime shape": {"role": "assistant", "content": [{"type": "audio", "transcript": EMAIL}]},
    "audio heard, with its transcript": {
        "role": "user", "content": [{"type": "input_audio", "audio": "AAAA", "transcript": f"mail {EMAIL}"}]},
    "a call's arguments that are not text": {
        "type": "function_call", "call_id": "c1", "name": "notify", "arguments": {"to": EMAIL}},
    "content that is an object of no kind Latet reads": {"role": "user", "content": {"text": f"write to {EMAIL}"}},
    "content that is neither text nor parts": {"role": "user", "content": 42},
}  # fmt: skip


@pytest.mark.parametrize("item", list(UNKNOWN_ITEMS.values()), ids=list(UNKNOWN_ITEMS))
def test_an_input_latet_does_not_read_is_refused_on_a_server_and_sent_as_it_came_on_a_computer(item):
    """A kind of item or of part Latet does not know, an item or a part that is not an object, audio and what was heard
    in it: a server refuses the request, in words that name nothing of it; a computer sends it as it came."""
    body = _responses(item)
    refused, _ = _prepare(True, "/openai/v1/responses", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/responses", body)
    assert computer.block_reason is None and computer.body == raw


def test_audio_in_a_turn_of_the_realtime_shape_is_refused_on_a_server_and_sent_as_it_came_on_a_computer():
    item = {"type": "message", "role": "assistant", "content": [{"type": "audio", "transcript": f"mail {EMAIL}"}]}
    frame = json.dumps({"type": "response.create", "response": {"input": [item]}})
    mediator, session = _session_mediator(True)
    _, refused = mediator.mask_ws_frame(frame, session)
    assert refused is not None and (refused.block_reason or "").startswith(FIELDS_REFUSED)
    mediator, session = _session_mediator(False)
    text, sent = mediator.mask_ws_frame(frame, session)
    assert sent is not None and sent.block_reason is None and text == frame


ANTHROPIC_KEY = {"x-api-key": "sk-ant-api-test", "content-type": "application/json"}
EXAMPLES = [{"name": "notify", "description": "writes", "input_schema": SCHEMA, "input_examples": [{"to": EMAIL}]}]


@pytest.mark.parametrize("on_server", [False, True])
def test_a_tools_examples_are_masked(on_server):
    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], "tools": EXAMPLES}
    prepared, _ = _prepare(on_server, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    assert len(json.loads(prepared.body)["tools"][0]["input_examples"]) == 1


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize("content", [f"mail {EMAIL}", [{"type": "text", "text": f"mail {EMAIL}"}]])
def test_a_predicted_output_is_masked(content, on_server):
    body = {"model": "m", "messages": [{"role": "user", "content": "fix it"}], "prediction": {"type": "content",
                                                                                         "content": content}}  # fmt: skip
    prepared, _ = _prepare(on_server, "/openai/v1/chat/completions", body)
    assert prepared.block_reason is None and _in_clear(prepared) == []


OUTPUT_FORMAT = {"name": "reply", "description": f"a reply to {EMAIL}", "schema": SCHEMA, "strict": True}


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/openai/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                         "response_format": {"type": "json_schema", "json_schema": OUTPUT_FORMAT}}),
        ("/openai/v1/responses", {"model": "m", "input": "hi",
                                  "text": {"format": {"type": "json_schema", **OUTPUT_FORMAT}, "verbosity": "low"}}),
    ],
    ids=["chat completions", "responses"],
)  # fmt: skip
def test_the_schema_of_a_structured_output_is_masked_and_its_name_left_as_it_is(path, body, on_server):
    prepared, _ = _prepare(on_server, path, body)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    sent = json.loads(prepared.body)
    fmt = sent["response_format"]["json_schema"] if "response_format" in sent else sent["text"]["format"]
    assert fmt["name"] == "reply" and fmt["strict"] is True


@pytest.mark.parametrize("on_server", [False, True])
def test_the_schema_of_a_turns_structured_output_is_masked(on_server):
    frame = {"type": "response.create", "model": "m", "input": "hi",
             "text": {"format": {"type": "json_schema", **OUTPUT_FORMAT}}}  # fmt: skip
    mediator, session = _session_mediator(on_server)
    text, prepared = mediator.mask_ws_frame(json.dumps(frame), session)
    assert prepared is not None and prepared.block_reason is None and EMAIL not in text and PHONE not in text


def test_a_structured_output_latet_cannot_read_is_refused_on_a_server_and_sent_as_it_came_on_a_computer():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": f"a reply to {EMAIL}"}}  # fmt: skip
    refused, _ = _prepare(True, "/openai/v1/chat/completions", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/chat/completions", body)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize(
    ("path", "body", "upstream", "headers"),
    [
        ("/openai/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                         "user": EMAIL, "safety_identifier": PHONE,
                                         "metadata": {"owner": EMAIL, "tags": ["a"]}}, OPENAI, None),
        ("/openai/v1/responses", {"model": "m", "input": "hi", "user": EMAIL, "safety_identifier": PHONE,
                                  "metadata": {"owner": EMAIL, "n": {"deep": 1}}}, OPENAI, None),
        ("/anthropic/v1/messages", {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
                                    "metadata": {"user_id": EMAIL}}, ANTHROPIC, ANTHROPIC_KEY),
    ],
    ids=["chat completions", "responses", "anthropic"],
)  # fmt: skip
def test_who_a_request_says_it_is_for_is_masked_where_latet_rewrites_and_never_refused(
    path, body, upstream, headers, on_server
):
    """``user``, ``safety_identifier`` and ``metadata``: what a client says of whom the call is for, and its own notes.
    Each text is read and masked; a value that is no text is left as it is, never refused."""
    prepared, _ = _prepare(on_server, path, body, upstream, headers)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    sent, kept = json.loads(prepared.body), body["metadata"]
    assert {k: v for k, v in sent["metadata"].items() if not isinstance(v, str)} == {
        k: v for k, v in kept.items() if not isinstance(v, str)
    }


def test_who_claude_codes_request_says_it_is_for_is_checked():
    """A request Latet only checks: its metadata is read by the final check. Claude Code's own (a hash and ids) leaves;
    an address in its place stops the request."""
    ids = (
        "user_"
        + "9" * 64
        + "_account_4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11_session_4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a12"
    )
    body = {
        "model": "m",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": ids},
    }
    own, raw = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert own.mode == VERIFY and own.block_reason is None and own.body == raw
    body["metadata"]["user_id"] = EMAIL
    stopped, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert stopped.block_reason is not None and "EMAIL[21]" in stopped.block_reason


SUBSCRIPTION = {
    "content-type": "application/json",
    "user-agent": "some-agent/1.0",
    "authorization": "Bearer sk-ant-oat01",
}


@pytest.mark.parametrize(
    "tools", [DEFINITIONS["anthropic with an API key"][2]["tools"], EXAMPLES], ids=["schema", "examples"]
)
def test_a_server_checks_claude_codes_tools_and_stops_a_value_left_in_clear_in_them(tools):
    """A server never trusts a computer: the plugin masks the descriptions of Claude Code's tools, and a server's final
    check reads the descriptions, the schemas of their parameters and the examples of their input, as it reads another
    checked client's. A value left in clear there stops the request (an MCP tool's schema naming a customer's address
    left the server unread)."""
    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    prepared, raw = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert prepared.mode == VERIFY and prepared.body == raw
    assert prepared.block_reason is not None and "EMAIL[21]" in prepared.block_reason


# Claude Code's own tools, as its plugin sends them: a description it masked, and a schema that names no value of yours.
BASH_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The command to execute"},
        "timeout": {"type": "number", "description": "Optional timeout in milliseconds (max 600000)"},
        "description": {"type": "string", "description": "Clear, concise description of what this command does"},
    },
    "required": ["command"],
    "additionalProperties": False,
}


def test_a_server_sends_claude_codes_tools_masked_by_its_plugin_as_they_came():
    mediator = _mediator(None, True)
    described = mediator.engine.masker.mask(f"Commits as {EMAIL}.", SlotKind.TOOL_DESCRIPTION).text  # the plugin's
    assert EMAIL not in described
    tools = [
        {"name": "Bash", "description": described, "input_schema": BASH_SCHEMA},
        {"name": "Read", "description": "Reads a file", "input_schema": {"type": "object", "properties": {}}},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
    ]
    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    raw = json.dumps(body).encode()
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY and prepared.block_reason is None and prepared.body == raw


@pytest.mark.parametrize("on_server", [False, True])
def test_another_checked_clients_tools_are_read_by_the_final_check(on_server):
    """A client other than Claude Code, on a route Latet only checks, has no plugin to mask its tools: the final check
    reads their descriptions, their parameters and their examples, as the client wrote them."""
    for tools in (DEFINITIONS["anthropic with an API key"][2]["tools"], EXAMPLES):
        body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], "tools": tools}
        prepared, raw = _prepare(on_server, "/anthropic/v1/messages", body, ANTHROPIC, SUBSCRIPTION)
        assert prepared.mode == VERIFY and prepared.body == raw
        assert prepared.block_reason is not None and "EMAIL[21]" in prepared.block_reason


# --- Anthropic's structured output ---------------------------------------------------------------------------------

ANTHROPIC_FORMAT = {"type": "json_schema", "schema": SCHEMA}


def _anthropic(**fields: object) -> dict:
    return {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}], **fields}


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize(
    "fields", [{"output_config": {"format": ANTHROPIC_FORMAT, "effort": "high"}}, {"output_format": ANTHROPIC_FORMAT}]
)
def test_anthropics_structured_output_is_masked(fields, on_server):
    """``output_config.format``, and the older ``output_format``: the schema of the answer Anthropic is asked for, read
    as a tool's parameters are."""
    prepared, _ = _prepare(on_server, "/anthropic/v1/messages", _anthropic(**fields), ANTHROPIC, ANTHROPIC_KEY)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    sent = json.loads(prepared.body)
    assert sent.get("output_config", {}).get("effort", "high") == "high"


@pytest.mark.parametrize("headers", [CLAUDE_CODE, SUBSCRIPTION], ids=["claude code", "subscription"])
def test_anthropics_structured_output_is_checked_where_latet_only_checks(headers):
    body = _anthropic(output_config={"format": ANTHROPIC_FORMAT})
    stopped, raw = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, headers)
    assert stopped.mode == VERIFY and stopped.body == raw
    assert stopped.block_reason is not None and "EMAIL[21]" in stopped.block_reason


@pytest.mark.parametrize(
    "fields",
    [
        {"output_config": {"format": f"a reply to {EMAIL}"}},
        {"output_config": f"a reply to {EMAIL}"},
        {"output_format": {"type": "json_schema", "schema": f"a reply to {EMAIL}"}},
        {"output_format": [f"a reply to {EMAIL}"]},
    ],
)
def test_an_anthropic_structured_output_latet_cannot_read_is_refused_on_a_server(fields):
    body = _anthropic(**fields)
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert computer.block_reason is None and computer.body == raw


# --- what chat completions and Anthropic's messages carry, read or refused on a server ----------------------------


def _chat(*messages: object, **fields: object) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": "go on"}, *messages], **fields}


@pytest.mark.parametrize("on_server", [False, True])
def test_a_chat_refusal_and_a_custom_tools_input_are_masked(on_server):
    """An assistant's refusal, as a part of its content or beside it, is the model's text and is read as such; so is
    the input of a call of a custom tool (``tool_calls[].custom.input``)."""
    refusal = {"role": "assistant", "content": [{"type": "refusal", "refusal": f"I will not write to {EMAIL}"}]}
    beside = {"role": "assistant", "content": None, "refusal": f"I will not call {PHONE}"}
    custom = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "custom", "custom": {"name": "draft", "input": f"to {EMAIL}"}}]}  # fmt: skip
    prepared, _ = _prepare(on_server, "/openai/v1/chat/completions", _chat(refusal, beside, custom))
    assert prepared.block_reason is None and _in_clear(prepared) == []


CLIP = {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}  # an audio clip, nothing heard


@pytest.mark.parametrize("on_server", [False, True])
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/openai/v1/chat/completions", _chat({"role": "user", "content": [{"type": "text", "text": "hear"}, CLIP]})),
        (
            "/openai/v1/responses",
            _responses({"role": "user", "content": [{"type": "input_text", "text": "hear"}, CLIP]}),
        ),
    ],
    ids=["chat completions", "responses"],
)
def test_an_audio_clip_is_sent_as_a_document_is_by_a_server_too(path, body, on_server):
    """An audio clip a request attaches, with nothing of what was heard in it, is not read: sent as it came where
    ``images`` lets what Latet does not read through (as by default), by a server too, and not sent where it does not."""
    sent, raw = _prepare(on_server, path, body)
    assert sent.block_reason is None and sent.body == raw and sent.uninspected == ["input_audio"]
    mediator = _mediator(None, on_server)
    mediator.images = image_policy(Config(images="block"))
    stopped = mediator.prepare(OPENAI, "POST", path, BEARER, raw)
    assert stopped.block_reason is not None and "input_audio" in stopped.block_reason


UNREAD_CHAT = {
    "a part of a kind Latet does not read": _chat({"role": "user", "content": [
        {"type": "text", "text": "watch"}, {"type": "video_url", "video_url": {"url": f"https://x.test/?to={EMAIL}"}}]}),
    "a part that is not an object": _chat({"role": "user", "content": [f"write to {EMAIL}"]}),
    "a part whose kind is not text": _chat({"role": "user", "content": [{"type": ["text"], "text": EMAIL}]}),
    "a part whose text is not text": _chat({"role": "user", "content": [{"type": "text", "text": [EMAIL]}]}),
    "content that is neither text nor parts": _chat({"role": "user", "content": {"text": f"write to {EMAIL}"}}),
    "a message that is not an object": _chat(f"write to {EMAIL}"),
    "messages that are not a list": {"model": "m", "messages": {"role": "user", "content": f"write to {EMAIL}"}},
    "a tool call that is not an object": _chat({"role": "assistant", "tool_calls": [f"notify {EMAIL}"]}),
    "tool calls that are not a list": _chat({"role": "assistant", "tool_calls": {
        "id": "c1", "type": "function", "function": {"name": "notify", "arguments": json.dumps({"to": EMAIL})}}}),
    "an older call's arguments that are not text": _chat({"role": "assistant", "content": None,
                                                         "function_call": {"name": "notify", "arguments": {"to": EMAIL}}}),
    "a call's arguments that are not text": _chat({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "notify", "arguments": {"to": EMAIL}}}]}),
    "a tool call of a kind Latet does not read": _chat({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "a_new_kind", "a_new_kind": {"input": f"to {EMAIL}"}}]}),
    "a refusal that is not text": _chat({"role": "assistant", "content": None, "refusal": [EMAIL]}),
}  # fmt: skip


@pytest.mark.parametrize("body", list(UNREAD_CHAT.values()), ids=list(UNREAD_CHAT))
def test_what_chat_completions_carry_unread_is_refused_on_a_server_and_sent_as_it_came_on_a_computer(body):
    refused, _ = _prepare(True, "/openai/v1/chat/completions", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/chat/completions", body)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize("on_server", [False, True])
def test_a_tool_added_in_the_conversation_is_read_as_a_requests_tools_are(on_server):
    """Claude Code adds and removes tools as it goes, in a system message of the conversation: a tool added comes with
    its definition, read as a tool of the request is; a tool removed is named alone."""
    added = {"role": "system", "content": [
        {"type": "text", "text": "tools changed"},
        {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "old"}},
        {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": {
            "name": "notify", "description": f"writes to {EMAIL}", "input_schema": SCHEMA}}},
        {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "search"}},
    ]}  # fmt: skip
    body = _anthropic(messages=[{"role": "user", "content": "hi"}, added])
    prepared, _ = _prepare(on_server, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert prepared.block_reason is None and _in_clear(prepared) == []
    definition = json.loads(prepared.body)["messages"][1]["content"][2]["tool"]["definition"]
    assert definition["name"] == "notify"
    # Where Latet only checks, as the request's own tools: Claude Code's plugin masks their descriptions, which a
    # computer sends as they came and a server's final check reads; and that check reads another client's, which has
    # no plugin.
    own, raw = _prepare(on_server, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert own.mode == VERIFY and own.body == raw
    if on_server:
        assert own.block_reason is not None and "EMAIL[21]" in own.block_reason
    else:
        assert own.block_reason is None
    other, raw = _prepare(on_server, "/anthropic/v1/messages", body, ANTHROPIC, SUBSCRIPTION)
    assert other.mode == VERIFY and other.body == raw
    assert other.block_reason is not None and "EMAIL[21]" in other.block_reason


def test_a_tool_added_in_a_form_latet_does_not_read_is_refused_on_a_server_as_a_requests_tools_are():
    added = {"role": "system", "content": [{"type": "tool_addition", "tool": {"type": "tool_bundle", "tools": []}}]}
    body = _anthropic(messages=[{"role": "user", "content": "hi"}, added])
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED)
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert computer.block_reason is None and computer.body == raw
    # Claude Code's: refused by a server, which reads its tools too; sent as it came by a computer, which does not.
    own, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert (own.block_reason or "").startswith(FIELDS_REFUSED)
    own, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert own.block_reason is None and own.body == raw


# Blocks Claude Code sends back that the provider made, or that hold no content: a server passes them as a computer does.
CLAUDE_CODES_BLOCKS = [
    {"type": "thinking", "thinking": "the user wants the status", "signature": "EqQB"},
    {"type": "redacted_thinking", "data": "EmwK"},
    {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "status page"}},
    {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [
        {"type": "web_search_result", "url": "https://status.example.com", "title": "Status", "encrypted_content": "E"}]},
    {"type": "web_fetch_tool_result", "tool_use_id": "srv_2", "content": {"type": "web_fetch_result", "url": "u"}},
    {"type": "code_execution_tool_result", "tool_use_id": "srv_3", "content": {"type": "code_execution_result"}},
    {"type": "bash_code_execution_tool_result", "tool_use_id": "srv_4", "content": {"stdout": "ok"}},
    {"type": "text_editor_code_execution_tool_result", "tool_use_id": "srv_5", "content": {"file_type": "text"}},
    {"type": "tool_search_tool_result", "tool_use_id": "srv_6", "content": {
        "type": "tool_search_tool_search_result", "tool_references": [{"type": "tool_reference", "tool_name": "x"}]}},
    {"type": "advisor_tool_result", "tool_use_id": "srv_7", "content": {"type": "advisor_result", "text": "fine"}},
    {"type": "mcp_tool_use", "id": "mcp_1", "name": "list", "server_name": "crm", "input": {}},
    {"type": "mcp_tool_result", "tool_use_id": "mcp_1", "content": [{"type": "text", "text": "two items"}]},
    {"type": "mcp_tool_listing", "server_name": "crm", "tools": []},
    {"type": "compaction", "content": "The user asked for the status; it is up."},
    {"type": "connector_text", "text": "Checking the status page."},
    {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
    {"type": "container_upload", "file_id": "file_1"},
]  # fmt: skip


@pytest.mark.parametrize("block", CLAUDE_CODES_BLOCKS, ids=[str(b["type"]) for b in CLAUDE_CODES_BLOCKS])
def test_a_block_the_provider_made_passes_on_a_server(block):
    body = _anthropic(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": [block]},
                                {"role": "user", "content": "go on"}])  # fmt: skip
    prepared, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert prepared.block_reason is None, block["type"]


def test_a_tool_result_naming_a_tool_passes_on_a_server():
    """A tool search of Claude Code's own answers with references to the tools it found, in a tool's result."""
    result = {"type": "tool_result", "tool_use_id": "t1", "content": [
        {"type": "tool_reference", "tool_name": "notify"}, {"type": "text", "text": "found one"}]}  # fmt: skip
    call = {"type": "tool_use", "id": "t1", "name": "ToolSearch", "input": {"query": "notify"}}
    body = _anthropic(messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": [call]},
                                {"role": "user", "content": [result]}])  # fmt: skip
    prepared, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, CLAUDE_CODE)
    assert prepared.block_reason is None


UNREAD_ANTHROPIC = {
    "a block of a kind Latet does not read": [{"role": "user", "content": [
        {"type": "text", "text": "see"}, {"type": "a_new_block", "text": f"mail {EMAIL}"}]}],
    "a block that is not an object": [{"role": "user", "content": [f"mail {EMAIL}"]}],
    "a block whose kind is not text": [{"role": "user", "content": [{"type": {"t": 1}, "text": EMAIL}]}],
    "a text block whose text is not text": [{"role": "user", "content": [{"type": "text", "text": [EMAIL]}]}],
    "a tool call whose input is a string": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "send", "input": f"to {EMAIL}"}]}],
    "a tool's result that is neither text nor blocks": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": {"text": f"to {EMAIL}"}}]}],
    "content that is neither text nor blocks": [{"role": "user", "content": {"text": f"mail {EMAIL}"}}],
    "a message that is not an object": [f"mail {EMAIL}"],
    "a tool added in another form": [{"role": "system", "content": [
        {"type": "tool_addition", "tool": {"type": "a_new_form", "text": f"mail {EMAIL}"}}]}],
}  # fmt: skip


@pytest.mark.parametrize("messages", list(UNREAD_ANTHROPIC.values()), ids=list(UNREAD_ANTHROPIC))
def test_what_anthropics_messages_carry_unread_is_refused_on_a_server_and_sent_as_it_came_on_a_computer(messages):
    body = _anthropic(messages=messages)
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert computer.block_reason is None and computer.body == raw


@pytest.mark.parametrize(
    "fields",
    [
        {"system": {"text": f"mail {EMAIL}"}},
        {"system": [{"type": "a_new_block", "text": f"mail {EMAIL}"}]},
        {"system": [f"mail {EMAIL}"]},
        {"messages": {"role": "user", "content": f"mail {EMAIL}"}},
    ],
    ids=[
        "system as an object",
        "a system block of another kind",
        "a system block not an object",
        "messages not a list",
    ],
)
def test_anthropics_own_fields_in_another_form_are_refused_on_a_server(fields):
    body = {**_anthropic(), **fields}
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert computer.block_reason is None and computer.body == raw


def test_messages_that_are_a_number_are_refused_on_a_server_and_sent_by_a_computer():
    """Neither a list nor anything Latet reads: a server refuses it; a computer sends it, the system prompt masked."""
    body = _anthropic(messages=5, system=f"mail {EMAIL}")
    refused, _ = _prepare(True, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED)
    computer, _ = _prepare(False, "/anthropic/v1/messages", body, ANTHROPIC, ANTHROPIC_KEY)
    sent = json.loads(computer.body)
    assert computer.block_reason is None and sent["messages"] == 5 and EMAIL not in sent["system"]


# --- the Responses API's own fields in another form -----------------------------------------------------------------

UNREAD_RESPONSES_FIELDS = {
    "an input that is one item, an object": {"model": "m", "input": {"role": "user", "content": f"write to {EMAIL}"}},
    "instructions given as parts": {"model": "m", "input": "hi",
                                    "instructions": [{"type": "input_text", "text": f"the owner is {EMAIL}"}]},
    "a prompt that is a string": {"model": "m", "input": "hi", "prompt": f"pmpt_1 for {EMAIL}"},
}  # fmt: skip


@pytest.mark.parametrize("body", list(UNREAD_RESPONSES_FIELDS.values()), ids=list(UNREAD_RESPONSES_FIELDS))
def test_the_responses_apis_own_fields_in_another_form_are_refused_on_a_server(body):
    refused, _ = _prepare(True, "/openai/v1/responses", body)
    assert (refused.block_reason or "").startswith(FIELDS_REFUSED) and EMAIL not in (refused.block_reason or "")
    computer, raw = _prepare(False, "/openai/v1/responses", body)
    assert computer.block_reason is None and computer.body == raw


def test_a_responses_refusal_is_masked_as_the_models_text():
    item = {"role": "assistant", "content": [{"type": "refusal", "refusal": f"I will not write to {EMAIL}"}]}
    prepared, _ = _prepare(True, "/openai/v1/responses", _responses(item))
    assert prepared.block_reason is None and _in_clear(prepared) == []
