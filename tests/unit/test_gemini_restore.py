"""Gemini's answers restored: whole, as an array, and streamed event by event, a stand-in cut between two events coming
back whole, and every event one that Gemini CLI and Google's JS SDK accept (no empty part, no empty parts, no event
without candidates, a signed part never merged with another)."""

import json

import pytest

from latet.gateway.mediate import JSONTransform, SSETransform
from latet.protocols.rewrite import GeminiStream, restore_gemini_answer
from latet.protocols.sse import SSEParser, serialize

from .test_gemini_mediate import CALL, GEMINI, KEY, _request, _user
from .test_protocol_buffering import FAKE, ORIGINAL, _context
from .test_what_leaves import EMAIL, _mediator

HELD = FAKE[:6]  # "masked": the start of a stand-in, held back until the rest comes
USAGE = {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5}


def _event(*parts, finish=None, index=0, **extra) -> dict:
    candidate = {"index": index, "content": {"role": "model", "parts": list(parts)}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate], **extra}


def _run(stream: GeminiStream, events: list[dict]) -> list[dict]:
    out: list[dict] = []
    for event in events:
        result = stream.on_event(event)
        out.extend([event] if result is None else result)
    return out + stream.on_end()


def _text(events: list[dict], index: int = 0) -> str:
    return "".join(
        part.get("text", "")
        for event in events
        for candidate in event["candidates"]
        if candidate.get("index", 0) == index
        for part in candidate.get("content", {}).get("parts", [])
        if not part.get("thought")
    )


def _accepted(events: list[dict]) -> None:
    """What Gemini CLI and js-genai accept: candidates in every event, parts in every candidate, no empty part."""
    for event in events:
        assert event.get("candidates"), event
        for candidate in event["candidates"]:
            parts = candidate["content"]["parts"]
            assert parts, event
            for part in parts:
                assert part and part != {"text": ""}, event


# --- whole answers -----------------------------------------------------------------------------------------------------


def test_a_whole_answer_is_restored_and_remembered():
    remembered = []
    ctx = _context(lambda masked, restored: remembered.append((masked, restored)))
    answer = _event(
        {"text": "thinking of " + FAKE, "thought": True},
        {"text": "write to " + FAKE},
        {"functionCall": {"name": "send", "args": {FAKE: "key", "to": FAKE}}},
        finish="STOP",
    )
    restored = restore_gemini_answer(ctx, answer)
    parts = restored["candidates"][0]["content"]["parts"]
    assert parts[0]["text"] == "thinking of " + FAKE  # thoughts are left as the model wrote them
    assert parts[1]["text"] == "write to " + ORIGINAL
    assert parts[2]["functionCall"]["args"] == {ORIGINAL: "key", "to": ORIGINAL}
    assert ("write to " + FAKE, "write to " + ORIGINAL) in remembered
    assert (FAKE, ORIGINAL) in remembered
    assert answer["candidates"][0]["content"]["parts"][1]["text"] == "write to " + FAKE  # a copy: the answer unchanged


def test_a_streamed_array_answer_is_restored_as_a_stream_is():
    answers = [_event({"text": "to " + FAKE[:7]}), _event({"text": FAKE[7:] + " and " + FAKE}, finish="STOP")]
    restored = restore_gemini_answer(_context(), answers)
    assert isinstance(restored, list) and _text(restored) == f"to {ORIGINAL} and {ORIGINAL}"
    _accepted(restored)


def test_a_whole_answer_passes_through_the_json_transform():
    transform = JSONTransform("gemini", _context(), None)
    transform.feed(json.dumps([_event({"text": FAKE}, finish="STOP")]).encode())
    assert _text(json.loads(transform.end())) == ORIGINAL


# --- streams -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cut", range(1, len(FAKE)))
def test_a_stand_in_cut_between_events_comes_back_whole(cut):
    events = [_event({"text": "to " + FAKE[:cut]}), _event({"text": FAKE[cut:] + " ok"}, finish="STOP")]
    out = _run(GeminiStream(_context()), events)
    assert _text(out) == f"to {ORIGINAL} ok"
    _accepted(out)


def test_a_stand_in_cut_in_three_comes_back_whole():
    events = [
        _event({"text": "to " + FAKE[:4]}),
        _event({"text": FAKE[4:9]}),
        _event({"text": FAKE[9:]}, finish="STOP"),
    ]
    out = _run(GeminiStream(_context()), events)
    assert _text(out) == "to " + ORIGINAL
    _accepted(out)


def test_held_text_goes_out_before_a_function_call():
    events = [
        _event({"text": "sending to " + HELD}),
        _event({"functionCall": {"name": "send", "args": {"to": FAKE}}}, finish="STOP"),
    ]
    out = _run(GeminiStream(_context()), events)
    parts = out[1]["candidates"][0]["content"]["parts"]
    assert parts == [{"text": HELD}, {"functionCall": {"name": "send", "args": {"to": ORIGINAL}}}]
    _accepted(out)


def test_a_function_calls_arguments_are_remembered():
    remembered = []
    stream = GeminiStream(_context(lambda masked, restored: remembered.append((masked, restored))))
    _run(stream, [_event({"functionCall": {"name": "send", "args": {"to": FAKE}}}, finish="STOP")])
    assert (FAKE, ORIGINAL) in remembered


def test_a_signed_empty_part_stays_as_google_sent_it():
    events = [_event({"text": "to " + HELD}), _event({"text": "", "thoughtSignature": "c2ln"}, finish="STOP")]
    out = _run(GeminiStream(_context()), events)
    assert out[1]["candidates"][0]["content"]["parts"] == [{"text": HELD}, {"text": "", "thoughtSignature": "c2ln"}]


def test_a_signed_text_part_is_restored_whole_in_its_own_place():
    events = [
        _event({"text": "to " + HELD}),
        _event({"text": FAKE[6:] + " and " + FAKE, "thoughtSignature": "c2ln"}, finish="STOP"),
    ]
    out = _run(GeminiStream(_context()), events)
    assert out[1]["candidates"][0]["content"]["parts"] == [
        {"text": HELD},
        {"text": FAKE[6:] + " and " + ORIGINAL, "thoughtSignature": "c2ln"},
    ]


def test_a_thought_is_passed_as_it_came_and_held_text_goes_before_it():
    events = [_event({"text": "to " + HELD}), _event({"text": "about " + FAKE, "thought": True}), _event(finish="STOP")]
    out = _run(GeminiStream(_context()), events)
    assert out[1]["candidates"][0]["content"]["parts"] == [{"text": HELD}, {"text": "about " + FAKE, "thought": True}]


def test_the_finish_reason_flushes():
    out = _run(GeminiStream(_context()), [_event({"text": "to " + HELD}, finish="STOP")])
    assert len(out) == 1 and _text(out) == "to " + HELD
    _accepted(out)


def test_an_event_held_whole_is_not_sent():
    stream = GeminiStream(_context())
    held = _event({"text": HELD}, usageMetadata=USAGE, modelVersion="gemini-3-pro", responseId="r1")
    held["candidates"][0]["safetyRatings"] = []
    assert stream.on_event(held) == []
    out = stream.on_event(_event({"text": FAKE[6:]}, finish="STOP", usageMetadata=USAGE))
    assert out is not None and _text(out) == ORIGINAL


def test_an_event_that_carries_more_is_sent_never_empty():
    stream = GeminiStream(_context())
    out = stream.on_event(_event({"text": HELD}, promptFeedback={"safetyRatings": []}))
    assert out is not None and out[0]["promptFeedback"] == {"safetyRatings": []}
    _accepted(out)
    assert _text(out) == HELD


def test_the_end_of_a_stream_sends_what_was_held():
    stream = GeminiStream(_context())
    assert _text(stream.on_event(_event({"text": "to " + HELD})) or []) == "to "
    assert stream.on_end() == [{"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": HELD}]}}]}]


def test_candidates_have_channels_of_their_own():
    events = [
        {
            "candidates": [
                {"index": 0, "content": {"role": "model", "parts": [{"text": "a " + FAKE[:5]}]}},
                {"index": 1, "content": {"role": "model", "parts": [{"text": "b " + FAKE[:8]}]}},
            ]
        },
        {
            "candidates": [
                {"index": 1, "content": {"role": "model", "parts": [{"text": FAKE[8:]}]}, "finishReason": "STOP"},
                {"index": 0, "content": {"role": "model", "parts": [{"text": FAKE[5:]}]}, "finishReason": "STOP"},
            ]
        },
    ]
    out = _run(GeminiStream(_context()), events)
    assert _text(out, 0) == "a " + ORIGINAL and _text(out, 1) == "b " + ORIGINAL
    _accepted(out)


def test_an_event_with_no_candidates_passes_as_it_came():
    stream = GeminiStream(_context())
    assert stream.on_event({"promptFeedback": {"blockReason": "SAFETY"}}) is None


# --- through the gateway's transform -----------------------------------------------------------------------------------


def _sse(events: list[dict]) -> bytes:
    return b"".join(serialize(None, json.dumps(event), b"\r\n") for event in events)


def _parsed(wire: bytes) -> list[dict]:
    return [event.json() for event in SSEParser(limit=2**20).feed(wire)]


def test_a_conversation_sent_back_leaves_as_the_model_wrote_it():
    mediator = _mediator()
    first = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"write to {EMAIL}")))
    assert first.session is not None
    fake = json.loads(first.body)["contents"][0]["parts"][0]["text"].removeprefix("write to ")
    written = f"Dear {fake}, hello"
    transform = SSETransform("gemini", mediator.restore_context(first.session), None, lambda _: None)
    wire = transform.feed(_sse([_event({"text": written[:9]}), _event({"text": written[9:]}, finish="STOP")]))
    restored = _text(_parsed(wire + transform.end()))
    assert restored == f"Dear {EMAIL}, hello"
    turn = {"role": "model", "parts": [{"text": restored}]}
    again = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"write to {EMAIL}"), turn, _user("thanks")))
    assert json.loads(again.body)["contents"][1]["parts"][0]["text"] == written


def test_the_events_keep_their_line_ends():
    transform = SSETransform("gemini", _context(), None, lambda _: None)
    wire = transform.feed(_sse([_event({"text": "to " + FAKE}, finish="STOP")])) + transform.end()
    assert wire.endswith(b"\r\n\r\n") and ORIGINAL.encode() in wire


def test_a_stopped_stream_ends_in_googles_shape(monkeypatch):
    monkeypatch.setattr("latet.protocols.sse.MAX_DECODED", 64)
    transform = SSETransform("gemini", _context(), None, lambda _: None)
    wire = transform.feed(b'data: {"candidates": [' + b"x" * 200)  # an event that never ends within what Latet reads
    [stopped] = _parsed(wire)
    assert stopped["error"]["code"] == 500 and stopped["error"]["status"] == "INTERNAL"
    assert stopped["error"]["message"].startswith("Latet")
    assert wire.startswith(b'data: {"error":')  # Google's Python SDK knows an error by its first key


# --- Vertex's streamed function-call arguments -------------------------------------------------------------------------


def _piece(path: str, value: str | None = None, more: bool = True, **other) -> dict:
    piece = {"jsonPath": path, **({"stringValue": value} if value is not None else {}), **other}
    return {**piece, "willContinue": True} if more else piece


def _call(*pieces, more: bool = True, **call) -> dict:
    body = {**call, **({"partialArgs": list(pieces)} if pieces else {})}
    return _event({"functionCall": {**body, "willContinue": True} if more else body})


def _streamed(events: list[dict], path: str) -> str:
    return "".join(
        piece.get("stringValue", "")
        for event in events
        for candidate in event["candidates"]
        for part in candidate.get("content", {}).get("parts", [])
        for piece in part.get("functionCall", {}).get("partialArgs", [])
        if piece["jsonPath"] == path
    )


def test_streamed_arguments_are_restored_across_events():
    remembered = []
    stream = GeminiStream(_context(lambda masked, restored: remembered.append((masked, restored))))
    events = [
        _call(name="send"),
        _call(_piece("$.to", FAKE[:4])),
        _call(_piece("$.to", FAKE[4:11]), _piece("$.count", numberValue=3, more=False)),
        _call(_piece("$.to", FAKE[11:])),
        _call(_piece("$.to", "", more=False)),
        _call(more=False),
    ]
    out = _run(stream, events)
    assert _streamed(out, "$.to") == ORIGINAL
    assert len(out) == len(events)  # no event dropped: each carries its call
    counts = [p for e in out for p in e["candidates"][0]["content"]["parts"][0]["functionCall"].get("partialArgs", [])]
    assert {"jsonPath": "$.count", "numberValue": 3} in counts
    assert (FAKE, ORIGINAL) in remembered
    _accepted(out)


def test_a_call_that_ends_with_text_held_flushes_into_a_piece_of_that_path():
    out = _run(GeminiStream(_context()), [_call(name="send"), _call(_piece("$.to", "to " + HELD)), _call(more=False)])
    assert _streamed(out, "$.to") == "to " + HELD
    last = out[-1]["candidates"][0]["content"]["parts"][0]["functionCall"]
    assert last["partialArgs"] == [{"jsonPath": "$.to", "stringValue": HELD}] and "willContinue" not in last


def test_a_stream_that_ends_inside_a_call_sends_what_was_held():
    out = _run(GeminiStream(_context()), [_call(name="send"), _call(_piece("$.to", "to " + HELD))])
    assert _streamed(out, "$.to") == "to " + HELD
    call = {"functionCall": {"partialArgs": [{"jsonPath": "$.to", "stringValue": HELD}]}}
    assert out[-1] == {"candidates": [{"index": 0, "content": {"role": "model", "parts": [call]}}]}


def test_googles_empty_last_part_is_sent_as_google_sent_it():
    last = _event({"text": ""}, finish="STOP", usageMetadata=USAGE)
    out = _run(GeminiStream(_context()), [_event({"text": "all " + FAKE + " done"}), last])
    assert _text(out) == f"all {ORIGINAL} done"
    assert out[-1]["candidates"][0]["content"]["parts"]  # never an empty list: python-genai's chat drops the turn


def test_a_finish_without_content_carries_what_was_held():
    finish = {"candidates": [{"index": 0, "finishReason": "STOP"}], "usageMetadata": USAGE}
    out = _run(GeminiStream(_context()), [_event({"text": "to " + HELD}), finish])
    assert len(out) == 2  # nothing after the finish: a client may stop reading there
    assert out[-1]["candidates"][0]["finishReason"] == "STOP"
    assert out[-1]["candidates"][0]["content"] == {"role": "model", "parts": [{"text": HELD}]}


def test_a_candidate_without_content_or_finish_keeps_what_was_held():
    out = _run(
        GeminiStream(_context()),
        [_event({"text": "to " + HELD}), {"candidates": [{"index": 0}]}, _event({"text": FAKE[6:]}, finish="STOP")],
    )
    assert _text(out) == "to " + ORIGINAL
