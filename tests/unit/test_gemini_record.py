"""What the dashboard keeps of a Gemini exchange: Gemini CLI named as its agent, and the answer as the model wrote it
(its text, its function calls, its token counts), streamed, whole or as an array."""

import json

from latet.gateway.recorder import ResponseCapture, agent_of

USAGE = {"promptTokenCount": 10, "cachedContentTokenCount": 4, "candidatesTokenCount": 3, "thoughtsTokenCount": 2}


def _answer(*parts, finish=None, **extra) -> dict:
    candidate = {"index": 0, "content": {"role": "model", "parts": list(parts)}}
    return {"candidates": [candidate | ({"finishReason": finish} if finish else {})], **extra}


def _captured(content_type: str, body: bytes) -> ResponseCapture:
    capture = ResponseCapture("gemini", content_type)
    for i in range(0, len(body), 11):
        capture.feed(body[i : i + 11])
    capture.end()
    capture.settle()
    return capture


def test_gemini_clis_user_agent_names_it():
    real = "GeminiCLI-tui/0.64.0/gemini-3-pro (win32; x64; terminal)"  # what Gemini CLI 0.64 sends, run with -p too
    assert agent_of({"user-agent": real}) == ("gemini-cli", "0.64.0")
    assert agent_of({"user-agent": "GeminiCLI/0.64.0 (win32; x64)"}) == ("gemini-cli", "0.64.0")
    assert agent_of({"user-agent": "google-genai-sdk/2.30.0 gl-node/v22.12.0"}) == ("other", None)


def test_a_streamed_answer_is_captured():
    events = [
        _answer({"text": "thinking", "thought": True}),
        _answer({"text": "Hello "}),
        _answer(
            {"text": "there"},
            {"functionCall": {"name": "send", "args": {"to": "x"}}},
            finish="STOP",
            usageMetadata=USAGE,
        ),
    ]
    wire = b"".join(f"data: {json.dumps(e)}\r\n\r\n".encode() for e in events)
    capture = _captured("text/event-stream", wire)
    assert "".join(capture.text) == "Hello there"
    assert list(capture.tools.values()) == [{"name": "send", "args": '{"to":"x"}'}]
    assert capture.usage == {"input": 6, "output": 5, "cache_read": 4, "cache_write": 0}
    assert capture.done and not capture.failed and not capture.broken


def test_a_whole_answer_and_an_array_are_captured():
    whole = _captured(
        "application/json", json.dumps(_answer({"text": "Hi"}, finish="STOP", usageMetadata=USAGE)).encode()
    )
    assert "".join(whole.text) == "Hi" and whole.usage["output"] == 5 and whole.done
    array = [_answer({"text": "a"}), _answer({"text": "b"}, finish="STOP")]
    streamed = _captured("application/json", json.dumps(array).encode())
    assert "".join(streamed.text) == "ab" and streamed.done


def test_an_error_event_ends_it_as_failed():
    capture = _captured(
        "text/event-stream", b'data: {"error": {"code": 500, "message": "x", "status": "INTERNAL"}}\n\n'
    )
    assert capture.done and capture.failed
