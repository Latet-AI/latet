"""The tutorial's pretend model: it answers each request with the step's next scripted turn,
streamed as Anthropic streams (small pieces, so a fake is split across events), fills in what it received
through the chapter's patterns, keeps every body, and says which turns were never used."""

import json

import pytest

from latet.gateway.routes import Upstream
from latet.tutorial.chapters import Capture, Turn
from latet.tutorial.model import MODEL_ID, PRETEND_URL, PretendModel, PretendUpstream

URL = f"{PRETEND_URL}/v1/messages"
KEY = "3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN"  # a fake, as the model gets it


def _turn(**fields) -> Turn:
    return Turn.model_validate(fields)


def _capture(pattern: str, where: str = "any") -> Capture:
    return Capture.model_validate({"pattern": pattern, "in": where})


def _events(text: str) -> list[dict]:
    events = []
    for chunk in text.split("\n\n"):
        lines = chunk.split("\n")
        if not chunk.strip():
            continue
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        event = json.loads(lines[1][6:])
        assert lines[0] == f"event: {event['type']}"
        events.append(event)
    return events


def _body(*messages: dict) -> dict:
    return {"model": MODEL_ID, "max_tokens": 1024, "stream": True, "messages": list(messages)}


READ = {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {}}]}


def _result(text: str) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": text}]}


async def _post(model: PretendModel, body: dict):
    async with model.client() as client:
        return await client.post(URL, json=body)


def test_the_sandbox_reaches_the_pretend_model_only_and_never_over_a_websocket():
    upstream = PretendUpstream()
    assert (upstream.name, upstream.prefix) == ("anthropic", "/anthropic")
    assert upstream.target("/anthropic/v1/messages", "") == "http://pretend-model.invalid/v1/messages"
    assert upstream.websocket is False and Upstream.websocket is True


async def test_an_answer_streams_as_anthropic_streams_it():
    model = PretendModel()
    write = {"file_path": "config.local.json", "content": '{"api_key": "x"}'}
    model.expect(
        [_turn(id="write", text="Got the key. Writing the config.", tool={"name": "Write", "input": write})], {}
    )
    response = await _post(model, _body({"role": "user", "content": "hi"}))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["message_start", "ping"] and kinds[-2:] == ["message_delta", "message_stop"]
    assert events[0]["message"]["model"] == MODEL_ID and events[0]["message"]["id"] == "msg_write"
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    text = [e["delta"]["text"] for e in events if e["type"] == "content_block_delta" and e["index"] == 0]
    assert "".join(text) == "Got the key. Writing the config." and max(len(t) for t in text) <= 4
    [start] = [e for e in events if e["type"] == "content_block_start" and e["index"] == 1]
    assert start["content_block"] == {"type": "tool_use", "id": "toolu_write", "name": "Write", "input": {}}
    pieces = [e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta" and e["index"] == 1]
    assert json.loads("".join(pieces)) == write and len(pieces) > 1 and max(len(p) for p in pieces) <= 7
    assert [e["index"] for e in events if e["type"] == "content_block_stop"] == [0, 1]


async def test_a_text_alone_ends_the_turn():
    model = PretendModel()
    model.expect([_turn(id="done", text="Done. I made no mistakes. Probably.")], {})
    events = _events((await _post(model, _body({"role": "user", "content": "hi"}))).text)
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    assert [e["content_block"]["type"] for e in events if e["type"] == "content_block_start"] == ["text"]


async def test_an_answer_uses_what_the_model_received_split_across_events():
    model = PretendModel()
    captures = {"KEY": _capture(r"DEMO_API_KEY=(\S+)", "tool_result"), "EMAIL": _capture(r"(\S+@\S+)", "user")}
    model.expect([_turn(id="noted", text="I wrote down {{received:KEY}} and {{received:EMAIL}}.")], captures)
    body = _body({"role": "user", "content": "mail a.b@c.example now"}, READ, _result(f"DEMO_API_KEY={KEY}\n"))
    events = _events((await _post(model, body)).text)
    text = [e["delta"]["text"] for e in events if e["type"] == "content_block_delta"]
    assert "".join(text) == f"I wrote down {KEY} and a.b@c.example."
    assert not any(KEY in piece for piece in text)  # never in one piece
    assert model.captured == {"KEY": KEY, "EMAIL": "a.b@c.example"}


async def test_a_pattern_looks_only_where_it_is_told_and_at_the_latest_text_first():
    model = PretendModel()
    captures = {"KEY": _capture(r"KEY=(\w+)", "tool_result"), "ANY": _capture(r"KEY=(\w+)")}
    model.expect([_turn(id="a", text="{{received:KEY}} {{received:ANY}}")], captures)
    body = _body(_result("KEY=old"), {"role": "assistant", "content": "ok"}, {"role": "user", "content": "KEY=typed"})
    events = _events((await _post(model, body)).text)
    assert "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta") == "old typed"


async def test_a_value_the_model_never_received_is_an_error_and_the_turn_stays():
    model = PretendModel()
    model.expect([_turn(id="noted", text="I wrote down {{received:KEY}}.")], {"KEY": _capture(r"KEY=(\w+)")})
    response = await _post(model, _body({"role": "user", "content": "nothing here"}))
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Tutorial Model never received KEY: nothing matched its pattern"
    assert model.leftover() == ["noted"] and model.received[-1].turn is None


async def test_a_group_that_found_nothing_is_not_what_the_model_received():
    """A pattern can match with its group empty or left out: that is nothing received, never an empty text."""
    model = PretendModel()
    captures = {"KEY": _capture(r"KEY=(\w+)?"), "NOTE": _capture(r"note:(\w*)")}
    model.expect([_turn(id="noted", text="I wrote down {{received:KEY}}.")], captures)
    response = await _post(model, _body({"role": "user", "content": "KEY= is empty, note: too"}))
    assert response.status_code == 500 and "never received KEY" in response.json()["error"]["message"]
    assert model.captured == {}
    # A later match whose group holds something is the one found.
    model.expect([_turn(id="noted", text="I wrote down {{received:KEY}}.")], captures)
    events = _events((await _post(model, _body({"role": "user", "content": "KEY= then KEY=abc1"}))).text)
    assert "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta") == "I wrote down abc1."


async def test_every_body_is_kept_with_the_turn_that_answered_it():
    model = PretendModel()
    model.expect([_turn(id="one", text="One."), _turn(id="two", text="Two.")], {})
    first, second = _body({"role": "user", "content": "1"}), _body({"role": "user", "content": "2"})
    await _post(model, first)
    await _post(model, second)
    assert [(json.loads(r.body), r.turn) for r in model.received] == [(first, "one"), (second, "two")]
    assert model.received[0].text == model.received[0].body.decode()
    assert model.received[1].answer == [{"type": "text", "text": "Two."}]


async def test_the_text_of_each_turn_is_kept_as_the_model_sent_it():
    """By turn, what the model said, with what it received in place: a check reads it back, restored. A turn with no
    text said nothing; reset forgets them."""
    model = PretendModel()
    read = _turn(id="read", tool={"name": "Read", "input": {"file_path": ".env"}})
    model.expect([read, _turn(id="noted", text="I wrote down {{received:KEY}}.")], {"KEY": _capture(r"KEY=(\w+)")})
    await _post(model, _body({"role": "user", "content": "KEY=abc1"}))
    assert model.served == {}
    await _post(model, _body({"role": "user", "content": "KEY=abc1"}))
    assert model.served == {"noted": "I wrote down abc1."}
    model.reset()
    assert model.served == {}


async def test_turns_never_used_are_reported_until_the_model_is_reset():
    model = PretendModel()
    model.expect([_turn(id="a", text="A."), _turn(id="b", text="B.")], {})
    await _post(model, _body({"role": "user", "content": "1"}))
    assert model.queued() == ["b"] and model.leftover() == ["b"]
    model.expect([_turn(id="c", text="C.")], {})  # the next step: b was never used
    assert model.queued() == ["c"] and model.leftover() == ["b", "c"]
    model.reset()
    assert (model.queued(), model.leftover(), model.received, model.captured) == ([], [], [], {})


async def test_with_no_turn_left_the_model_says_so():
    model = PretendModel()
    response = await _post(model, _body({"role": "user", "content": "hi"}))
    assert response.status_code == 500 and "nothing more to say" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/v1/messages"), ("POST", "/v1/models"), ("POST", "/v1/messages/count_tokens"), ("GET", "/")],
)
async def test_anything_but_posting_a_message_is_not_found(method, path):
    model = PretendModel()
    model.expect([_turn(id="a", text="A.")], {})
    async with model.client() as client:
        response = await client.request(method, PRETEND_URL + path)
    assert response.status_code == 404 and response.json()["type"] == "error"
    assert model.received == [] and model.queued() == ["a"]
