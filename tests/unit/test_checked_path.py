"""Claude Code's requests are checked, never rewritten: the final check stops what would leave in clear, and nothing of
what leaves in clear by your choice or by Latet's own hand. A type your policy lets through, a word inside a fake, what
the model wrote, exactly as it wrote it, leave; a value of yours in the same place still stops the request.

What the model wrote is known, never guessed: a text of an answer Latet passed on as it came, or the input of a tool
call the plugin had the daemon restore before the tool ran. A text in the model's place that is neither, and every
assistant turn of another client on a route Latet only checks, is checked as a client's text is."""

import base64
import json

import httpx
import pytest
from starlette.testclient import TestClient

from latet.bridge.api import Bridge
from latet.config import Config, IdentityConfig, TermConfig
from latet.engine.build import build
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.gateway.app import GatewayConfig, OnServer, create_app
from latet.gateway.mediate import VERIFY, Mediator
from latet.gateway.routes import Upstream
from latet.gateway.written import strings

from ..conftest import ROOT_KEY
from .test_what_leaves import ANTHROPIC, CLAUDE_CODE, EMAIL, PHONE, SECRET, STOPPED

PASSWORD = 'TEST_PASSWORD = "hunter2"\n'  # an assignment the model writes in a test, on a line of its own
SESSION = CLAUDE_CODE["x-claude-code-session-id"]
EDIT = {
    "file_path": "/home/u/proj/tests/test_login.py",
    "old_string": "import os\n",
    "new_string": f"import os\n{PASSWORD}",
}
# Claude Code's Edit answers with a snippet of the file it edited: the password the model wrote, shown again.
ECHO = (
    "The file /home/u/proj/tests/test_login.py has been updated. Here's the result of running `cat -n` on a snippet of "
    f"the edited file:\n     1\timport os\n     2\t{PASSWORD.strip()}\n     3\t"
)


def _mediator(config: Config | None = None, vault: Vault | None = None) -> Mediator:
    vault = vault if vault is not None else Vault(ROOT_KEY)
    config = config if config is not None else Config()
    built = build(config, vault, identity=config.identity)
    return Mediator(built.engine, built.verifier)


def _claude_code(*, output: str = "done", edit: str = "x = 1\n", said: str | None = None) -> bytes:
    """A request of Claude Code's: the model's Edit (its new text ``edit``, after its words ``said``), and the tool's
    output after it."""
    call = {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "tests/x.py", "new_string": edit}}
    words = [{"type": "text", "text": said}] if said is not None else []
    messages = [
        {"role": "user", "content": "add the test"},
        {"role": "assistant", "content": [*words, call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}]},
    ]
    return json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()


def _checked(mediator: Mediator, raw: bytes, headers: dict[str, str] | None = None):
    prepared = mediator.prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", headers or CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY
    return prepared


def _restored_for_the_tool(bridge: Bridge, **tool_input: str) -> None:
    """The plugin, before a local tool runs: the strings of the model's call sent to the daemon to restore, marked as
    the model's (``register.ts``, ``tool.call``)."""
    bridge._rehydrate({"session": SESSION, "items": strings(tool_input), "model": True})


def test_a_type_your_policy_lets_through_leaves_and_another_type_in_the_same_place_still_stops_it():
    """Your policy lets addresses through: the plugin leaves them in clear on purpose, and the daemon's final check, as
    the plugin's own (the bridge's), lets them out. A phone number in the same place is stopped."""
    mediator = _mediator(Config(policies={"EMAIL": "allow"}))
    allowed = _checked(mediator, _claude_code(output=f"the owner is {EMAIL}"))
    assert allowed.block_reason is None and allowed.leaks == []
    plugin = mediator.verifier.verify([(f"the owner is {EMAIL}", SlotKind.TOOL_RESULT, False)], "cc:s-1")
    assert plugin.leaks == []
    stopped = _checked(mediator, _claude_code(output=f"the owner is {PHONE}"))
    assert stopped.block_reason == f"{STOPPED}PHONE[16]"


def test_a_word_inside_a_fake_leaves_and_the_same_word_of_yours_in_clear_still_stops_it():
    """A fake drawn for a person holds one of your terms and your user name (Daniel Jordan for Robert Johnson, beside
    the term Jordan and the user daniel): the plugin sent the fake, and nothing of yours is in it. Your term or your
    user name in clear, in the same text, is stopped."""
    config = Config(
        terms=[TermConfig(value="Jordan", type="CLIENT")],
        identity=IdentityConfig(usernames=["daniel"], username="daniel"),
    )
    vault = Vault(ROOT_KEY)
    mediator = _mediator(config, vault)
    vault.intern("PERSON", EntityClass.PII, "Robert Johnson", generate=lambda original, drbg: "Daniel Jordan")
    fake = _checked(mediator, _claude_code(output="the ticket was opened by Daniel Jordan"))
    assert fake.block_reason is None and fake.leaks == []
    for clear, found in (("Jordan called back", "CLIENT[6]"), ("see /home/daniel/notes", "USERNAME[6]")):
        stopped = _checked(mediator, _claude_code(output=f"the ticket was opened by Daniel Jordan; {clear}"))
        assert stopped.block_reason == f"{STOPPED}{found}", clear


def test_what_the_model_wrote_in_its_tool_call_leaves_once_the_plugin_had_it_restored():
    """The model writes a password for a test in its Edit: the plugin sent the Edit's strings to the daemon before the
    tool ran, so the final check knows them as the model's, and they leave, on the daemon's check as on the plugin's.
    The same text in the model's place that the daemon never saw the model write is checked as a client's text is:
    stopped, and so is the assignment in a tool's output (a file of yours)."""
    mediator = _mediator()
    bridge = Bridge(mediator, "token")
    unknown = _checked(mediator, _claude_code(edit=PASSWORD))
    assert unknown.block_reason == f"{STOPPED}PASSWORD[7]"
    _restored_for_the_tool(bridge, file_path="tests/x.py", new_string=PASSWORD)
    written = _checked(mediator, _claude_code(edit=PASSWORD))
    assert written.block_reason is None and written.leaks == []
    # The plugin's own check, through the bridge: the same answer.
    slots = [("add the test", "user_text", False), ("tests/x.py", "tool_input", True), (PASSWORD, "tool_input", True)]
    result, _ = bridge._verify({"session": SESSION}, [(t, SlotKind(k), m) for t, k, m in slots], f"cc:{SESSION}")
    assert result.leaks == []
    yours = _checked(_mediator(), _claude_code(output=PASSWORD))  # another conversation: the text is new to it
    assert yours.block_reason == f"{STOPPED}PASSWORD[7]"


def test_a_secret_the_vault_knows_leaves_in_what_the_model_wrote_and_stops_the_request_elsewhere():
    """A secret of yours the vault knows, in the model's Edit as the model wrote it, leaves: those are the model's own
    bytes. Written in the model's place by anyone else, it stops the request."""
    vault = Vault(ROOT_KEY)
    vault.intern("GITHUB_PAT", EntityClass.SECRET, SECRET)  # masked before, in an earlier tool output
    mediator = _mediator(vault=vault)
    edit = f'TOKEN = "{SECRET}"\n'
    stopped = _checked(mediator, _claude_code(edit=edit)).block_reason
    assert stopped == f"{STOPPED}GITHUB_PAT[40]"  # one value, found three ways, named by the specific rule
    _restored_for_the_tool(Bridge(mediator, "token"), file_path="tests/x.py", new_string=edit)
    assert _checked(mediator, _claude_code(edit=edit)).block_reason is None


def test_the_edits_answer_masked_by_the_plugin_stops_no_request_after():
    """The review's probe: the model writes TEST_PASSWORD = "hunter2" in an Edit; Claude Code's Edit answers with a
    snippet of the file, which the plugin has the daemon mask, and the vault learns hunter2 as a secret. The next
    request carries the model's Edit as it wrote it, and the masked answer: it leaves, and so does every one after."""
    mediator = _mediator()
    bridge = Bridge(mediator, "token")
    _restored_for_the_tool(bridge, **EDIT)  # before the tool ran
    data = {"session": SESSION, "items": [{"text": ECHO, "kind": "tool_result", "tool": "Edit"}]}
    _, out, _, _, _ = bridge._mask(data)
    masked = out[0]["text"]
    assert mediator.engine.vault.by_original("hunter2") is not None and "hunter2" not in masked
    call = {"type": "tool_use", "id": "t1", "name": "Edit", "input": EDIT}
    messages = [
        {"role": "user", "content": "add a login test"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": masked}]},
    ]
    for turn in range(3):
        raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()
        assert _checked(mediator, raw).block_reason is None, turn
        messages += [{"role": "assistant", "content": "done"}, {"role": "user", "content": "go on"}]


def test_a_text_restored_for_the_screen_is_not_taken_as_the_models():
    """The plugin restores what the screen shows too, whoever wrote it: only a tool call's input, marked so, is the
    model's."""
    mediator = _mediator()
    Bridge(mediator, "token")._rehydrate({"session": SESSION, "items": ["tests/x.py", PASSWORD]})
    assert _checked(mediator, _claude_code(edit=PASSWORD)).block_reason == f"{STOPPED}PASSWORD[7]"


def test_what_the_plugin_says_the_model_wrote_is_not_taken_on_a_server():
    """A Latet server never takes a computer's word for what the model wrote: there, the model's texts are those of the
    answers the server passed on itself."""
    mediator = _mediator()
    bridge = Bridge(mediator, "token", on_server=OnServer("https://latet.example", "id-1"))
    _restored_for_the_tool(bridge, file_path="tests/x.py", new_string=PASSWORD)
    assert _checked(mediator, _claude_code(edit=PASSWORD)).block_reason == f"{STOPPED}PASSWORD[7]"


# --- an answer Latet passed on as it came ------------------------------------------------------------------------------

PROVIDER = "http://127.0.0.1:1"
SAID = f"I added the login test's password:\n{PASSWORD}"


def _answer(said: str, edit: str) -> list[dict]:
    raw = json.dumps({"file_path": "tests/x.py", "new_string": edit})
    return [
        {"type": "message_start", "message": {"id": "msg_1", "content": [], "usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": said[:9]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": said[9:]}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
        },
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[:11]}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": raw[11:]}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ]


def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _bedrock(events: list[dict]) -> bytes:
    from latet.protocols.eventstream import encode

    def frame(event: dict) -> bytes:
        payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode()
        return encode({":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"}, payload)

    return b"".join(frame(e) for e in events)


class _Provider:
    def __init__(self) -> None:
        self.answer = httpx.Response(200)
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self.answer


@pytest.fixture
def gateway():
    provider = _Provider()
    mediator = _mediator()
    config = GatewayConfig(
        port=5283,
        upstreams=[Upstream("anthropic", "/anthropic", PROVIDER)],
        mediator=mediator,
        client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    with TestClient(create_app(config), base_url="http://127.0.0.1:5283") as client:
        yield client, provider, mediator


def _whole(events: list[dict]) -> bytes:
    """The same answer, not streamed: one body."""
    raw = json.dumps({"file_path": "tests/x.py", "new_string": PASSWORD})
    said = "".join(e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta")
    call = {"type": "tool_use", "id": "t1", "name": "Edit", "input": json.loads(raw)}
    return json.dumps({"id": "msg_1", "content": [{"type": "text", "text": said}, call]}).encode()


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("text/event-stream", _sse), ("application/vnd.amazon.eventstream", _bedrock), ("application/json", _whole)],
    ids=["anthropic", "bedrock", "whole"],
)
def test_what_the_model_wrote_in_an_answer_latet_passed_on_leaves_as_it_wrote_it(gateway, content_type, body):
    """Claude Code's request leaves, and its answer is passed on as it came: what the model wrote there, its words and
    its Edit, is known as the model's. The next request carries them back as the model wrote them: it leaves. A text
    in the model's place that differs by one character from what it wrote is checked as a client's text is."""
    client, provider, _ = gateway
    provider.answer = httpx.Response(
        200, headers={"content-type": content_type}, stream=httpx.ByteStream(body(_answer(SAID, PASSWORD)))
    )
    first = client.post("/anthropic/v1/messages", headers=CLAUDE_CODE, content=_claude_code())
    assert first.status_code == 200 and len(provider.sent) == 1
    provider.answer = httpx.Response(
        200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(b'{"content": []}')
    )
    back = client.post("/anthropic/v1/messages", headers=CLAUDE_CODE, content=_claude_code(edit=PASSWORD, said=SAID))
    assert back.status_code == 200 and len(provider.sent) == 2
    other = SAID.replace("login", "logon")
    changed = client.post(
        "/anthropic/v1/messages", headers=CLAUDE_CODE, content=_claude_code(edit=PASSWORD, said=other)
    )
    assert changed.status_code == 400 and "PASSWORD[7]" in changed.text and len(provider.sent) == 2


# --- another client, on a route Latet only checks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/anthropic/v1/messages", {"user-agent": "some-agent/1.0", "authorization": "Bearer sk-ant-oat01-x"}),
        ("/anthropic/model/m/invoke", {"user-agent": "some-agent/1.0"}),
    ],
    ids=["subscription", "bedrock"],
)
def test_another_clients_assistant_turn_is_checked_as_a_clients_text_is(path, headers):
    """The review's probe: a client other than Claude Code, on a route Latet only checks (a subscription's token, or
    Bedrock), sends an assistant turn holding a token of yours that the vault does not know (a prefill, a resumed
    history): whatever its role, the client wrote it. It is stopped."""
    token = SECRET
    messages = [
        {"role": "user", "content": "continue the deployment notes"},
        {"role": "assistant", "content": f"Deployment notes so far: the CI uses GITHUB_TOKEN={token} for the release."},
        {"role": "user", "content": "go on"},
    ]
    raw = json.dumps({"model": "m", "max_tokens": 10, "messages": messages, "anthropic_version": "x"}).encode()
    prepared = _mediator().prepare(ANTHROPIC, "POST", path, {"content-type": "application/json", **headers}, raw)
    assert prepared.mode == VERIFY
    assert prepared.block_reason is not None and prepared.block_reason.startswith(f"{STOPPED}GITHUB_PAT[40]")
    assert "ASSIGNED_SECRET" not in prepared.block_reason and "GENERIC_API_KEY" not in prepared.block_reason


def test_another_client_gets_nothing_of_what_the_model_wrote_known_in_its_conversation():
    """What the model wrote is Claude Code's alone to send back: another client's conversation that holds the same text
    as one the model wrote there, and its answers, change nothing of its check."""
    mediator = _mediator()
    headers = {"content-type": "application/json", "authorization": "Bearer sk-ant-oat01-x", "x-session-id": "s-9"}
    mediator.verifier.wrote("x-session-id:s-9", ["tests/x.py", PASSWORD])
    stopped = _checked(mediator, _claude_code(edit=PASSWORD), headers)
    assert stopped.block_reason == f"{STOPPED}PASSWORD[7]" and not stopped.claude_code
    assert mediator.written(stopped, "text/event-stream", None) is None  # its answer is not read for the model's words


class _Shared:
    """A store shared by processes, as a Latet server's is: what it was told, in order."""

    def __init__(self) -> None:
        self.told: list[bytes] = []

    def remember(self, key: bytes, masked: str) -> None:
        self.told.append(key)

    def recall(self, key: bytes) -> str | None:
        return "w" if key in self.told else None


def test_what_the_model_wrote_found_here_has_its_shared_record_written_again_once_a_minute():
    """Found in this process's memory, a text the model wrote has its record in the store the other instances read
    written again, never waited for, at most once a minute: a conversation this instance alone relays keeps it alive
    for them."""
    verifier, shared, now = _mediator().verifier, _Shared(), [1000.0]
    verifier.shared = shared  # type: ignore[assignment]
    verifier.clock = lambda: now[0]
    verifier.wrote("cc:s-1", [PASSWORD])
    assert len(shared.told) == 1
    now[0] += 59
    assert verifier.written("cc:s-1", PASSWORD) and len(shared.told) == 1  # within the minute: not again
    now[0] += 1
    assert verifier.written("cc:s-1", PASSWORD) and len(shared.told) == 2  # a minute on: written again
    assert verifier.written("cc:s-1", PASSWORD) and len(shared.told) == 2  # and not again within the next
    assert not verifier.written("cc:s-1", "x = 1\n") and len(shared.told) == 2
