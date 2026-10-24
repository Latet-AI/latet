"""A fake LLM provider: records every byte it receives and echoes the last user text back, split
into small streaming pieces (text and tool-call arguments), in each wire format Latet handles."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

PIECE = 5  # small pieces so surrogates are always split across events
# A Bedrock answer as Amazon streams it (event-stream frames), recorded in the spike.
BEDROCK_STREAM = Path(__file__).parents[1] / "data" / "eventstream" / "claude_stream.bin"


def _pieces(text: str, size: int = PIECE) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _gemini_text(body: dict[str, Any]) -> str | None:
    """The last user turn's text of a Gemini request (either spelling of its fields), None for another protocol's."""
    contents = body.get("contents")
    if contents is None:
        return None
    for content in reversed(contents if isinstance(contents, list) else [contents]):
        if isinstance(content, dict) and content.get("role", "user") == "user":
            parts = content.get("parts") or []
            parts = parts if isinstance(parts, list) else [parts]
            texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if texts:
                return " ".join(texts)
    return ""


def _last_user_text(body: dict[str, Any]) -> str:
    if (said := _gemini_text(body)) is not None:
        return said
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    value = body.get("input")
    if isinstance(value, str):
        return value
    for item in reversed(value or []):
        if item.get("type", "message") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                return content
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def response_object(body: dict[str, Any], output: list[dict[str, Any]]) -> dict[str, Any]:
    """A Responses API answer not streamed, with the fields OpenAI always sends: a client that reads it into a schema
    of its own (Junie) refuses one without them."""
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_790_000_000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": body.get("model"),
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": body.get("previous_response_id"),
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "metadata": {},
    }


def _sse(event: str | None, data: Any) -> bytes:
    head = f"event: {event}\n" if event else ""
    return f"{head}data: {json.dumps(data)}\n\n".encode()


class FakeUpstream:
    def __init__(self, *, real: bool = False, text_only: bool = False, finish: tuple[str, str] | None = None) -> None:
        # ``real``: answers as the providers do in what the official SDKs read (a chat not streamed answered as JSON,
        # the model and the token counts in every message); otherwise as the tests of the gateway expect them.
        # ``text_only``: answers with text and no tool call, as the header `x-fake-text-only: 1` asks of one request:
        # an agent runs the tool calls it is sent, and would call `write` again and again.
        # ``finish``: (a tool, its parameter) an agent ends its turn with and takes no answer without (Junie's `submit`,
        # its `solution_summary`): a request not streamed that offers it is answered with a call to it, the echo in it.
        self.real = real
        self.text_only = text_only
        self.finish = finish
        self.received: list[bytes] = []
        self.requests: list[dict[str, Any]] = []
        self.ws_received: list[str] = []
        self.app = Starlette(
            routes=[
                Route("/v1/messages", self.anthropic, methods=["POST"]),
                Route("/v1/chat/completions", self.chat, methods=["POST"]),
                Route("/v1/responses", self.responses, methods=["POST"]),
                WebSocketRoute("/backend-api/codex/responses", self.responses_ws),
                Route("/{path:path}", self.anything, methods=["GET", "POST", "PUT", "DELETE", "HEAD"]),
            ]
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FakeUpstream:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def all_received(self) -> str:
        return "\n".join([b.decode("utf-8", "replace") for b in self.received] + self.ws_received)

    async def _receive(self, request: Request) -> bytes:
        """The body, recorded with the request's method, path as sent, query and headers."""
        raw = await request.body()
        self.received.append(raw)
        path = request.scope.get("raw_path") or request.url.path.encode()
        self.requests.append(
            {
                "method": request.method,
                "path": path.decode("latin-1"),
                "query": request.url.query,
                "headers": dict(request.headers),
            }
        )
        return raw

    async def anything(self, request: Request) -> Any:
        """Any other path: Bedrock's and Vertex's answers (a fixed one), else an empty JSON."""
        raw = await self._receive(request)
        path = request.url.path
        method = path.rsplit(":", 1)[-1] if ":" in path.rsplit("/", 1)[-1] else ""
        if method in ("generateContent", "streamGenerateContent", "countTokens", "embedContent", "batchEmbedContents"):
            return self.gemini(request, method, json.loads(raw or b"{}"))
        if request.headers.get("x-amz-security-token") == "expired-token":  # AWS's answer to stale credentials
            expired = {"message": "The security token included in the request is expired"}
            return JSONResponse(expired, 403, headers={"x-amzn-ErrorType": "ExpiredTokenException:http://internal"})
        if path.endswith("/invoke-with-response-stream"):
            return Response(BEDROCK_STREAM.read_bytes(), media_type="application/vnd.amazon.eventstream")
        if path.endswith("/count-tokens"):
            return JSONResponse({"inputTokens": 12})
        if path.endswith(":streamRawPredict"):
            events = [_sse("message_start", {"type": "message_start", "message": {"id": "msg_v", "content": []}})]
            events.append(_sse("message_stop", {"type": "message_stop"}))
            return Response(b"".join(events), media_type="text/event-stream")
        if path.endswith("/invoke") or path.endswith(":rawPredict"):
            return JSONResponse({"id": "msg_c", "type": "message", "role": "assistant", "content": []})
        if "/inference-profiles" in path:
            return JSONResponse({"inferenceProfileSummaries": []})
        return JSONResponse({})

    # --- Anthropic ---------------------------------------------------------------------------------

    async def anthropic(self, request: Request) -> Any:
        raw = await self._receive(request)
        body = json.loads(raw)
        echo = "You said: " + _last_user_text(body)
        args = json.dumps({"content": _last_user_text(body)})
        text_only = self._text_only(request)
        if (finish := self._finish(body)) is not None:
            call = {"type": "tool_use", "id": "toolu_1", "name": finish[0], "input": finish[1]}
            return JSONResponse({"id": "msg_1", "type": "message", "role": "assistant", "content": [call],
                                 "stop_reason": "tool_use", **self._anthropic_counts(body)})  # fmt: skip
        if not body.get("stream"):
            content = [{"type": "text", "text": echo}]
            if not text_only:
                content.append({"type": "tool_use", "id": "toolu_1", "name": "Write", "input": json.loads(args)})
            return JSONResponse(
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": content,
                    "stop_reason": "end_turn" if text_only else "tool_use",
                    **self._anthropic_counts(body),
                }
            )

        def events():
            started = {"id": "msg_1", "content": [], **self._anthropic_counts(body, start=True)}
            yield _sse("message_start", {"type": "message_start", "message": started})
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            )
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm " + echo},
                },
            )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _sse(
                "content_block_start",
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            )
            yield _sse("ping", {"type": "ping"})
            for piece in _pieces(echo):
                yield _sse(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": piece}},
                )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 1})
            if text_only:
                delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
                yield _sse("message_delta", delta | ({"usage": {"output_tokens": len(echo)}} if self.real else {}))
                yield _sse("message_stop", {"type": "message_stop"})
                return
            yield _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {}},
                },
            )
            for piece in _pieces(args, 7):
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 2,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    },
                )
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": 2})
            delta: dict[str, Any] = {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
            if self.real:
                delta["usage"] = {"output_tokens": len(echo)}
            yield _sse("message_delta", delta)
            yield _sse("message_stop", {"type": "message_stop"})

        return StreamingResponse(events(), media_type="text/event-stream")

    def _finish(self, body: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
        """The tool and arguments a request not streamed is answered with (``finish``), None when it offers no such
        tool."""
        if self.finish is None or body.get("stream"):
            return None
        name, parameter = self.finish
        offered = {t.get("name") or (t.get("function") or {}).get("name") for t in body.get("tools") or []}
        return (name, {parameter: "You said: " + _last_user_text(body)}) if name in offered else None

    def _text_only(self, request: Request) -> bool:
        return self.text_only or request.headers.get("x-fake-text-only") == "1"

    def _anthropic_counts(self, body: dict[str, Any], start: bool = False) -> dict[str, Any]:
        """What a real message carries besides its content (``real``): its role, model and token counts."""
        if not self.real:
            return {}
        usage = {"input_tokens": 1, "output_tokens": 1}
        return {
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "stop_sequence": None,
            "usage": usage,
        } | ({"stop_reason": None} if start else {})

    # --- Google Gemini ---------------------------------------------------------------------------

    def gemini(self, request: Request, method: str, body: dict[str, Any]) -> Any:
        """Gemini's answers, on the Gemini API's paths and Vertex's alike: the echo in pieces, then (unless ``real``, or
        the header ``x-fake-text-only: 1``) a call of ``write``; streamed as SSE events (``alt=sse``) or as one array.
        ``real`` answers as Google does: an id, the model and the token counts in every event, the last part signed."""
        if method == "countTokens":
            return JSONResponse({"totalTokens": 12})
        if method == "embedContent":
            return JSONResponse({"embedding": {"values": [0.1, 0.2]}})
        if method == "batchEmbedContents":
            return JSONResponse({"embeddings": [{"values": [0.1]} for _ in body.get("requests") or []]})
        said = _last_user_text(body)
        echo = "You said: " + said
        text_only = self.real or request.headers.get("x-fake-text-only") == "1"
        events = [{"text": piece} for piece in _pieces(echo)]
        if not text_only:
            events.append({"functionCall": {"name": "write", "args": {"content": said}}})
        if self.real:
            events.append({"text": "", "thoughtSignature": "c2lnbmF0dXJl"})
        model = request.url.path.rsplit("/", 1)[-1].split(":", 1)[0]
        usage = {"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6}
        real = {"usageMetadata": usage, "modelVersion": model, "responseId": "resp-gemini-1"} if self.real else {}

        def answer(parts: list[dict[str, Any]], last: bool) -> dict[str, Any]:
            candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
            if last:
                candidate["finishReason"] = "STOP"
            return {"candidates": [candidate], **real}

        if method == "generateContent":
            text = [{"text": echo}] + [e for e in events if "text" not in e or e.get("thoughtSignature")]
            return JSONResponse(answer(text, True))
        answers = [answer([event], i == len(events) - 1) for i, event in enumerate(events)]
        if request.query_params.get("alt") != "sse":
            return JSONResponse(answers)
        wire = (f"data: {json.dumps(a)}\r\n\r\n".encode() for a in answers)
        return StreamingResponse(wire, media_type="text/event-stream")

    # --- OpenAI Chat -----------------------------------------------------------------------------

    async def chat(self, request: Request) -> Any:
        raw = await self._receive(request)
        body = json.loads(raw)
        echo = "You said: " + _last_user_text(body)
        args = json.dumps({"content": _last_user_text(body)})
        if self.real:
            return self._real_chat(body, echo)

        def chunks():
            for piece in _pieces(echo):
                yield f"data: {json.dumps({'id': 'c1', 'choices': [{'index': 0, 'delta': {'content': piece}, 'finish_reason': None}]})}\n\n".encode()
            for i, piece in enumerate(_pieces(args, 7)):
                call = {"index": 0, "function": {"arguments": piece}}
                if i == 0:
                    call.update(id="call_1", type="function", function={"name": "write", "arguments": piece})
                yield f"data: {json.dumps({'id': 'c1', 'choices': [{'index': 0, 'delta': {'tool_calls': [call]}, 'finish_reason': None}]})}\n\n".encode()
            yield f"data: {json.dumps({'id': 'c1', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    def _real_chat(self, body: dict[str, Any], echo: str) -> Any:
        """A chat as OpenAI answers it (``real``): JSON when it is not streamed, chunks of text when it is."""
        head = {"id": "chatcmpl-1", "created": 1_790_000_000, "model": body.get("model")}
        if not body.get("stream"):
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            message = {"index": 0, "message": {"role": "assistant", "content": echo}, "finish_reason": "stop"}
            if (finish := self._finish(body)) is not None:
                call = {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": finish[0], "arguments": json.dumps(finish[1])},
                }
                said = {"role": "assistant", "content": None, "tool_calls": [call]}
                message = {"index": 0, "message": said, "finish_reason": "tool_calls"}
            return JSONResponse({**head, "object": "chat.completion", "choices": [message], "usage": usage})

        def chunks():
            for piece in _pieces(echo):
                choice = {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                yield _sse(None, {**head, "object": "chat.completion.chunk", "choices": [choice]})
            done = {"index": 0, "delta": {}, "finish_reason": "stop"}
            yield _sse(None, {**head, "object": "chat.completion.chunk", "choices": [done]})
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    # --- OpenAI Responses ------------------------------------------------------------------------

    def _responses_events(self, body: dict[str, Any], text_only: bool = False) -> list[dict[str, Any]]:
        echo = "You said: " + _last_user_text(body)
        args = json.dumps({"content": _last_user_text(body)})
        seq = iter(range(1, 10_000))
        events = [
            {"type": "response.created", "sequence_number": next(seq), "response": {"id": "resp_1", "output": []}}
        ]
        msg = {"type": "message", "id": "msg_1", "role": "assistant", "content": []}
        events.append(
            {"type": "response.output_item.added", "sequence_number": next(seq), "output_index": 0, "item": msg}
        )
        # The message's part opens and closes as OpenAI's do: Prime Agent adds a delta only to a part already open
        part = {"type": "output_text", "text": "", "annotations": []}
        events.append(
            {
                "type": "response.content_part.added",
                "sequence_number": next(seq),
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "part": part,
            }
        )
        for piece in _pieces(echo):
            events.append(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": next(seq),
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": piece,
                }
            )
        events.append(
            {
                "type": "response.output_text.done",
                "sequence_number": next(seq),
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "text": echo,
            }
        )
        events.append(
            {
                "type": "response.content_part.done",
                "sequence_number": next(seq),
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "part": {**part, "text": echo},
            }
        )
        done_msg = {**msg, "content": [{"type": "output_text", "text": echo}]}
        events.append(
            {"type": "response.output_item.done", "sequence_number": next(seq), "output_index": 0, "item": done_msg}
        )
        if text_only:  # an agent that runs tool calls (Codex) would call `write` again and again
            usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
            done = {"id": "resp_1", "status": "completed", "output": [done_msg], "usage": usage}
            events.append({"type": "response.completed", "sequence_number": next(seq), "response": done})
            return events
        call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write", "arguments": ""}
        events.append(
            {"type": "response.output_item.added", "sequence_number": next(seq), "output_index": 1, "item": call}
        )
        for piece in _pieces(args, 7):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": next(seq),
                    "item_id": "fc_1",
                    "output_index": 1,
                    "delta": piece,
                }
            )
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "sequence_number": next(seq),
                "item_id": "fc_1",
                "output_index": 1,
                "arguments": args,
            }
        )
        done_call = {**call, "arguments": args}
        events.append(
            {"type": "response.output_item.done", "sequence_number": next(seq), "output_index": 1, "item": done_call}
        )
        events.append(
            {
                "type": "response.completed",
                "sequence_number": next(seq),
                "response": {"id": "resp_1", "output": [done_msg, done_call]},
            }
        )
        return events

    async def responses(self, request: Request) -> Any:
        raw = await self._receive(request)
        body = json.loads(raw)
        events = self._responses_events(body, self._text_only(request))
        if self.real and not body.get("stream"):  # a response not streamed comes whole, as JSON (Junie asks for it)
            if (finish := self._finish(body)) is not None:
                call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": finish[0],
                        "arguments": json.dumps(finish[1]), "status": "completed"}  # fmt: skip
                return JSONResponse(response_object(body, [call]))
            return JSONResponse(response_object(body, events[-1]["response"]["output"]))
        if self.real:  # the response the stream begins and ends with is whole, as OpenAI sends it (Goose reads it)
            events[0]["response"] = response_object(body, []) | {"status": "in_progress", "usage": None}
            events[-1]["response"] = response_object(body, events[-1]["response"]["output"])
        return StreamingResponse((_sse(e["type"], e) for e in events), media_type="text/event-stream")

    async def responses_ws(self, websocket: WebSocket) -> None:
        await websocket.accept()
        # `x-fake-hold: 1`: a turn's last event, `response.completed`, waits for a `fake.release` frame.
        hold = websocket.headers.get("x-fake-hold") == "1"
        held: list[dict[str, Any]] = []
        while True:
            try:
                frame = await websocket.receive_text()
            except Exception:
                return
            self.ws_received.append(frame)
            body = json.loads(frame)
            if body.get("type") == "fake.release":
                events, held = held, []
            else:
                events = self._responses_events(body)
                if hold:
                    events, held = events[:-1], events[-1:]
            for event in events:
                await websocket.send_text(json.dumps(event))
