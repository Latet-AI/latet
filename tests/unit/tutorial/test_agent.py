"""The tutorial's pretend agent: its requests look like a coding agent's that Latet masks (never like
Claude Code's), it runs the model's tools inside its workspace only, and a tool call the step's turns leave
unanswered is run first by the next step ("Let it write")."""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from latet.gateway.mediate import cwd_from
from latet.gateway.recorder import agent_of
from latet.protocols import anthropic
from latet.tutorial import agent as agent_module
from latet.tutorial.agent import AGENT, SUBSCRIPTION, UA, PaneLine, Sent, TutorialAgent, parse
from latet.tutorial.chapters import Step, ToolCall, Turn
from latet.tutorial.model import MODEL_ID, PretendModel

PORT = 6690


def _step(**fields) -> Step:
    return Step.model_validate({"kind": "watch", "title": "A step", "explain": "Plain words.", **fields})


def _gateway(model: PretendModel, seen: list[dict[str, str]]):
    """A gateway that masks nothing: each request goes on to the pretend model as it is, its headers noted."""

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen.append({k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]})
            path = scope["path"].removeprefix("/anthropic")
            scope = {**scope, "path": path, "raw_path": path.encode()}
        await model.app(scope, receive, send)

    return app


async def _stopping(scope, receive, send):
    """A gateway that stops every request, as Latet stops one it may only check."""
    error = {"type": "invalid_request_error", "message": "Latet: sensitive values would leave in clear"}
    await JSONResponse({"type": "error", "error": error}, status_code=400)(scope, receive, send)


@pytest.fixture
def model() -> PretendModel:
    return PretendModel()


@pytest.fixture
def workspace(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    (folder / ".env").write_bytes(b"# billing\nDEMO_API_KEY=abc123\n")
    (tmp_path / "outside.txt").write_bytes(b"OUTSIDE-THE-WORKSPACE\n")
    return folder


@pytest.fixture
def agent(workspace) -> TutorialAgent:
    agent = TutorialAgent(workspace, PORT)
    agent.new_conversation("basics")
    return agent


def _messages(model: PretendModel, index: int = -1) -> list[dict]:
    return json.loads(model.received[index].body)["messages"]


def _results(model: PretendModel, index: int = -1) -> list[dict]:
    """The tool results in the last message of a request the model received."""
    return [b for b in _messages(model, index)[-1]["content"] if b["type"] == "tool_result"]


async def test_a_request_looks_like_a_coding_agent_that_latet_masks(model, agent):
    seen: list[dict[str, str]] = []
    step = _step(prompt="make no mistakes", model=[{"id": "hi", "text": "Hi."}])
    model.expect(step.model, {})
    run = await agent.run_step(_gateway(model, seen), step, model, 0)
    assert run.error is None and run.sent == [Sent(200, "hi")]
    [headers] = seen
    assert headers["user-agent"] == UA and headers["x-session-id"] == agent.session
    assert headers["x-api-key"] and "authorization" not in headers
    assert "x-claude-code-session-id" not in headers and "x-latet-project" not in headers
    body = json.loads(model.received[0].body)
    assert body["model"] == MODEL_ID and body["stream"] is True
    assert isinstance(body["system"], str) and body["system"].startswith("You are Tutorial Agent")
    assert [tool["name"] for tool in body["tools"]] == ["Read", "Write", "Bash"]
    assert body["messages"] == [{"role": "user", "content": "make no mistakes"}]
    # As Latet reads it: not Claude Code (so it masks rather than only checks), no project folder named.
    assert not anthropic.is_claude_code(headers, body)
    assert cwd_from(headers, anthropic.scan_request(body)) is None
    assert "Working directory" not in model.received[0].text and "<cwd>" not in model.received[0].text
    assert agent_of(headers) == ("tutorial-agent", "0.1")  # the dashboard names it Tutorial Agent
    assert AGENT == "Tutorial Agent" and agent.session_key == f"x-session-id:{agent.session}"


async def test_a_step_signed_in_with_a_subscription_sends_no_api_key(model, agent):
    seen: list[dict[str, str]] = []
    step = _step(prompt="deploy", model=[{"id": "hi", "text": "Hi."}], agent={"auth": "subscription"})
    model.expect(step.model, {})
    await agent.run_step(_gateway(model, seen), step, model, 0)
    assert seen[0]["authorization"] == SUBSCRIPTION and "x-api-key" not in seen[0]
    assert "sk-ant-oat" in SUBSCRIPTION  # what tells Latet it may only check the request


async def test_a_new_conversation_has_a_new_session_and_no_history(model, agent):
    seen: list[dict[str, str]] = []
    gateway = _gateway(model, seen)
    first = _step(prompt="one", model=[{"id": "one", "text": "One."}])
    model.expect(first.model, {})
    await agent.run_step(gateway, first, model, 0)
    before = agent.session
    assert agent.new_conversation("basics") != before
    second = _step(prompt="two", model=[{"id": "two", "text": "Two."}])
    model.expect(second.model, {})
    await agent.run_step(gateway, second, model, 1)
    assert seen[1]["x-session-id"] == agent.session != before
    assert _messages(model) == [{"role": "user", "content": "two"}]


async def test_a_step_that_clears_starts_a_new_conversation(model, agent):
    gateway = _gateway(model, [])
    first = _step(prompt="one", model=[{"id": "one", "text": "One."}])
    model.expect(first.model, {})
    await agent.run_step(gateway, first, model, 0)
    before = agent.session
    second = _step(prompt="two", model=[{"id": "two", "text": "Two."}], agent={"clear": True})
    model.expect(second.model, {})
    run = await agent.run_step(gateway, second, model, 1)
    assert agent.session != before and _messages(model) == [{"role": "user", "content": "two"}]
    assert run.lines[0] == PaneLine(1, "system", text="/clear: a new conversation, with no history", note="cleared")


async def test_a_step_that_resumes_a_conversation_sends_what_was_said_then_first_and_shows_it_as_resumed(model, agent):
    """A conversation from before Latet was on, resumed: what was said then goes before the prompt, as Claude Code
    sends a resumed conversation, and the pane shows it, greyed, under a line that says where it comes from."""
    gateway = _gateway(model, [])
    first = _step(prompt="one", model=[{"id": "one", "text": "One."}])
    model.expect(first.model, {})
    await agent.run_step(gateway, first, model, 0)
    before = agent.session
    then = [{"user": "deploy with KEY-1"}, {"model": "Deploying."}]
    step = _step(prompt="continue the deploy", model=[{"id": "go", "text": "Done."}], agent={"history": then})
    model.expect(step.model, {})
    run = await agent.run_step(gateway, step, model, 1)
    assert agent.session != before  # not the conversation of the steps before
    assert _messages(model) == [
        {"role": "user", "content": "deploy with KEY-1"},
        {"role": "assistant", "content": [{"type": "text", "text": "Deploying."}]},
        {"role": "user", "content": "continue the deploy"},
    ]
    assert [line.as_json() for line in run.lines] == [
        {
            "step": 1,
            "type": "system",
            "text": "/resume: yesterday's conversation, from before Latet was on",
            "note": "resumed",
        },
        {"step": 1, "type": "prompt", "text": "deploy with KEY-1", "resumed": True},
        {"step": 1, "type": "model", "text": "Deploying.", "resumed": True},
        {"step": 1, "type": "prompt", "text": "continue the deploy"},
        {"step": 1, "type": "model", "text": "Done."},
    ]


async def test_the_pane_shows_the_prompt_the_agent_and_the_answer_as_it_got_it(model, agent):
    step = _step(prompt="make no mistakes", agent={"says": ["On it."]}, model=[{"id": "hi", "text": "Who is JSON?"}])
    model.expect(step.model, {})
    run = await agent.run_step(_gateway(model, []), step, model, 3)
    assert [line.as_json() for line in run.lines] == [
        {"step": 3, "type": "prompt", "text": "make no mistakes"},
        {"step": 3, "type": "agent", "text": "On it."},
        {"step": 3, "type": "model", "text": "Who is JSON?"},
    ]
    assert run.got == ["Who is JSON?"]


async def test_read_sends_the_file_as_it_is(model, agent):
    step = _step(
        prompt="read .env",
        agent={"files": [".env"]},
        model=[{"id": "read", "tool": {"name": "Read", "input": {"file_path": ".env"}}}, {"id": "ok", "text": "Ok."}],
    )
    model.expect(step.model, {})
    run = await agent.run_step(_gateway(model, []), step, model, 0)
    [result] = _results(model)
    assert result == {"type": "tool_result", "tool_use_id": "toolu_read", "content": "# billing\nDEMO_API_KEY=abc123\n"}
    assert run.lines[1] == PaneLine(0, "tool", name="Read", summary=".env", output="Read 2 lines")
    assert run.sent == [Sent(200, "read"), Sent(200, "ok")]


async def test_read_sends_a_picture_as_the_image_itself(model, agent, workspace):
    picture = b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000
    (workspace / "shot.png").write_bytes(picture)
    step = _step(
        prompt="look at shot.png",
        agent={"files": ["shot.png"]},
        model=[
            {"id": "read", "tool": {"name": "Read", "input": {"file_path": "shot.png"}}},
            {"id": "ok", "text": "Ok."},
        ],
    )
    model.expect(step.model, {})
    run = await agent.run_step(_gateway(model, []), step, model, 0)
    [result] = _results(model)
    source = {"type": "base64", "media_type": "image/png", "data": base64.b64encode(picture).decode()}
    assert result == {
        "type": "tool_result",
        "tool_use_id": "toolu_read",
        "content": [{"type": "image", "source": source}],
    }
    assert run.lines[1] == PaneLine(0, "tool", name="Read", summary="shot.png", output="Read image (3 KB)")


def _asks(model: PretendModel, tool: str, **given: str) -> Step:
    """The model asks for ``tool`` with ``given``, whatever a chapter could say, then says Ok: the step."""
    call = Turn.model_construct(id="call", text=None, tool=ToolCall.model_construct(name=tool, input=given))
    model.expect([call, Turn(id="ok", text="Ok.")], {})
    return _step(prompt="do it", model=[{"id": "ok", "text": "Ok."}])


@pytest.mark.parametrize(
    ("name", "media"),
    [
        ("shot.png", "image/png"),
        ("shots/shot.PNG", "image/png"),  # the suffix is read in any case
        ("shot.jpg", "image/jpeg"),
        ("shot.JPEG", "image/jpeg"),
        ("shot.gif", "image/gif"),
        ("shot.webp", "image/webp"),
    ],
)
async def test_read_sends_a_picture_as_the_type_its_name_says(model, agent, workspace, name, media):
    (workspace / name).parent.mkdir(exist_ok=True)
    (workspace / name).write_bytes(b"the picture's bytes")
    await agent.run_step(_gateway(model, []), _asks(model, "Read", file_path=name), model, 0)
    [result] = _results(model)
    assert "is_error" not in result
    data = base64.b64encode(b"the picture's bytes").decode()
    assert result["content"] == [{"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}]


async def test_read_sends_a_file_that_is_not_a_picture_as_text_whatever_it_holds(model, agent, workspace):
    """Only a picture's suffix makes Read send an image: an SVG is a text, and a name that merely holds `.png` is not
    a picture."""
    (workspace / "logo.svg").write_bytes(b"<svg/>\n")
    (workspace / "notes.png.txt").write_bytes(b"a text\n")
    for name, text in (("logo.svg", "<svg/>\n"), ("notes.png.txt", "a text\n")):
        await agent.run_step(_gateway(model, []), _asks(model, "Read", file_path=name), model, 0)
        assert _results(model)[0]["content"] == text


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "..\\outside.txt",
        "sub/../../outside.txt",
        ".. /outside.txt",  # Windows drops the space: `..`
        "/etc/passwd",
        "C:/Windows/win.ini",
        "",
        "CON",  # Windows' devices: reading the console would wait for it, and stop everything else meanwhile
        "nul",
        "aux.txt",
    ],
)
async def test_read_never_leaves_the_workspace(model, agent, path):
    step = _asks(model, "Read", file_path=path)
    run = await asyncio.wait_for(agent.run_step(_gateway(model, []), step, model, 0), timeout=10)
    [result] = _results(model)
    assert result["is_error"] is True and "OUTSIDE-THE-WORKSPACE" not in model.received[-1].text
    assert run.lines[1].type == "tool" and run.lines[1].output == result["content"]


async def test_read_reads_files_only(model, agent, workspace, monkeypatch):
    """A folder is not read, nor a device a path still names there (`NUL` on Windows): reading one could wait."""
    (workspace / "src").mkdir()
    await agent.run_step(_gateway(model, []), _asks(model, "Read", file_path="src"), model, 0)
    [result] = _results(model)
    assert (result["is_error"], result["content"]) == (True, "Not a file.")
    monkeypatch.setattr(agent_module, "relative", lambda path: True)  # as if a device's name got past the paths' check
    await agent.run_step(_gateway(model, []), _asks(model, "Read", file_path="NUL"), model, 1)
    [result] = _results(model)
    assert result["is_error"] is True and result["content"] in ("Not a file.", "File does not exist.")


def _link(link: Path, folder: Path) -> None:
    """``link``, a link to ``folder``: a symbolic one, or on Windows a junction (which needs no rights)."""
    try:
        os.symlink(folder, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(folder), str(link))


async def test_no_tool_goes_out_of_the_workspace_through_a_link(model, agent, workspace, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_bytes(b"OUTSIDE-THE-WORKSPACE\n")
    try:
        _link(workspace / "out", elsewhere)
    except OSError:
        pytest.skip("this computer does not let a test make a link")
    assert (workspace / "out" / "secret.txt").is_file()  # the link leads there
    gateway = _gateway(model, [])
    await agent.run_step(gateway, _asks(model, "Read", file_path="out/secret.txt"), model, 0)
    [read] = _results(model)
    assert read["is_error"] is True and "OUTSIDE-THE-WORKSPACE" not in model.received[-1].text
    await agent.run_step(gateway, _asks(model, "Write", file_path="out/new.txt", content="x"), model, 1)
    [write] = _results(model)
    assert write["is_error"] is True and not (elsewhere / "new.txt").exists()


async def test_write_writes_inside_the_workspace_only_and_as_it_was_sent(model, agent, workspace, tmp_path):
    content = '{"api_key": "abc123"}\n'
    write = {"file_path": "conf/config.local.json", "content": content}
    escape = ToolCall.model_construct(name="Write", input={"file_path": "../escape.txt", "content": "x"})
    model.expect(
        [
            Turn.model_validate({"id": "write", "tool": {"name": "Write", "input": write}}),
            Turn.model_construct(id="escape", text=None, tool=escape),
            Turn(id="ok", text="Ok."),
        ],
        {},
    )
    run = await agent.run_step(
        _gateway(model, []), _step(prompt="write it", model=[{"id": "ok", "text": "Ok."}]), model, 0
    )
    assert (workspace / "conf" / "config.local.json").read_bytes() == content.encode()  # no \r\n on Windows either
    assert not (tmp_path / "escape.txt").exists()
    [escaped] = _results(model)
    assert escaped["is_error"] is True
    assert PaneLine(0, "file", path="conf/config.local.json", content=content) in run.lines
    assert content in run.got


async def test_bash_answers_from_the_step_or_from_the_sandbox_and_runs_nothing_else(model, workspace):
    asked: list[str] = []

    async def config_set(command: str) -> str:
        asked.append(command)
        return "The change waits for your approval in the dashboard."

    agent = TutorialAgent(workspace, PORT, commands={"latet config set": config_set})
    agent.new_conversation("settings")
    psql = 'psql -c "select * from customers limit 5"'
    calls = [psql, "latet config set ner off", "rm -rf /"]
    step = _step(
        prompt="make the tests faster",
        agent={"bash": {psql: " id | name\n----+------\n  1 | Ada\n"}},
        model=[
            *({"id": f"bash-{i}", "tool": {"name": "Bash", "input": {"command": c}}} for i, c in enumerate(calls)),
            {"id": "ok", "text": "Ok."},
        ],
    )
    model.expect(step.model, {})
    run = await agent.run_step(_gateway(model, []), step, model, 0)
    outputs = [_results(model, i)[0]["content"] for i in (1, 2, 3)]
    assert outputs[0] == " id | name\n----+------\n  1 | Ada\n"
    assert outputs[1] == "The change waits for your approval in the dashboard."
    assert "runs no commands" in outputs[2]
    assert asked == ["latet config set ner off"]
    assert [line.summary for line in run.lines if line.type == "tool"] == calls


async def test_a_tool_call_left_at_the_end_of_a_step_is_run_first_by_the_next(model, agent, workspace):
    gateway = _gateway(model, [])
    write = {"file_path": "config.local.json", "content": '{"api_key": "abc123"}'}
    first = _step(
        prompt="read .env and set up config.local.json",
        agent={"files": [".env"]},
        model=[
            {"id": "read", "tool": {"name": "Read", "input": {"file_path": ".env"}}},
            {"id": "write", "text": "Writing the config.", "tool": {"name": "Write", "input": write}},
        ],
    )
    model.expect(first.model, {})
    run = await agent.run_step(gateway, first, model, 0)
    assert not (workspace / "config.local.json").exists()  # not yet: the step's turns ran out
    assert run.lines[-1] == PaneLine(0, "tool", name="Write", summary="config.local.json", pending=True)
    assert model.leftover() == []

    second = _step(button="Let it write", model=[{"id": "done", "text": "Done."}])
    model.expect(second.model, {})
    run = await agent.run_step(gateway, second, model, 1)
    assert (workspace / "config.local.json").read_bytes() == write["content"].encode()
    assert [line.type for line in run.lines] == ["tool", "file", "model"]
    assert run.lines[0].pending is None and run.lines[0].output
    [result] = _results(model)
    assert result["tool_use_id"] == "toolu_write"
    assert write["content"] in run.got  # what it ran is what it got
    assert run.sent == [Sent(200, "done")]


async def test_a_step_with_nothing_to_send_sends_nothing_and_keeps_what_is_left(model, agent):
    gateway = _gateway(model, [])
    first = _step(prompt="list", model=[{"id": "ls", "tool": {"name": "Bash", "input": {"command": "ls"}}}])
    model.expect(first.model, {})
    await agent.run_step(gateway, first, model, 0)
    do = Step.model_validate({"kind": "do", "title": "Click", "explain": "Click.", "wait": {"pin": "x"}})
    run = await agent.run_step(gateway, do, model, 1)
    assert run.lines == [] and run.sent == [] and len(model.received) == 1
    assert [call["name"] for call in agent.pending] == ["Bash"]


async def test_a_stopped_request_shows_the_error_and_ends_the_step(model, agent):
    step = _step(prompt="deploy with the key", agent={"expect": "blocked"})
    run = await agent.run_step(_stopping, step, model, 0)
    assert run.error == "Latet: sensitive values would leave in clear"
    assert run.lines[-1] == PaneLine(0, "error", text="Latet: sensitive values would leave in clear")
    assert run.sent == [Sent(400, None)] and model.received == []


def test_the_stream_is_put_together_as_an_agent_reads_it():
    events = [
        {"type": "message_start", "message": {"id": "msg_x", "content": []}},
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Who is "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "JSON?"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"comm'}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": 'and": "ls"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    stream = "".join(f"event: {e['type']}\r\ndata: {json.dumps(e)}\r\n\r\n" for e in events)
    answer = parse(stream)
    assert answer.stop == "tool_use" and answer.error is None
    assert answer.blocks == [
        {"type": "text", "text": "Who is JSON?"},
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}, "args": '{"command": "ls"}'},
    ]
    assert answer.content[1] == {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    error = parse('event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}\n\n')
    assert error.error == "Busy"
