"""What the model wrote in an answer Latet passes on as it came (a request it only checks, Claude Code's): each text
block and each string of each tool call's input, read as the answer passes, each once its block has ended, in
Anthropic's stream, Bedrock's event-stream and a whole body, encoded or not. Past what is read of an answer, nothing
more is taken."""

import base64
import gzip
import json

import pytest

from latet.gateway import written
from latet.gateway.written import Written, strings
from latet.protocols.eventstream import encode

TEXT = 'Set TEST_PASSWORD = "hunter2" in the test.'
EDIT = {"file_path": "tests/test_login.py", "old_string": "import os\n", "new_string": 'TEST_PASSWORD = "hunter2"\n'}


def _events() -> list[dict]:
    """An answer of Anthropic's, as its stream says it: a text in three pieces, then an Edit whose input comes cut."""
    raw = json.dumps(EDIT)
    return [
        {"type": "message_start", "message": {"id": "msg_1", "content": [], "usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": TEXT[:7]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": TEXT[7:30]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": TEXT[30:]}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[:20]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[20:]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
        {"type": "message_stop"},
    ]


def _sse(events: list[dict]) -> list[bytes]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events]


def _read(capture: Written, chunks: list[bytes]) -> list[list[str]]:
    """What each chunk ends, then what the answer's end does."""
    out = [[text for step in capture.steps(chunk) for text in step] for chunk in chunks]
    return [*out, [text for step in capture.steps(None) for text in step]]


WRITTEN = [TEXT, EDIT["file_path"], EDIT["old_string"], EDIT["new_string"]]


def test_a_stream_ends_each_text_with_its_block():
    """A text is taken once its block has ended, never before; a tool call's input, each of its strings, at its end."""
    ended = _read(Written("text/event-stream", None), _sse(_events()))
    assert ended[5] == [TEXT]
    assert ended[9] == [EDIT["file_path"], EDIT["old_string"], EDIT["new_string"]]
    assert [i for i, texts in enumerate(ended) if texts] == [5, 9]


def test_a_stream_cut_anywhere_ends_the_same_texts():
    whole = b"".join(_sse(_events()))
    ended = _read(Written("text/event-stream; charset=utf-8", None), [whole[i : i + 1] for i in range(len(whole))])
    assert [text for texts in ended for text in texts] == WRITTEN


def test_a_compressed_stream_ends_the_same_texts():
    whole = gzip.compress(b"".join(_sse(_events())))
    ended = _read(Written("text/event-stream", "gzip"), [whole[i : i + 50] for i in range(0, len(whole), 50)])
    assert [text for texts in ended for text in texts] == WRITTEN


def test_bedrocks_event_stream_ends_the_same_texts():
    def frame(event: dict) -> bytes:
        payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode()
        return encode({":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"}, payload)

    ended = _read(Written("application/vnd.amazon.eventstream", None), [frame(e) for e in _events()])
    assert ended[5] == [TEXT] and [text for texts in ended for text in texts] == WRITTEN


def test_a_whole_answer_ends_its_texts_at_its_end():
    answer = {
        "content": [{"type": "text", "text": TEXT}, {"type": "tool_use", "id": "t1", "name": "Edit", "input": EDIT}]
    }
    raw = json.dumps(answer).encode()
    ended = _read(Written("application/json", None), [raw[:30], raw[30:]])
    assert ended == [[], [], WRITTEN]


def test_a_tool_call_with_no_input_given_in_pieces_keeps_the_one_it_began_with():
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t", "name": "Read", "input": {"path": "a.txt"}},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    assert [text for texts in _read(Written("text/event-stream", None), _sse(events)) for text in texts] == ["a.txt"]


def test_what_is_no_text_nor_tool_call_is_not_taken():
    """Thinking, a server's tool, a citation: none is a text the request carries back as a text of the model's."""
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"q": "x"}'},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    assert not any(_read(Written("text/event-stream", None), _sse(events)))


def test_past_what_is_read_of_an_answer_nothing_more_is_taken(monkeypatch):
    """An answer larger than is read of it: what ended before is taken, nothing after, and what the model wrote there is
    checked as a client's text is."""
    chunks = _sse(_events())
    monkeypatch.setattr(written, "WRITTEN_BYTES", len(b"".join(chunks[:6])) + 10)  # the text's block, not the Edit's
    capture = Written("text/event-stream", None)
    ended = _read(capture, chunks)
    assert ended[5] == [TEXT] and not any(ended[6:]) and capture.broken


def test_past_the_events_read_of_bedrocks_stream_nothing_more_is_taken(monkeypatch):
    def frame(event: dict) -> bytes:
        payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode()
        return encode({":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"}, payload)

    monkeypatch.setattr(written, "WRITTEN_EVENTS", 7)  # the text's block, not the Edit's
    capture = Written("application/vnd.amazon.eventstream", None)
    ended = _read(capture, [frame(e) for e in _events()])
    assert ended[5] == [TEXT] and not any(ended[6:]) and capture.broken


@pytest.mark.parametrize(
    ("content_type", "encoding"),
    [("text/plain", None), ("text/event-stream", "compress"), ("application/octet-stream", None)],
)
def test_an_answer_that_is_not_read_ends_nothing(content_type, encoding):
    capture = Written(content_type, encoding)
    assert capture.broken and not any(_read(capture, _sse(_events())))


def test_once_broken_an_answer_is_read_no_further_whatever_follows():
    """Once Latet stopped reading an answer, it spends nothing more on it: what follows is not even decoded."""
    capture = Written("text/event-stream", None)
    capture.broken = True
    assert not any(_read(capture, _sse(_events())))
    assert capture._size == 0


def test_an_answer_that_does_not_decode_ends_nothing_more():
    capture = Written("text/event-stream", "gzip")
    assert not any(_read(capture, [b"not gzip at all", *_sse(_events())])) and capture.broken


async def test_what_a_large_tool_call_wrote_is_recorded_in_slices_the_loop_served_between():
    """A tool call whose input holds thousands of short strings ends them all at once: they are recorded a slice at a
    time, each string once, and the event loop serves the others between two slices."""
    import asyncio
    import itertools

    from latet.config import Config
    from latet.engine.build import build
    from latet.engine.vault import Vault
    from latet.gateway import mediate
    from latet.gateway.mediate import Mediator

    from ..conftest import ROOT_KEY

    built = build(Config(), Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    slices: list[tuple[int, int]] = []  # each slice's size, and the turns the other had taken when it came
    wrote = built.verifier.wrote
    built.verifier.wrote = lambda session, texts: (slices.append((len(texts), turns)), wrote(session, texts))[1]  # type: ignore[method-assign]
    items = [f"v{i % 3000}" for i in range(4500)]  # 3,000 strings, a half of them twice
    raw = json.dumps({"items": items})
    events = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t", "name": "W", "input": {}},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw}},
        {"type": "content_block_stop", "index": 0},
    ]
    turns = 0
    done = False

    async def other() -> None:  # another answer's work, on the same loop
        nonlocal turns
        while not done:
            turns += 1
            await asyncio.sleep(0)

    beside = asyncio.create_task(other())
    capture = Written("text/event-stream", None)
    session = mediator.engine.session("cc:s-1")
    for chunk in _sse(events):
        await mediator.read_written(capture, session, chunk)
    done = True
    await beside
    sizes = [size for size, _ in slices]
    assert sum(sizes) == 3000 and max(sizes) <= mediate.WRITTEN_AT_ONCE and len(sizes) >= 3
    assert all(a[1] < b[1] for a, b in itertools.pairwise(slices))  # the other served between two
    assert built.verifier.written("cc:s-1", "v2999") and not built.verifier.written("cc:s-1", "v3000")


def test_strings_are_each_string_of_a_json_value_in_their_order():
    assert strings({"a": "x", "b": [1, "y", {"c": "z"}], "d": None, "e": True}) == ["x", "y", "z"]
