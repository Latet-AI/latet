"""Bedrock's streamed answers (AWS event-stream frames): read for the dashboard, never changed."""

import base64
import json
from pathlib import Path

import pytest

from latet.gateway.recorder import ResponseCapture
from latet.protocols.eventstream import EventStreamDecoder, EventStreamError, encode

SAMPLE = (Path(__file__).parents[1] / "data" / "eventstream" / "claude_stream.bin").read_bytes()
EVENTSTREAM = "application/vnd.amazon.eventstream"


def _chunk(event: dict) -> bytes:
    payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode()
    return encode({":event-type": "chunk", ":content-type": "application/json", ":message-type": "event"}, payload)


def _events(messages) -> list[dict]:
    return [json.loads(base64.b64decode(json.loads(m.payload)["bytes"])) for m in messages]


def test_a_bedrock_answer_reads_frame_by_frame():
    messages = EventStreamDecoder().feed(SAMPLE)
    assert len(messages) == 6 and {m.headers[":event-type"] for m in messages} == {"chunk"}
    deltas = [e["delta"]["text"] for e in _events(messages) if e["type"] == "content_block_delta"]
    assert deltas == ["Hello from Bedrock"]


def test_frames_split_anywhere_read_the_same():
    decoder = EventStreamDecoder()
    messages = [m for i in range(len(SAMPLE)) for m in decoder.feed(SAMPLE[i : i + 1])]
    assert _events(messages) == _events(EventStreamDecoder().feed(SAMPLE))


def test_a_damaged_frame_is_refused():
    damaged = bytearray(SAMPLE)
    damaged[40] ^= 0xFF
    with pytest.raises(EventStreamError):
        EventStreamDecoder().feed(bytes(damaged))


def test_the_dashboard_reads_text_and_usage_of_a_bedrock_answer():
    capture = ResponseCapture("anthropic", EVENTSTREAM)
    for i in range(0, len(SAMPLE), 7):
        capture.feed(SAMPLE[i : i + 7])
    capture.end()
    capture.settle()  # what the recorder reads, in its thread
    assert "".join(capture.text) == "Hello from Bedrock"
    assert capture.done and not capture.failed and not capture.broken
    assert capture.usage


def test_the_dashboard_reads_a_tool_call_of_a_bedrock_answer():
    stream = b"".join(
        _chunk(e)
        for e in (
            {"type": "message_start", "message": {"id": "m", "content": [], "usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "Write"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
            {"type": "message_stop"},
        )
    )
    capture = ResponseCapture("anthropic", EVENTSTREAM)
    capture.feed(stream)
    capture.end()
    capture.settle()  # what the recorder reads, in its thread
    assert [(t["name"], t["args"]) for t in capture.tools.values()] == [("Write", '{"a":1}')]


def test_a_damaged_answer_stops_the_record_only():
    damaged = bytearray(SAMPLE)
    damaged[-1] ^= 0xFF
    capture = ResponseCapture("anthropic", EVENTSTREAM)
    capture.feed(bytes(damaged))
    capture.end()
    capture.settle()  # what the recorder reads, in its thread
    assert capture.broken


def test_an_exception_frame_is_a_failed_answer():
    frame = encode(
        {":message-type": "exception", ":exception-type": "throttlingException", ":content-type": "application/json"},
        b'{"message": "Too many requests"}',
    )
    capture = ResponseCapture("anthropic", EVENTSTREAM)
    capture.feed(frame)
    capture.end()
    capture.settle()  # what the recorder reads, in its thread
    assert capture.done and capture.failed
