"""The tutorial's sandbox, proven before it is built: a pretend agent talks to the real gateway in-process,
and the gateway to a pretend model through the client it is given (httpx's ASGITransport both ways, no
network). Masked out, restored back, in a stream and in a tool call; a request Latet may only check is
stopped; the demo values get the same fakes at every run; a server on 127.0.0.1 ends, its folder deletable."""

import asyncio
import contextlib
import json
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.responses import JSONResponse

from latet.config import Config, TermConfig
from latet.engine.detectors.base import DetectContext
from latet.engine.detectors.gitleaks import GitleaksDetector
from latet.engine.types import SlotKind
from latet.gateway.app import create_app
from latet.runtime import Runtime, make_runtime
from latet.tutorial.agent import parse, send
from latet.tutorial.chapters import Capture, Turn
from latet.tutorial.model import PretendModel, PretendUpstream
from latet.tutorial.sandbox import BASE, ROOT_KEY

pytestmark = pytest.mark.e2e

PORT = 6690  # in-process, nothing listens on it: it only names the host the gateway accepts

# The demo values, all invented, and what the sandbox makes of each: the rule that finds it, its type and
# class, and its fake. The chapters and the screenshots rely on these: a change here changes the tutorial.
KEY = "3D8QnFhA0QhAC9t5j7t6JtgLgPWLFMHbLRuT7TWZ"  # 40 characters, no provider's prefix
EMAIL = "james.wilson@acme.com"
PHONE = "+44 20 7946 0958"
CARD = "4242 4242 4242 4242"  # Stripe's test card
TERM = "Project Falcon"  # added in the Rules as the editor adds it: type CUSTOM
GOLDEN = {
    KEY: ("gitleaks:generic-api-key", "GENERIC_API_KEY", "SECRET", "3G0OoYyX6TrNM8v8g6r5AjrVwCAXUXKoGDzV6URN"),
    EMAIL: ("pii:email", "EMAIL", "PII", "emily.adams@proseware-4795.example"),
    "acme.com": ("pii:email", "DOMAIN", "PII", "proseware-4795.example"),  # an email brings its domain along
    PHONE: ("pii:phone", "PHONE", "PII", "+44 28 9578 4573"),
    CARD: ("pii:credit-card", "CREDIT_CARD", "PII", "4365 0952 1057 8408"),
    TERM: ("terms:custom", "CUSTOM", "CUSTOM", "Woodgrove Tailspin"),
}
FAKE = {value: golden[3] for value, golden in GOLDEN.items()}

ENV = f"# billing service\nBILLING_URL=https://billing.example.test/api\nDEMO_API_KEY={KEY}\n"
PROMPT = f"make no mistakes. set up config.local.json for prod, then email {EMAIL} the invoice, his number is {PHONE}"
PASTED = f"make no mistakes. deploy with DEMO_API_KEY={KEY}"  # one line: the pair and gitleaks' rule find it: one leak
# Where the model finds each value in the body it got, as a chapter's `received` patterns do.
GOT = {
    "KEY": Capture.model_validate({"pattern": r"DEMO_API_KEY=(\w+)", "in": "tool_result"}),
    "EMAIL": Capture.model_validate({"pattern": r"email (\S+@[\w.-]+) the invoice", "in": "user"}),
    "PHONE": Capture.model_validate({"pattern": r"his number is (\+[\d ]+\d)", "in": "user"}),
}
MADE_UP = "+44 20 7946 0123"  # a number the model makes up: a phone to the detectors, but nobody's value
CONFIG = '{"billing_url": "https://billing.example.test/api", "api_key": "{{received:KEY}}"}'
# The model's first turn: it writes the config with the key it got, and says what it will do.
WRITE = Turn.model_validate(
    {
        "id": "write",
        "text": "Who is JSON? Config written. I'll email {{received:EMAIL}}, or call {{received:PHONE}}. "
        f"Complaints: {MADE_UP}.",
        "tool": {"name": "Write", "input": {"file_path": "config.local.json", "content": CONFIG}},
    }
)


def _runtime(root: Path, port: int, **settings: Any) -> Runtime:
    """A runtime with the sandbox's base settings and its fixed key (the same fakes for everyone, at every run),
    its files in ``root``."""
    config = Config.model_validate({**BASE, "port": port, **settings})
    return make_runtime(config, data=root / "data", config_file=root / "config.yaml", root_key=ROOT_KEY)


def _sandbox(rt: Runtime, model: PretendModel) -> None:
    """The pretend model, reached in-process, is the gateway's only upstream."""
    rt.gateway.upstreams = [PretendUpstream()]
    rt.gateway.client = model.client


@contextlib.asynccontextmanager
async def _agent(rt: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    """The gateway, started as the daemon starts it, and the agent's client to it, in-process."""
    app = create_app(rt.gateway)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        base = f"http://127.0.0.1:{rt.gateway.port}"
        async with httpx.AsyncClient(transport=transport, base_url=base, trust_env=False) as agent:
            yield agent


@pytest.fixture
def model() -> PretendModel:
    return PretendModel()


@pytest.fixture
def rt(tmp_path, model):
    runtime = _runtime(tmp_path, PORT)
    _sandbox(runtime, model)
    yield runtime
    runtime.close()


async def test_a_round_trip_masks_what_leaves_and_restores_what_comes_back(rt, model):
    model.expect([WRITE, Turn(id="done", text="Done. I made no mistakes. Probably.")], GOT)
    read = {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": ".env"}}
    history: list[dict[str, Any]] = [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": [read]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_read", "content": ENV}]},
    ]
    async with _agent(rt) as agent:
        first = await send(agent, "tutorial-1", history)
        assert first.status_code == 200
        text, write = parse(first.text).blocks
        # The second turn resends it all, the model's answer as the agent got it.
        done = {"type": "tool_result", "tool_use_id": "toolu_write", "content": "Wrote config.local.json"}
        written = {key: write[key] for key in ("type", "id", "name", "input")}
        history += [{"role": "assistant", "content": [text, written]}, {"role": "user", "content": [done]}]
        second = await send(agent, "tutorial-1", history)
        assert second.status_code == 200

    # The model got fakes only, the sandbox's own; the agent got the real values back, in the text and in
    # the Write call's arguments (JSON).
    sent = model.received[0].text
    for value in (KEY, EMAIL, PHONE, "acme.com"):
        assert value not in sent and FAKE[value] in sent
    assert text["text"] == f"Who is JSON? Config written. I'll email {EMAIL}, or call {PHONE}. Complaints: {MADE_UP}."
    assert KEY in write["args"] and FAKE[KEY] not in write["args"]
    config = json.dumps({"billing_url": "https://billing.example.test/api", "api_key": KEY})
    assert write["input"] == {"file_path": "config.local.json", "content": config}
    assert parse(second.text).blocks == [{"type": "text", "text": "Done. I made no mistakes. Probably."}]

    # The second turn: the history leaves as it left before, and the model's answer comes back to it as it
    # wrote it, fakes and all, and the number it made up not masked as if it were the user's.
    before, after = (json.loads(r.body) for r in model.received)
    assert after["messages"][:3] == before["messages"]
    assert after["messages"][3] == {"role": "assistant", "content": model.received[0].answer}
    assert KEY not in model.received[1].text and MADE_UP in model.received[1].text

    # The dashboard's record: the exchange of this conversation, the values it masked and restored.
    assert await asyncio.to_thread(rt.recorder.drain, 10)
    first, _ = rt.exchanges.in_order("x-session-id:tutorial-1")  # as they were sent (their start times can be equal)
    detail = rt.exchanges.detail(first)
    assert detail is not None
    summary = detail["summary"]
    assert (summary["mode"], summary["status"], summary["verified"]) == ("rewrite", 200, True)
    assert summary["agent"] != "claude-code"
    assert {"GENERIC_API_KEY", "EMAIL", "PHONE"} <= set(summary["by_type"])
    slots = detail["request"]["slots"]
    masked = {slot["masked"][r["masked_start"] : r["masked_end"]] for slot in slots for r in slot["replacements"]}
    assert masked == {FAKE[KEY], FAKE[EMAIL], FAKE[PHONE]}
    response = detail["response"]  # as the model wrote it, with where each fake was restored
    answered = {response["text"][s["masked_start"] : s["masked_end"]] for s in response["text_spans"]}
    assert answered == {FAKE[EMAIL], FAKE[PHONE]}
    [call] = response["tool_calls"]
    assert call["name"] == "Write"
    assert {call["args"][s["masked_start"] : s["masked_end"]] for s in call["spans"]} == {FAKE[KEY]}


async def test_a_request_latet_may_only_check_is_stopped_until_the_secret_is_left_out(rt, model):
    model.expect([Turn(id="deploy", text="Deploying with the key in DEMO_API_KEY. No mistakes made.")], {})
    pasted = [{"role": "user", "content": PASTED}]
    async with _agent(rt) as agent:
        stopped = await send(agent, "tutorial-stopped", pasted, auth="subscription")
        again = [*pasted, {"role": "user", "content": "try again. make no mistakes"}]
        retried = await send(agent, "tutorial-stopped", again, auth="subscription")
        fresh = [{"role": "user", "content": "deploy with the key in DEMO_API_KEY; never paste it"}]
        passed = await send(agent, "tutorial-fresh", fresh, auth="subscription")

    # Anthropic's error shape, as Claude Code reads it; never the value.
    for response in (stopped, retried):
        assert response.status_code == 400 and response.headers["x-should-retry"] == "false"
        error = response.json()
        assert set(error) == {"type", "error"} and error["type"] == "error"
        assert error["error"]["type"] == "invalid_request_error"
        message = error["error"]["message"]
        assert message.startswith("Latet: sensitive values would leave in clear: GENERIC_API_KEY[40]")
        assert KEY not in response.text
    # Only the new conversation, without the secret, reached the model; its answer comes back as it is.
    assert passed.status_code == 200
    expected = [{"type": "text", "text": "Deploying with the key in DEMO_API_KEY. No mistakes made."}]
    assert parse(passed.text).blocks == expected
    assert len(model.received) == 1 and KEY not in model.received[0].text

    assert await asyncio.to_thread(rt.recorder.drain, 10)
    stops, _ = rt.exchanges.list(session="x-session-id:tutorial-stopped")
    assert len(stops) == 2
    for summary in stops:
        assert (summary["mode"], summary["status"], summary["verified"]) == ("verify", 400, False)
        assert {"blocked", "leak"} <= set(summary["flags"])
        assert "would leave in clear" in summary["block_reason"] and KEY not in summary["block_reason"]
    [leak] = rt.exchanges.detail(stops[-1]["id"])["leaks"]  # its type and length, never the value
    assert (leak["type"], leak["kind"], leak["preview"]) == ("GENERIC_API_KEY", "user_text", "GENERIC_API_KEY[40]")
    [fine], _ = rt.exchanges.list(session="x-session-id:tutorial-fresh")
    assert (fine["mode"], fine["status"], fine["verified"]) == ("verify", 200, True)
    assert not {"blocked", "leak"} & set(fine["flags"])


def test_the_demo_values_are_found_and_get_the_same_fakes_at_every_run(tmp_path):
    rt = _runtime(tmp_path, PORT, terms=[TermConfig(value=TERM)])
    try:
        engine = rt.built.engine
        texts = [
            (ENV, SlotKind.TOOL_RESULT),  # the agent reads .env
            (f"email {EMAIL} the invoice, his number is {PHONE}. Who is JSON?", SlotKind.USER_TEXT),
            (f'def test_checkout():\n    pay(card="{CARD}", amount=2000)\n', SlotKind.TOOL_RESULT),  # a test file
            (f"status of {TERM}? I'll tip you $200", SlotKind.USER_TEXT),
        ]
        found = {}
        for text, kind in texts:
            for r in engine.mask_text(text, kind, engine.session("probe")).replacements:
                found[r.original] = (r.rule, r.etype, r.eclass.name, r.replacement)
        domain = rt.built.vault.by_original("acme.com")
        assert domain is not None
        found["acme.com"] = (domain.rule, domain.etype, domain.eclass.name, domain.surrogate)
        assert found == GOLDEN
    finally:
        rt.close()


def test_the_demo_key_matches_no_provider_rule():
    """Secret scanners see an invented key, no provider's: of gitleaks' rules only the generic one finds it,
    in .env and in a one-line prompt (where it is the only detector that does)."""
    gitleaks = GitleaksDetector()
    for text, kind in ((ENV, SlotKind.TOOL_RESULT), (PASTED, SlotKind.USER_TEXT)):
        assert [span.rule for span in gitleaks.detect(text, DetectContext(kind))] == ["gitleaks:generic-api-key"]


def test_a_server_on_this_computer_ends_and_its_folder_can_be_deleted(tmp_path):
    root = tmp_path / "latet-tutorial-spike"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))  # a free port, known before the app is made: the host it accepts
    port = listener.getsockname()[1]
    model = PretendModel()
    model.expect([Turn(id="noted", text="Who is JSON? I wrote down {{received:EMAIL}}.")], GOT)
    rt = _runtime(root, port)
    _sandbox(rt, model)
    gateway = create_app(rt.gateway)

    async def served(scope: Any, receive: Any, send_: Any) -> None:
        """The gateway, and a route where the agent runs on the server's own loop and talks to the same
        gateway in-process, as the tutorial's agent will."""
        if scope["type"] != "http" or scope["path"] != "/spike/agent":
            await gateway(scope, receive, send_)
            return
        transport = httpx.ASGITransport(app=gateway)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(transport=transport, base_url=base, trust_env=False) as agent:
            response = await send(agent, "tutorial-server", [{"role": "user", "content": PROMPT}])
        answer = {"status": response.status_code, "answer": parse(response.text).blocks}
        await JSONResponse(answer)(scope, receive, send_)

    # The settings `latet serve` and the daemon run uvicorn with (cli/main.py run_server), on the socket above.
    config = uvicorn.Config(
        served,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws="websockets-sansio",
        ws_max_size=64 * 1024 * 1024,
        ws_per_message_deflate=True,
        timeout_keep_alive=75,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(config)
    gateway.state.server = server
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            assert http.get("/_latet/health").json()["masking"] is True
            turn = http.get("/spike/agent").json()
        stopping = time.monotonic()
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive() and time.monotonic() - stopping < 3  # no connection or task held it
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()  # uvicorn closed it at shutdown; not when the server never started
        rt.close()
    assert turn == {"status": 200, "answer": [{"type": "text", "text": f"Who is JSON? I wrote down {EMAIL}."}]}
    assert EMAIL not in model.received[0].text
    shutil.rmtree(root)  # Windows refuses to delete a file still open: none may be
    assert not root.exists()
