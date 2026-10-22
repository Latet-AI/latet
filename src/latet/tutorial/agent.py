"""The tutorial's pretend agent.

It sends what a coding agent sends through Latet's gateway: a system prompt, the user's prompt, its tools (Read,
Write, Bash), the conversation so far and the tools' results; it reads the gateway's stream with a small parser
of its own, runs the tools the model asks for inside its workspace only (its Read sends a picture as the image
itself, as Claude Code's does), and shows what it got back, restored. It never looks like Claude Code (Latet
masks its requests: gateway mode) unless a step signs it in with a subscription, which Latet may only check. It
calls the gateway in-process, on the caller's event loop: the masking runs in a worker thread, and calling the
gateway from there would wait on itself.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import httpx
from starlette.types import ASGIApp

from latet.tutorial.chapters import IMAGE_TYPES, Said, Step, relative
from latet.tutorial.model import MODEL_ID, PretendModel

AGENT: Final = "Tutorial Agent"
UA: Final = "tutorial-agent/0.1"
API_KEY: Final = "tutorial-agent-key"  # the key a provider would get (the pretend model reads none)
# A Pro/Max login as Claude Code sends it (`sk-ant-oat`): Latet may then only check the request.
SUBSCRIPTION: Final = "Bearer sk-ant-oat-tutorial-not-a-token"
SYSTEM: Final = "You are Tutorial Agent, a coding agent."
NOT_RUN: Final = "Not run: in the tutorial, the agent runs no commands."
# A conversation from before Latet was on, resumed: the pane says so above what was said in it.
RESUMED: Final = "/resume: yesterday's conversation, from before Latet was on"
# A step that clears: the pane says a new conversation starts.
CLEARED: Final = "/clear: a new conversation, with no history"

Auth = Literal["api-key", "subscription"]
Command = Callable[[str], Awaitable[str]]  # a command acting on the sandbox: what it prints


def _tool(name: str, *fields: str) -> dict[str, Any]:
    schema = {"type": "object", "properties": {f: {"type": "string"} for f in fields}, "required": list(fields)}
    return {"name": name, "description": f"{name} in the project's folder.", "input_schema": schema}


TOOLS: Final = [_tool("Read", "file_path"), _tool("Write", "file_path", "content"), _tool("Bash", "command")]


def headers(session: str, auth: Auth = "api-key") -> dict[str, str]:
    """A request's headers: its own user agent and conversation id, and a key or a subscription."""
    sent = {"user-agent": UA, "x-session-id": session}
    if auth == "subscription":
        sent["authorization"] = SUBSCRIPTION
    else:
        sent["x-api-key"] = API_KEY
    return sent


def body(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": MODEL_ID,
        "max_tokens": 1024,
        "stream": True,
        "system": SYSTEM,
        "tools": TOOLS,
        "messages": messages,
    }


async def send(
    client: httpx.AsyncClient, session: str, messages: list[dict[str, Any]], auth: Auth = "api-key"
) -> httpx.Response:
    """One request of the conversation ``session``, through the gateway's Anthropic route."""
    return await client.post("/anthropic/v1/messages", json=body(messages), headers=headers(session, auth))


@dataclass(slots=True)
class Answer:
    """The model's answer as the agent put it together: text blocks, and tool calls with their input as it
    came (``args``, JSON) and parsed (``input``)."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    stop: str | None = None
    error: str | None = None

    @property
    def content(self) -> list[dict[str, Any]]:
        """The answer as the agent sends it back in the conversation."""
        return [{key: value for key, value in block.items() if key != "args"} for block in self.blocks]


def _events(stream: str) -> Iterator[dict[str, Any]]:
    for chunk in stream.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].removeprefix(" ") for line in chunk.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            event = json.loads("\n".join(data))
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse(stream: str) -> Answer:
    """An Anthropic stream, put together."""
    answer = Answer()
    blocks: dict[int, dict[str, Any]] = {}
    for event in _events(stream):
        kind = event.get("type")
        if kind == "content_block_start":
            blocks[event["index"]] = {**event["content_block"], "args": ""}
        elif kind == "content_block_delta" and event.get("index") in blocks:
            block, delta = blocks[event["index"]], event.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                block["args"] += delta.get("partial_json", "")
        elif kind == "message_delta":
            answer.stop = (event.get("delta") or {}).get("stop_reason", answer.stop)
        elif kind == "error":
            answer.error = (event.get("error") or {}).get("message") or "the model's answer broke off"
    for _, block in sorted(blocks.items()):
        if block["type"] == "tool_use":
            try:
                block["input"] = json.loads(block["args"]) if block["args"] else {}
            except ValueError:
                answer.error = f"the input of {block.get('name')} could not be read"
                block["input"] = {}
        elif block["type"] == "text":
            del block["args"]
        else:
            continue  # thinking and the like: not shown, not sent back
        answer.blocks.append(block)
    return answer


@dataclass(frozen=True, slots=True)
class PaneLine:
    """One line of the agent's pane: a prompt, its own words, the model's text, an error, a note, a tool it ran
    (or will run: ``pending``), or a file it wrote."""

    step: int
    type: Literal["prompt", "agent", "model", "error", "system", "tool", "file"]
    text: str | None = None
    name: str | None = None  # tool
    summary: str | None = None
    output: str | None = None
    pending: bool | None = None
    path: str | None = None  # file
    content: str | None = None
    resumed: bool | None = None  # said in a conversation from before Latet was on, resumed
    # A system line the dashboard writes in the reader's language (`text` is the English, for the tests and logs).
    note: Literal["resumed", "cleared"] | None = None

    def as_json(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class Sent:
    """One request of a step: its status, and the model's turn that answered it (None: it never got one)."""

    status: int
    turn: str | None


@dataclass(slots=True)
class StepRun:
    """What a step did: the pane's lines, the requests sent, what came back, the error when one was stopped."""

    lines: list[PaneLine] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)
    got: list[str] = field(default_factory=list)  # the model's texts and tool inputs, as the agent got them
    error: str | None = None


class _Refused(Exception):
    """A tool call the agent does not run: the model is told why."""


class TutorialAgent:
    def __init__(self, workspace: Path, port: int, commands: Mapping[str, Command] | None = None) -> None:
        self.workspace = workspace
        self.port = port
        # Commands that act on the sandbox (`latet config set`): run by the session, which prints their answer.
        self.commands: dict[str, Command] = dict(commands or {})
        self.chapter = ""
        self.session = ""
        self.history: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []  # tool calls the model asked for that no step ran yet

    @property
    def session_key(self) -> str:
        """The conversation, as Latet names it in its records."""
        return f"x-session-id:{self.session}"

    def new_conversation(self, chapter_id: str) -> str:
        """A new conversation: a new id, no history, nothing left to run."""
        self.chapter = chapter_id
        self.session = f"tutorial-{chapter_id}-{secrets.token_hex(4)}"
        self.history, self.pending = [], []
        return self.session

    async def run_step(
        self, app: ASGIApp, step: Step, model: PretendModel, index: int, run: StepRun | None = None
    ) -> StepRun:
        """Run ``step`` through the gateway ``app``: the prompt (after the tool calls the step before left), then
        the model's tool calls, until the step's turns run out (a tool call then left waits for the next step)
        or the model stops asking. A step that sends nothing (the user acts, or reads) runs nothing. ``run``: the
        record to fill as it goes (the pane shows its lines while a request waits for the user)."""
        run = run if run is not None else StepRun()
        if step.agent.clear:
            self.new_conversation(self.chapter)
            run.lines.append(PaneLine(index, "system", text=CLEARED, note="cleared"))
        elif step.agent.history:  # resumed: what was said in it goes first, as it was said
            self.new_conversation(self.chapter)
            self.history = [_said(said) for said in step.agent.history]
            run.lines += resumed(step, index)
        elif not self.session:  # no conversation started yet
            self.new_conversation(self.chapter)
        if step.prompt is not None:
            run.lines.append(PaneLine(index, "prompt", text=step.prompt))
        run.lines += [PaneLine(index, "agent", text=said) for said in step.agent.says]
        if not step.sends:
            return run
        # In-process: no timeout applies, so a request the Live Reviewer holds waits as long as the user takes.
        transport = httpx.ASGITransport(app=app)
        base = f"http://127.0.0.1:{self.port}"
        async with httpx.AsyncClient(transport=transport, base_url=base, trust_env=False) as client:
            content = []
            for call in self.pending:
                run.got += [call["args"], *_texts(call["input"])]
                content.append(await self._run(call, step, index, run))
            self.pending = []
            if step.prompt is not None:
                content.append({"type": "text", "text": step.prompt})
            if not content:  # a chapter's checks refuse such a step: no prompt, and nothing left to run
                run.error = "nothing to send: no prompt, and no tool call left by the step before"
                return run
            only_text = len(content) == 1 and content[0]["type"] == "text"
            self.history.append({"role": "user", "content": content[0]["text"] if only_text else content})
            while True:
                before = len(model.received)
                response = await send(client, self.session, self.history, step.agent.auth)
                turn = model.received[before].turn if len(model.received) > before else None
                run.sent.append(Sent(response.status_code, turn))
                answer = parse(response.text) if response.status_code == 200 else Answer(error=_error(response))
                if answer.error is not None:
                    run.error = answer.error
                    run.lines.append(PaneLine(index, "error", text=answer.error))
                    break
                self.history.append({"role": "assistant", "content": answer.content})
                calls = []
                for block in answer.blocks:
                    if block["type"] == "text":
                        run.lines.append(PaneLine(index, "model", text=block["text"]))
                        run.got.append(block["text"])
                    else:
                        calls.append(block)
                        run.got += [block["args"], *_texts(block["input"])]
                if not calls:
                    break
                if not model.queued():  # the step's turns ran out: the next step runs them
                    self.pending = calls
                    run.lines += [
                        PaneLine(index, "tool", name=c["name"], summary=_summary(c), pending=True) for c in calls
                    ]
                    break
                results = [await self._run(call, step, index, run) for call in calls]
                self.history.append({"role": "user", "content": results})
        return run

    async def _run(self, call: dict[str, Any], step: Step, index: int, run: StepRun) -> dict[str, Any]:
        """Run one tool call; its result for the model."""
        name, given, summary = call["name"], call.get("input") or {}, _summary(call)
        try:
            if name == "Read":
                path = self._path(given.get("file_path"))
                if not path.is_file():  # a folder, or a device: reading Windows' CON would wait for the console
                    raise _Refused("Not a file." if path.exists() else "File does not exist.")
                media = IMAGE_TYPES.get(path.suffix.lower())
                if media is not None:  # a picture: Read sends the image itself, as Claude Code's does
                    data = path.read_bytes()
                    output = f"Read image ({round(len(data) / 1024)} KB)"
                    run.lines.append(PaneLine(index, "tool", name=name, summary=summary, output=output))
                    return _image(call, media, base64.b64encode(data).decode("ascii"))
                text = path.read_bytes().decode("utf-8")
                count = len(text.splitlines())
                run.lines.append(PaneLine(index, "tool", name=name, summary=summary, output=f"Read {_lines(count)}"))
                return _result(call, text)
            if name == "Write":
                path, text = self._path(given.get("file_path")), given.get("content")
                if not isinstance(text, str):
                    raise _Refused("Write needs a content")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))  # as it was sent: no newline translated
                output = f"Wrote {_lines(len(text.splitlines()))}"
                run.lines.append(PaneLine(index, "tool", name=name, summary=summary, output=output))
                run.lines.append(PaneLine(index, "file", path=given["file_path"], content=text))
                return _result(call, f"Wrote {given['file_path']}")
            if name == "Bash":
                command = given.get("command")
                if not isinstance(command, str):
                    raise _Refused("Bash needs a command")
                output = await self._bash(command, step)
                run.lines.append(PaneLine(index, "tool", name=name, summary=summary, output=output))
                return _result(call, output)
            raise _Refused(f"No tool named {name}")
        except _Refused as exc:
            refused = str(exc)
        except FileNotFoundError:
            refused = "File does not exist."
        except UnicodeDecodeError:
            refused = "Not a text file."
        except OSError as exc:  # its own words, without the path: the workspace's is this computer's
            refused = exc.strerror or type(exc).__name__
        run.lines.append(PaneLine(index, "tool", name=name, summary=summary, output=refused))
        return _result(call, refused, error=True)

    def _path(self, given: Any) -> Path:
        """A path the model gave, inside the workspace: never absolute, never above it, not through a link."""
        if not isinstance(given, str) or not relative(given):
            raise _Refused(f"Not a path in this project: {given!r}")
        root = self.workspace.resolve()
        path = (root / given).resolve()
        if not path.is_relative_to(root) or path == root:
            raise _Refused(f"Not a path in this project: {given!r}")
        return path

    async def _bash(self, command: str, step: Step) -> str:
        """A command's output: prepared by the step, or what the sandbox answers for its own commands."""
        if command in step.agent.bash:
            return step.agent.bash[command]
        for prefix, handler in self.commands.items():
            if command == prefix or command.startswith(prefix + " "):
                return await handler(command)
        return NOT_RUN


def resumed(step: Step, index: int) -> list[PaneLine]:
    """What the pane shows of the conversation step ``index`` resumes from before Latet was on: a line that says so,
    then what was said in it, greyed. The page shows them from the start of the step, as /resume shows a conversation
    before anything is sent; none for a step that resumes none."""
    if not step.agent.history:
        return []
    said = [
        PaneLine(index, "prompt", text=turn.user, resumed=True)
        if turn.user is not None
        else PaneLine(index, "model", text=turn.model, resumed=True)
        for turn in step.agent.history
    ]
    return [PaneLine(index, "system", text=RESUMED, note="resumed"), *said]


def _said(said: Said) -> dict[str, Any]:
    """A turn of a conversation resumed, as the agent sends it again."""
    if said.user is not None:
        return {"role": "user", "content": said.user}
    return {"role": "assistant", "content": [{"type": "text", "text": said.model}]}


def _summary(call: dict[str, Any]) -> str:
    given = call.get("input") or {}
    return str(given.get("command") if call.get("name") == "Bash" else given.get("file_path", ""))


def _lines(count: int) -> str:
    return f"{count} line" if count == 1 else f"{count} lines"


def _texts(value: Any) -> list[str]:
    """Every text in a tool call's input."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _result(call: dict[str, Any], content: str, error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": call["id"], "content": content}
    if error:
        result["is_error"] = True
    return result


def _image(call: dict[str, Any], media: str, data: str) -> dict[str, Any]:
    """A tool result holding a picture, as Claude Code's Read sends one."""
    image = {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "tool_result", "tool_use_id": call["id"], "content": [image]}


def _error(response: httpx.Response) -> str:
    """The message of an error answer (Anthropic's shape), or its status and text."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = None
    return message if isinstance(message, str) else f"{response.status_code} {response.text[:200]}".strip()
