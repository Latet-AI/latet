"""An answer never inflates past what Latet reads: decoded, it holds at most ``MAX_DECODED`` (a request's own bound),
whatever its content-encoding, and past it the answer is stopped with a clear error, never buffered without bound and
never restored in part. A provider of an identity's own (on a server, a destination the identity runs) could otherwise
send a few kilobytes that hold the instance every identity shares."""

import asyncio
import gzip
import json
import os
import threading
import time
import tracemalloc
import zlib

import brotli
import httpx
import pytest
import zstandard
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from latet.config import Config, IdentityConfig
from latet.engine.build import build
from latet.engine.masker import MaskReport
from latet.engine.types import SlotKind
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, create_app
from latet.gateway.mediate import RESTORED_EVENTS_AT_ONCE, REWRITE, JSONTransform, Mediator, Prepared, SSETransform
from latet.gateway.recorder import ResponseCapture
from latet.gateway.routes import Upstream
from latet.protocols import sse as sse_module
from latet.protocols.codec import MEMBERS_KEPT
from latet.protocols.rewrite import RestoreContext
from latet.protocols.sse import MAX_DECODED, ResponseError, ResponseTooLarge, SSEParser, StreamDecoder

from ..conftest import ROOT_KEY

ENCODINGS = ["gzip", "deflate", "br", "zstd"]
EMAIL = "emily.carter@acme.com"
_PIECE = b"a" * 2**20


def _encoded(encoding: str, *parts: bytes, repeat: int = 0) -> bytes:
    """``parts``, then a megabyte of one letter ``repeat`` times, encoded as ``encoding``, built a megabyte at a time:
    a few kilobytes on the wire for as much as one likes decoded."""
    pieces = [*parts, *([_PIECE] * repeat)]
    if encoding in ("gzip", "deflate"):
        z = zlib.compressobj(9, zlib.DEFLATED, (16 if encoding == "gzip" else 0) + zlib.MAX_WBITS)
        return b"".join([*(z.compress(p) for p in pieces), z.flush()])
    if encoding == "br":
        b = brotli.Compressor(quality=5)
        return b"".join([*(b.process(p) for p in pieces), b.finish()])
    s = zstandard.ZstdCompressor(level=3).compressobj()
    return b"".join([*(s.compress(p) for p in pieces), s.flush()])


def _fed(feed, wire: bytes, size: int = 64 * 2**10) -> int:
    """``wire`` given to ``feed`` as the forwarder gives an answer, ``size`` bytes at a time: the most one call answered."""
    largest = 0
    for at in range(0, len(wire), size):
        largest = max(largest, len(feed(wire[at : at + size]) or b""))
    return largest


def _peak(work) -> int:
    """What ``work()`` held at most, in bytes, while it ran."""
    tracemalloc.start()
    try:
        work()
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return peak


# --- the decoder, the parser, the JSON answer, the dashboard's capture -------------------------------------------------


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_answer_inflates_no_further_than_latet_reads(encoding):
    wire = _encoded(encoding, repeat=48)  # 48 MiB decoded, from kilobytes
    decoder = StreamDecoder(encoding)
    seen: list[int] = []

    def work():
        with pytest.raises(ResponseTooLarge):
            seen.append(_fed(decoder.feed, wire))

    assert _peak(work) < MAX_DECODED + 8 * 2**20


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_answer_under_the_bound_decodes_whole_whatever_the_pieces(encoding):
    body = b'{"text": "' + b"x" * 100_000 + b'"}'
    wire = _encoded(encoding, body)
    decoder = StreamDecoder(encoding)
    out = b"".join(decoder.feed(wire[at : at + 7]) for at in range(0, len(wire), 7)) + decoder.flush()
    assert out == body


@pytest.mark.parametrize("size", [4096, None])
@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_answer_of_megabytes_decodes_whole_whatever_its_encoding(encoding, size):
    """Every byte is handed on: brotli's decoder keeps some of what it decoded until it is asked again with no more
    input, and it is asked until it has nothing more."""
    body = b"".join(b'{"n":%d,"text":"%s"}\n' % (n, b"%x" % (n * 2654435761 % 2**64) * 4) for n in range(60_000))
    wire = _encoded(encoding, body)
    decoder = StreamDecoder(encoding)
    step = size or len(wire)
    out = b"".join(decoder.feed(wire[at : at + step]) for at in range(0, len(wire), step)) + decoder.flush()
    assert len(body) > 2 * 2**20 and out == body


def test_an_answer_the_review_found_100_kib_of_gzip_holding_100_mib_is_stopped_within_the_bound():
    """`probe_response_bomb.py`: the JSON answer's transform fed 100 KiB of gzip that holds 100 MiB."""
    wire = _encoded("gzip", b'{"id":"x","content":[{"type":"text","text":"', repeat=100)
    assert len(wire) < 110 * 2**10
    transform = JSONTransform("anthropic", None, "gzip")  # type: ignore[arg-type]  # ctx is read at end() alone

    def work():
        with pytest.raises(ResponseTooLarge):
            _fed(transform.feed, wire)

    assert _peak(work) < MAX_DECODED + 8 * 2**20 and len(transform.buf) <= MAX_DECODED


def test_one_chunk_of_brotli_the_review_found_is_stopped_within_the_bound():
    """`probe_brotli_chunk.py`: a few hundred bytes of brotli, in one chunk, that hold 200 MiB."""
    wire = _encoded("br", b"data: ", repeat=200)
    assert len(wire) < 1024
    decoder = StreamDecoder("br")

    def work():
        with pytest.raises(ResponseTooLarge):
            decoder.feed(wire)

    assert _peak(work) < MAX_DECODED + 8 * 2**20


def test_an_event_that_never_ends_is_stopped():
    parser = SSEParser(limit=2**20)
    assert [e.data for e in parser.feed(b"data: one\n\n")] == ["one"]
    parser.feed(b"data: " + b"a" * 2**19)
    with pytest.raises(ResponseTooLarge):
        parser.feed(b"a" * 2**19)


def test_a_json_answer_larger_than_latet_reads_is_stopped_even_unencoded():
    transform = JSONTransform("anthropic", None, None)  # type: ignore[arg-type]
    for _ in range(MAX_DECODED // 2**20):
        transform.feed(_PIECE)  # the bound itself still fits
    with pytest.raises(ResponseTooLarge):
        transform.feed(b"!")
    assert len(transform.buf) == MAX_DECODED


def test_an_answer_that_does_not_decode_as_it_says_is_an_error_of_its_own():
    with pytest.raises(ResponseError):
        StreamDecoder("gzip").feed(b"this is not gzip at all")


def test_a_zstd_frame_that_asks_for_a_large_window_is_not_decoded():
    """RFC 9659: an HTTP answer in zstd uses a window of 8 MiB at most; a decoder would set aside the window a frame asks
    for (128 MiB by default)."""
    params = zstandard.ZstdCompressionParameters.from_level(3, window_log=27)
    streamed = zstandard.ZstdCompressor(compression_params=params).compressobj()  # no size in the frame: its window
    wire = streamed.compress(b"a" * 2**20) + streamed.flush()
    with pytest.raises(ResponseError):
        StreamDecoder("zstd").feed(wire)
    within = zstandard.ZstdCompressor(level=19).compressobj()  # a usual frame decodes as before
    assert StreamDecoder("zstd").feed(within.compress(b"hello " * 1000) + within.flush()) == b"hello " * 1000


class _Handed:
    """A decoder whose calls are counted: the bytes of input each call is handed."""

    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def decompress(self, data, *args):  # zlib's
        self._calls.append(len(data))
        return self._inner.decompress(data, *args)

    def process(self, data, *args, **kwargs):  # brotli's
        self._calls.append(len(data))
        return self._inner.process(data, *args, **kwargs)

    def write(self, data):  # zstd's writer
        self._calls.append(len(data))
        return self._inner.write(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_one_large_chunk_is_handed_to_its_decoder_a_piece_at_a_time(encoding):
    """httpcore hands an answer over 64 KiB at a time; one that comes in one chunk is decoded the same way, a piece of
    64 KiB at most at each call, so that a chunk costs time in proportion to it (zlib hands back what it was not
    able to read: 64 MiB in one chunk took 9.8 s)."""
    body = os.urandom(2 * 2**20)  # what does not compress: a wire as long as the answer
    wire = _encoded(encoding, body)
    decoder, calls = StreamDecoder(encoding), []
    decoder._obj = _Handed(decoder._obj, calls)
    assert decoder.feed(wire) + decoder.flush() == body
    assert max(calls) <= 64 * 2**10


def test_an_answer_of_several_gzip_members_decodes_whole():
    """As Python's gzip reads it, member after member: the first one alone was decoded, silently, and an answer lost
    its end (a JSON answer then failed to parse and went back unrestored)."""
    parts = [b'{"text": "first,', b" second,", b' third"}']
    wire = b"".join(gzip.compress(part) for part in parts)
    for size in (len(wire), 7):
        decoder = StreamDecoder("gzip")
        out = b"".join(decoder.feed(wire[at : at + size]) for at in range(0, len(wire), size)) + decoder.flush()
        assert out == b"".join(parts) == gzip.decompress(wire)


def test_an_answer_of_more_gzip_members_than_a_client_writes_is_refused():
    assert StreamDecoder("gzip").feed(gzip.compress(b"x", mtime=0) * MEMBERS_KEPT) == b"x" * MEMBERS_KEPT
    with pytest.raises(ResponseError):
        StreamDecoder("gzip").feed(gzip.compress(b"x", mtime=0) * (MEMBERS_KEPT + 1))


def test_what_follows_a_gzip_member_and_is_no_member_is_refused_never_dropped():
    with pytest.raises(ResponseError):
        StreamDecoder("gzip").feed(gzip.compress(b'{"text": "done"}') + b"trailing words")


def test_a_piece_of_many_small_events_is_parsed_in_time_in_proportion_to_it():
    """Each event's end is looked for once: a piece of tens of thousands of tiny events (the dashboard's capture is
    handed a whole chunk, decoded) took time in proportion to its square (4 MiB: 33 s)."""
    event = b'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
    piece = event * (2 * 2**20 // len(event))
    started = time.perf_counter()
    events = SSEParser().feed(piece)
    assert len(events) == len(piece) // len(event) and time.perf_counter() - started < 1.0


def test_the_dashboards_capture_keeps_what_it_is_handed_and_reads_it_in_the_recorders_thread():
    """What the forwarder hands it on the event loop is only kept: a chunk of tens of thousands of tiny events was read
    there in one go (0.4 s for 4 MiB, once its parser looked for each end once). The recorder reads it (``settle``)."""
    event = b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"a"}}\n\n'
    count = 4 * 2**20 // len(event)
    wire = gzip.compress(event * count)  # before the clock starts: the test's own setup, not what it weighs
    capture = ResponseCapture("anthropic", "text/event-stream", "gzip")
    started = time.perf_counter()
    capture.feed(wire)
    capture.end()
    assert time.perf_counter() - started < 0.05  # kept, not read
    capture.settle()
    assert "".join(capture.text) == "a" * count and not capture.broken


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_the_dashboards_capture_stops_past_its_cap_without_decoding_further(encoding):
    wire = _encoded(encoding, b"data: ", repeat=48)
    capture = ResponseCapture("anthropic", "text/event-stream", encoding)

    def handed_and_read():
        _fed(capture.feed, wire)
        capture.end()
        capture.settle()  # what the recorder reads, in its thread

    assert _peak(handed_and_read) < MAX_DECODED + 8 * 2**20
    assert capture.broken
    decoded = capture._decoder
    assert decoded is not None and decoded.total <= 8_000_000 + 2 * 2**20  # stopped at its own cap, not the answer's
    total = decoded.total
    capture.feed(wire)  # past its cap: nothing more is decoded
    assert decoded.total == total


def test_the_dashboards_capture_of_an_unencoded_event_that_never_ends_stops_at_its_cap():
    capture = ResponseCapture("anthropic", "text/event-stream", None)
    capture.feed(b"data: ")
    for _ in range(9):
        capture.feed(_PIECE)  # one event of 9 MiB, which no encoding bounds
    assert capture.broken and len(capture._events._buf) <= 8_000_000 + 2**20


def test_a_stream_once_stopped_passes_nothing_more_on():
    transform = SSETransform("chat", RestoreContext([], []), "gzip", lambda response_id: None)
    stopped = transform.feed(_encoded("gzip", b"data: ", repeat=40))
    assert transform.stopped == "ResponseTooLarge" and b"latet_blocked" in stopped
    assert transform.feed(b'data: {"choices":[]}\n\n') == b"" and transform.end() == b""


def test_the_dashboards_capture_of_a_json_answer_stops_at_its_cap():
    capture = ResponseCapture("anthropic", "application/json", None)
    for _ in range(9):
        capture.feed(_PIECE)  # 9 MiB of a body never parsed past 8
    assert capture.broken and len(capture._body) <= 8_000_000


# --- through the gateway -------------------------------------------------------------------------------------------------


def _gateway(answer):
    """A gateway over a provider that answers ``answer(request)``, masking with the default rules."""
    built = build(Config(), Vault(ROOT_KEY))
    model = httpx.MockTransport(answer)
    config = GatewayConfig(
        port=5283,
        upstreams=[
            Upstream("openai", "/openai", "http://127.0.0.1:1"),
            Upstream("anthropic", "/anthropic", "http://x"),
        ],
        mediator=Mediator(built.engine, built.verifier),
        client=lambda: httpx.AsyncClient(transport=model),
    )
    return TestClient(create_app(config), base_url="http://127.0.0.1:5283")


def _masked_text(request: httpx.Request) -> str:
    return json.loads(request.content)["messages"][0]["content"]


CHAT = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
HEADERS = {"authorization": "Bearer sk-test"}


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_a_json_answer_larger_than_latet_reads_is_answered_with_a_clear_error(encoding):
    wire = _encoded(encoding, b'{"choices":[{"message":{"role":"assistant","content":"', repeat=40)

    def bomb(request):
        headers = {"content-type": "application/json", "content-encoding": encoding}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    with _gateway(bomb) as c:
        stopped = c.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert stopped.status_code == 502 and "larger than Latet reads" in stopped.json()["error"]["message"]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_event_stream_larger_than_latet_reads_ends_with_an_error_event(encoding):
    first = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
    wire = _encoded(encoding, first, b"data: ", repeat=40)

    def bomb(request):
        headers = {"content-type": "text/event-stream", "content-encoding": encoding}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    with _gateway(bomb) as c:
        streamed = c.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
    events = [block for block in streamed.text.split("\n\n") if block]
    assert streamed.status_code == 200 and len(events) == 2 and '"hi"' in events[0]
    assert json.loads(events[-1].removeprefix("data: "))["error"]["code"] == "latet_blocked"
    assert "larger than Latet reads" in events[-1]


class _Chunks(httpx.AsyncByteStream):
    """An answer streamed in these chunks, counting how many were read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.read = chunks, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk


def test_nothing_of_a_stopped_stream_is_read_or_passed_on_after_it_stopped(monkeypatch):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 2**20)
    first = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
    late = b'data: {"choices":[{"index":0,"delta":{"content":"late"}}]}\n\n'
    stream = _Chunks([first, b"data: " + b"a" * (2 * 2**20), late, late])  # an event that never ends, unencoded

    def answer(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    with _gateway(answer) as c:
        streamed = c.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
    assert '"hi"' in streamed.text and "larger than Latet reads" in streamed.text and '"late"' not in streamed.text
    assert stream.read == 2


def test_an_anthropic_event_stream_ends_with_its_own_error_event():
    wire = _encoded("gzip", b"event: message_start\ndata: {}\n\n", b"data: ", repeat=40)

    def bomb(request):
        headers = {"content-type": "text/event-stream", "content-encoding": "gzip"}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
    with _gateway(bomb) as c:
        streamed = c.post("/anthropic/v1/messages", json=body, headers={"x-api-key": "sk-test"})
    last = [block for block in streamed.text.split("\n\n") if block][-1]
    assert last.startswith("event: error\n") and json.loads(last.split("data: ", 1)[1])["type"] == "error"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_answer_under_the_bound_is_restored_whole_whatever_its_encoding(encoding):
    def echo(request):
        said = _masked_text(request)
        answer = {"choices": [{"index": 0, "message": {"role": "assistant", "content": f"you said: {said}"}}]}
        wire = _encoded(encoding, json.dumps(answer).encode())
        headers = {"content-type": "application/json", "content-encoding": encoding}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    with _gateway(echo) as c:
        answered = c.post("/openai/v1/chat/completions", json=CHAT, headers=HEADERS)
    assert answered.status_code == 200
    assert answered.json()["choices"][0]["message"]["content"] == f"you said: write to {EMAIL}"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_an_event_stream_under_the_bound_is_restored_whole_whatever_its_encoding(encoding):
    def echo(request):
        said = _masked_text(request)
        events = b"".join(
            b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": piece}}]}).encode() + b"\n\n"
            for piece in (said[:9], said[9:])
        )
        headers = {"content-type": "text/event-stream", "content-encoding": encoding}
        wire = _encoded(encoding, events + b"data: [DONE]\n\n")
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))

    with _gateway(echo) as c:
        streamed = c.post("/openai/v1/chat/completions", json={**CHAT, "stream": True}, headers=HEADERS)
    text = "".join(
        json.loads(block[6:])["choices"][0]["delta"].get("content", "")
        for block in streamed.text.split("\n\n")
        if block.startswith("data: {")
    )
    assert streamed.status_code == 200 and text == f"write to {EMAIL}"


# --- one identity's answers and every other's -----------------------------------------------------------------------


def _identity(values: int = 200):
    """An identity's mediator and conversation, the conversation shown values of its vault (what its answers restore),
    and a text with fakes in it, as the provider was sent it."""
    built = build(Config(identity=IdentityConfig(enabled=False)), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    session = built.engine.session("conv:answers")
    text = " ".join(f"ops{n}@acme-intra.it" for n in range(values))
    return mediator, session, mediator.mask_value(text, SlotKind.USER_TEXT, session, MaskReport()).text


def _tiny_events(said: str, count: int) -> bytes:
    """A stream of ``count`` tiny chat events, each saying ``said`` again, in gzip: what a provider an identity runs can
    send within the bound decoded."""
    event = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": said}}]}) + "\n\n").encode()
    return gzip.compress(event * count + b"data: [DONE]\n\n", 6)


async def _relayed(transform, wire: bytes) -> bytes:
    """An answer as the forwarder relays it: 64 KiB of its wire at a time, what each decodes to restored and passed
    on, the event loop given a turn after each piece."""
    out = []
    for at in range(0, len(wire), 64 * 2**10):
        async for piece in transform.restored(wire[at : at + 64 * 2**10]):
            out.append(piece)
            await asyncio.sleep(0)
    out.append(await transform.restored_end())
    return b"".join(out)


async def test_one_identity_restoring_many_answers_leaves_another_identitys_small_answer_fast(monkeypatch):
    """An identity's answers are restored a bounded step at a time, one of its answers at a time on the event loop, the
    others waiting their turn: however many streams of tiny events it has, another identity's small answer waits for a
    step or two, never for a step of each of its streams (16 such streams held every other identity seconds)."""
    steps: list[tuple[str, int]] = []
    restore = SSETransform._events

    def counted(self, events):
        steps.append(("heavy" if self.mediator is heavy else "light", len(events)))
        return restore(self, events)

    monkeypatch.setattr(SSETransform, "_events", counted)
    heavy, session, said = _identity()
    light, light_session, light_said = _identity(values=5)
    wire = _tiny_events(said[:20], 20_000)  # events of a few dozen bytes: each piece restored on the loop
    streams = [heavy.response_transform(Prepared(REWRITE, "chat", b"", session), "text/event-stream", "gzip")
               for _ in range(8)]  # fmt: skip
    running = [asyncio.ensure_future(_relayed(stream, wire)) for stream in streams]
    try:
        await asyncio.sleep(0.05)  # its answers are being restored
        small = light.response_transform(Prepared(REWRITE, "chat", b"", light_session), "text/event-stream", "gzip")
        first = len(steps)
        started = time.perf_counter()
        answer = await _relayed(small, _tiny_events(light_said[:30], 3))
        took = time.perf_counter() - started
        meanwhile = [who for who, _ in steps[first:]].count("heavy")
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
    assert b"[DONE]" in answer and not any(stream.stopped for stream in streams)
    assert max(count for who, count in steps if who == "heavy") <= RESTORED_EVENTS_AT_ONCE  # a bounded step
    assert meanwhile <= 4  # a step or two of the heavy identity's, not one of each of its eight streams
    assert took < 0.25


def test_an_answer_of_more_events_than_latet_reads_is_stopped_with_a_clear_error(monkeypatch):
    """Past the events a stream carries at most, it ends with an error in the shape the client reads, nothing more
    passed on (fail closed): a long answer has thousands of events, a provider sending more holds the loop."""
    monkeypatch.setattr(sse_module, "MAX_EVENTS", 1_000)
    mediator, session, said = _identity(values=5)
    transform = mediator.response_transform(Prepared(REWRITE, "chat", b"", session), "text/event-stream", "gzip")
    out = transform.feed(_tiny_events(said[:30], 1_500)) + transform.end()
    assert transform.stopped == "TooManyEvents" and b"latet_blocked" in out and b"[DONE]" not in out
    assert out.count(b"data: ") <= 1_001  # at most the events read, and the error
    assert transform.feed(_tiny_events(said[:30], 2)) == b"" and transform.end() == b""


class _Answering:
    """A WebSocket upstream on 127.0.0.1 that answers each frame it is sent with ``deltas`` frames and, unless
    ``ends`` is 0, writes a turn's end every ``ends`` frames as well as after them: how many it sent, and whether its
    connection ended."""

    def __init__(self, deltas: int, ends: int = 0) -> None:
        self.deltas, self.ends, self.sent, self.closed = deltas, ends, 0, threading.Event()
        self.server = serve(self._answer, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _answer(self, conn) -> None:
        done = json.dumps({"type": "response.completed", "response": {"status": "completed"}})
        try:
            for _ in conn:
                for n in range(1, self.deltas + 1):
                    conn.send(json.dumps({"type": "response.output_text.delta", "delta": "x"}))
                    self.sent += 1
                    if self.ends and n % self.ends == 0:
                        conn.send(done)
                conn.send(done)
        except ConnectionClosed:
            pass
        finally:
            self.closed.set()

    def close(self) -> None:
        self.server.shutdown()


def _relay_to(upstream: _Answering):
    """A computer's relay to ``upstream``, and a client's socket on it."""
    config = GatewayConfig(port=5283, upstreams=[Upstream("relay", "/relay", f"http://127.0.0.1:{upstream.port}")])
    client = TestClient(create_app(config), base_url="http://127.0.0.1:5283")
    return client, lambda: client.websocket_connect("/relay/v1/responses", headers={"host": "127.0.0.1:5283"})


_ASK = json.dumps({"type": "response.create", "response": {"input": "hi"}})


def test_a_websocket_turn_of_more_frames_than_a_stream_has_events_is_stopped_with_a_clear_error(monkeypatch):
    """Each frame an upstream sends is an event restored on the event loop: past a stream's events, what answers one
    frame of the client's is stopped with an error frame and the socket closes (fail closed), whatever ends of a turn
    the upstream writes among the frames."""
    monkeypatch.setattr(sse_module, "MAX_EVENTS", 100)
    for ends in (0, 40):  # no end of a turn among them, and one every 40 frames
        upstream = _Answering(deltas=50_000, ends=ends)
        client, connect = _relay_to(upstream)
        read: list[dict] = []
        try:
            with client, connect() as socket:
                socket.send_text(_ASK)
                while len(read) < 300 and (not read or read[-1]["type"] != "error"):  # 300: far past the cap
                    read.append(json.loads(socket.receive_text()))
                with pytest.raises(WebSocketDisconnect) as closed:  # then it closed: nothing more passed on
                    socket.receive_text()
                assert closed.value.code == 1008  # a policy violation: the upstream's
            assert upstream.closed.wait(5)
        finally:
            upstream.close()
        deltas = [f for f in read if f["type"] == "response.output_text.delta"]
        assert len(deltas) <= 100 and read[-1]["type"] == "error", read[-3:]
        assert read[-1]["error"]["type"] == "latet_blocked"
        assert "more events than Latet reads (100)" in read[-1]["error"]["message"]


def test_websocket_turns_each_under_a_streams_events_pass_whole(monkeypatch):
    monkeypatch.setattr(sse_module, "MAX_EVENTS", 100)
    upstream = _Answering(deltas=80)
    client, connect = _relay_to(upstream)
    read: list[str] = []
    try:
        with client, connect() as socket:
            for _ in range(3):  # 240 frames on one socket, 80 for each frame of the client's
                socket.send_text(_ASK)
                while (kind := json.loads(socket.receive_text())["type"]) not in ("response.completed", "error"):
                    read.append(kind)
                read.append(kind)
    finally:
        upstream.close()
    assert read.count("response.output_text.delta") == 240 and read.count("response.completed") == 3
    assert "error" not in read
