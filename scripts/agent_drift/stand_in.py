"""A stand-in of Anthropic's Messages API and OpenAI's Responses API, for the daily check of the agents' requests
(`.github/workflows/agent-drift.yml`): an agent pointed at it plays one scripted turn, and each request it sends is
recorded for the report (`report.py`), which reads them as Latet's gateway does.

The turn: a request that offers a shell tool the stand-in knows (Claude Code's Bash; Codex's shell_command,
exec_command or shell) is answered with a call of it running ``echo latet-canary``, its arguments as the tool's schema
asks them; a request that brings a tool's result back, with a final text; any other (a title, a summary), with that
text too. Each is answered as the provider streams it (Server-Sent Events, in the order and forms the provider sends
them), or whole when the request does not ask to stream; a token count is answered with a count. Any other call is
answered 404, and recorded.

The record: one JSON line for each request (its method, path, query, headers, the names of its credential headers but
never their values, its body as JSON, decoded first when it came coded) with what it was answered; then, when the
stand-in ran the agent (the command after ``--``), one line saying how it ended: its status, ``timeout`` when it was
stopped after ``--timeout`` seconds, or ``error`` when it did not start. The stand-in listens on 127.0.0.1 alone, at
``--port``, and exits with the agent's status (124 when it hung, 127 when it did not start).

    uv run python scripts/agent_drift/stand_in.py --port 18601 --record claude.jsonl --timeout 300 -- claude -p ...
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from latet.protocols import codec

COMMAND = "echo latet-canary"
FINAL = "Done: the command printed latet-canary."
# The shell tools the stand-in calls, the first offered of them: Claude Code's, then Codex's.
SHELL_TOOLS = ("Bash", "shell_command", "exec_command", "shell")
# Request headers recorded by their name alone, never their value.
CREDENTIALS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie"})
BODY_LIMIT = 64 << 20
JSON_TYPE = "application/json"
EVENTS_TYPE = "text/event-stream; charset=utf-8"
INPUT_TOKENS, OUTPUT_TOKENS = 24, 8


class Recorder:
    """The run's record: one JSON line each, written whole and flushed, from any of the server's threads."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._file = path.open("w", encoding="utf-8")

    def write(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=True) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _arguments(schema: dict[str, Any]) -> dict[str, Any]:
    """The arguments that run ``COMMAND``, as the tool's schema names them: ``cmd`` (exec_command), ``command`` as a
    list of words (shell) or as one text (Bash, shell_command), with Bash's ``description``."""
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    if "cmd" in properties:
        return {"cmd": COMMAND}
    command = properties.get("command")
    arguments: dict[str, Any] = {
        "command": COMMAND.split() if isinstance(command, dict) and command.get("type") == "array" else COMMAND
    }
    if "description" in properties:
        arguments["description"] = "Print a word"
    return arguments


def shell_tool(tools: Any) -> tuple[str, dict[str, Any]] | None:
    """The shell tool ``tools`` offers (an Anthropic or a Responses list), the first of ``SHELL_TOOLS``, with the
    arguments that call it; None when it offers none of them."""
    offered: dict[str, dict[str, Any]] = {}
    for tool in tools if isinstance(tools, list) else ():
        if not isinstance(tool, dict) or tool.get("type", "custom") not in ("custom", "function"):
            continue
        schema = tool.get("input_schema", tool.get("parameters"))
        if isinstance(tool.get("name"), str) and isinstance(schema, dict):
            offered.setdefault(tool["name"], schema)
    for name in SHELL_TOOLS:
        if name in offered:
            return name, _arguments(offered[name])
    return None


def _brings_a_result(body: dict[str, Any], protocol: str) -> bool:
    if protocol == "anthropic":
        messages = body.get("messages")
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for message in (messages if isinstance(messages, list) else ())
            if isinstance(message, dict) and isinstance(message.get("content"), list)
            for block in message["content"]
        )
    items = body.get("input")
    return any(
        isinstance(item, dict) and str(item.get("type", "")).endswith("call_output")
        for item in (items if isinstance(items, list) else ())
    )


def step(body: dict[str, Any], protocol: str) -> tuple[str, tuple[str, dict[str, Any]] | None]:
    """What the turn answers ``body`` with: a ``final text`` once a tool's result is back, a ``tool call`` of the shell
    tool offered, or a ``text``."""
    if _brings_a_result(body, protocol):
        return "final text", None
    tool = shell_tool(body.get("tools"))
    return ("tool call", tool) if tool is not None else ("text", None)


def _model(body: dict[str, Any], default: str) -> str:
    model = body.get("model")
    return model if isinstance(model, str) else default


def _events(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


Answer = tuple[int, str, bytes, str]  # status, content type, payload, what the turn answered


def messages(body: dict[str, Any]) -> Answer:
    """Anthropic's Messages API: a message holding the call of the shell tool or the text, streamed as its events."""
    answered, tool = step(body, "anthropic")
    usage = {
        "input_tokens": INPUT_TOKENS,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": OUTPUT_TOKENS,
    }
    if tool is not None:
        block: dict[str, Any] = {"type": "tool_use", "id": _id("toolu_01"), "name": tool[0], "input": tool[1]}
        opened: dict[str, Any] = block | {"input": {}}
        delta: dict[str, Any] = {"type": "input_json_delta", "partial_json": json.dumps(tool[1])}
        stop = "tool_use"
    else:
        block = {"type": "text", "text": FINAL}
        opened, delta, stop = {"type": "text", "text": ""}, {"type": "text_delta", "text": FINAL}, "end_turn"
    message = {
        "id": _id("msg_01"),
        "type": "message",
        "role": "assistant",
        "model": _model(body, "claude-stand-in"),
        "content": [block],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }
    if body.get("stream") is not True:
        return 200, JSON_TYPE, json.dumps(message).encode(), answered
    started = message | {"content": [], "stop_reason": None, "usage": usage | {"output_tokens": 1}}
    return (
        200,
        EVENTS_TYPE,
        _events(
            [
                {"type": "message_start", "message": started},
                {"type": "content_block_start", "index": 0, "content_block": opened},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": OUTPUT_TOKENS},
                },
                {"type": "message_stop"},
            ]
        ),
        answered,
    )


def responses(body: dict[str, Any]) -> Answer:
    """OpenAI's Responses API: a response whose one output item is the function call of the shell tool or the
    assistant's message, streamed as the API streams it, each event with its sequence number."""
    answered, tool = step(body, "responses")
    if tool is not None:
        arguments = json.dumps(tool[1])
        item: dict[str, Any] = {
            "type": "function_call",
            "id": _id("fc_"),
            "call_id": _id("call_"),
            "name": tool[0],
            "arguments": arguments,
            "status": "completed",
        }
        added = item | {"arguments": "", "status": "in_progress"}
        at = {"item_id": item["id"], "output_index": 0}
        middle: list[dict[str, Any]] = [
            {"type": "response.function_call_arguments.delta", **at, "delta": arguments},
            {"type": "response.function_call_arguments.done", **at, "arguments": arguments},
        ]
    else:
        part = {"type": "output_text", "text": FINAL, "annotations": []}
        item = {"type": "message", "id": _id("msg_"), "role": "assistant", "status": "completed", "content": [part]}
        added = item | {"content": [], "status": "in_progress"}
        at = {"item_id": item["id"], "output_index": 0, "content_index": 0}
        middle = [
            {"type": "response.content_part.added", **at, "part": part | {"text": ""}},
            {"type": "response.output_text.delta", **at, "delta": FINAL},
            {"type": "response.output_text.done", **at, "text": FINAL},
            {"type": "response.content_part.done", **at, "part": part},
        ]
    usage = {
        "input_tokens": INPUT_TOKENS,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": OUTPUT_TOKENS,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
    }
    response = {
        "id": _id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": _model(body, "gpt-stand-in"),
        "output": [item],
        "usage": usage,
    }
    if body.get("stream") is not True:
        return 200, JSON_TYPE, json.dumps(response).encode(), answered
    begun = response | {"status": "in_progress", "output": [], "usage": None}
    events = [
        {"type": "response.created", "response": begun},
        {"type": "response.in_progress", "response": begun},
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        *middle,
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    return 200, EVENTS_TYPE, _events([event | {"sequence_number": n} for n, event in enumerate(events)]), answered


def _error(status: int, kind: str, words: str, answered: str) -> Answer:
    error = {"type": "error", "error": {"type": kind, "message": words}}
    return status, JSON_TYPE, json.dumps(error).encode(), answered


def answer(method: str, path: str, body: Any, unreadable: bool) -> Answer:
    """The turn's answer to a request (``unreadable``: its body was no JSON the stand-in could decode)."""
    if method == "POST" and unreadable:
        return _error(400, "invalid_request_error", "the stand-in reads a JSON body alone", "unreadable")
    if method == "POST" and isinstance(body, dict):
        if path.endswith("/v1/messages/count_tokens"):
            return 200, JSON_TYPE, json.dumps({"input_tokens": INPUT_TOKENS}).encode(), "count"
        if path.endswith("/v1/messages"):
            return messages(body)
        if path.endswith("/responses"):
            return responses(body)
    return _error(404, "not_found_error", "the stand-in answers model calls alone", "not found")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, recorder: Recorder) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.recorder = recorder

    def handle_error(self, request: Any, client_address: Any) -> None:
        # An agent that hangs up a connection kept alive (or is stopped) is no error of the stand-in's.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "latet-agent-drift-stand-in"

    def _payload(self) -> bytes | None:
        """The request's body, as its length or its chunks give it; None for one past ``BODY_LIMIT``, left unread (the
        connection is closed after the answer)."""
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            chunks = []
            while (size := int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)) > 0:
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # the trailer, to its empty line
                pass
            return b"".join(chunks)
        length = int(self.headers.get("content-length") or 0)
        if length > BODY_LIMIT:
            self.close_connection = True
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _serve(self) -> None:
        raw = self._payload()
        path, _, query = self.path.partition("?")
        headers: dict[str, str] = {}
        credentials: list[str] = []
        for name, value in self.headers.items():
            if name.lower() in CREDENTIALS:
                credentials.append(name.lower())
            else:
                headers[name.lower()] = value
        request: dict[str, Any] = {
            "method": self.command,
            "path": path,
            "query": query,
            "headers": headers,
            "credentials": sorted(set(credentials)),
            "body": None,
        }
        if raw is None:
            request["unreadable"] = f"larger than {BODY_LIMIT} bytes"
        elif raw:
            try:  # the codec's errors are ValueErrors, as a body that is no JSON is
                request["body"] = json.loads(codec.decode(raw, headers.get("content-encoding"), limit=BODY_LIMIT))
            except ValueError as error:
                request["unreadable"] = type(error).__name__
        status, kind, payload, answered = answer(self.command, path, request["body"], "unreadable" in request)
        cast(_Server, self.server).recorder.write({"request": request, "answered": answered})
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-cache")
        self.send_header("request-id", _id("req_"))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_POST = do_GET = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _serve

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Its method and path, never its query or headers.
        sys.stderr.write(f"stand-in: {self.command} {self.path.partition('?')[0]} {code}\n")


def serve(port: int, recorder: Recorder) -> _Server:
    """The stand-in, listening on 127.0.0.1 at ``port`` (0: one the system picks) in a thread of its own."""
    server = _Server(port, recorder)
    threading.Thread(target=server.serve_forever, name="agent-drift-stand-in", daemon=True).start()
    return server


def _stop(process: subprocess.Popen[bytes]) -> None:
    """The agent stopped, with what it started in its session where the system has sessions."""
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    process.wait()


def supervise(command: list[str], timeout: int, recorder: Recorder) -> int:
    """``command`` run to its end, ``timeout`` seconds at most (then stopped: 124), and how it ended recorded."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=sys.platform != "win32")
    except OSError as error:
        recorder.write({"agent": {"error": type(error).__name__}})
        return 127
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process)
        recorder.write({"agent": {"timeout": timeout}})
        return 124
    recorder.write({"agent": {"exit": status}})
    return status if status >= 0 else 128 - status


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    own, command = (argv[: argv.index("--")], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [])
    parser = argparse.ArgumentParser(description="A stand-in of the providers that streams a scripted turn.")
    parser.add_argument("--port", type=int, required=True, help="the port on 127.0.0.1 to listen on")
    parser.add_argument("--record", type=Path, required=True, help="the JSON-lines file each request goes to")
    parser.add_argument("--timeout", type=int, default=300, help="seconds the agent may run (default: 300)")
    arguments = parser.parse_args(own)
    recorder = Recorder(arguments.record)
    server = serve(arguments.port, recorder)
    sys.stderr.write(f"stand-in: listening on 127.0.0.1:{server.server_address[1]}\n")
    try:
        if command:
            return supervise(command, arguments.timeout, recorder)
        threading.Event().wait()  # until interrupted
        return 0
    finally:
        server.shutdown()
        server.server_close()
        recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
