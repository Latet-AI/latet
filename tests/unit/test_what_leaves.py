"""What a request carries when it leaves is what Latet read and checked.

A JSON document that gives a key twice is read once, with the last copy, as Python's json reads it: it never leaves with
the copy Latet did not read. And a request the gateway rewrote is checked before it leaves, as one it may only check
is: what its masking left in clear (a card number written as a JSON number, which the balanced profile does not mask)
stops it. A JSON document's keys are masked as its values are (tests/unit/test_json_keys.py)."""

import asyncio
import base64
import json
import sys
from functools import partial

import pytest

from latet.bridge.api import Bridge
from latet.config import Config, IdentityConfig, LiveReview, TermConfig
from latet.engine import wellformed
from latet.engine.build import build
from latet.engine.types import EntityClass, SlotKind
from latet.engine.vault import Vault
from latet.gateway.mediate import _CHECK_BYTES, REWRITE, VERIFY, Mediator
from latet.gateway.routes import Upstream
from latet.protocols.base import map_json_values

from ..conftest import ROOT_KEY
from .test_reviewer import FakeServer, send
from .test_reviewer import mediator as reviewing
from .test_reviewer import request as reviewed_request
from .test_reviewer import reviewer as reviewer_of

SECRET = "ghp_" + "Zq3vR8kL2mN5pT7wX9yB4cD6fH1jK0sA3eG5"  # a GitHub token's shape
EMAIL = "emily.carter@acme.com"
PHONE = "+39 347 555 0192"
REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
OPENAI = Upstream("openai", "/openai", "http://127.0.0.1:1")
ANTHROPIC = Upstream("anthropic", "/anthropic", "http://127.0.0.1:1")
BEARER = {"authorization": "Bearer sk-test"}
CLAUDE_CODE = {
    "content-type": "application/json",
    "user-agent": "claude-cli/2.1.291 (external, cli)",
    "x-claude-code-session-id": "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11",
}


def _object(*pairs: tuple[str, object]) -> str:
    """A JSON object's text, its keys in this order: a key given twice stays twice (``json.dumps`` keeps one)."""
    return "{" + ", ".join(f"{json.dumps(key)}: {json.dumps(value)}" for key, value in pairs) + "}"


# review 6's probe: a tool's output that gives the token and the address, then gives each key again
TWICE = _object(("token", SECRET), ("owner", EMAIL), ("token", "redacted"), ("owner", "nobody"))
# a request's messages given twice: Latet reads the last copy
MESSAGES_TWICE = (
    ("messages", [{"role": "user", "content": f"mail {EMAIL}"}]),
    ("messages", [{"role": "user", "content": "hello"}]),
)


def _mediator(profile: str = "balanced", **settings) -> Mediator:
    built = build(Config(profile=profile, **settings), Vault(ROOT_KEY))
    return Mediator(built.engine, built.verifier)


def _chat(output: str) -> bytes:
    """A chat completions request, its tool's output ``output``."""
    call = {"id": "c1", "type": "function", "function": {"name": "query", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "list the users"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": output},
    ]
    return json.dumps({"model": "m", "messages": messages}).encode()


def _sent_chat(prepared) -> str:
    return json.loads(prepared.body)["messages"][2]["content"]


# --- a key given twice ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "doc"),
    [
        ('{"token": "a", "token": "b"}', {"token": "b"}),
        ('{"outer": {"k": 1, "k": 2}, "other": 3}', {"outer": {"k": 2}, "other": 3}),
        ('[{"x": 1}, {"y": 1, "y": 2}]', [{"x": 1}, {"y": 2}]),
        # two halves of a character, each read as U+FFFD: two keys as written, one once read
        ('{"k\\ud83d": "first", "k\\ude00": "last"}', {f"k{REPLACEMENT}": "last"}),
    ],
)
def test_a_document_that_gives_a_key_twice_is_read_with_its_last_copy_and_says_so(raw, doc):
    read = wellformed.read(raw)
    assert read.repeated and read.doc == doc
    assert wellformed.read(raw.encode()).repeated  # bytes too, as a request's body comes


def test_a_key_in_two_objects_is_given_once_in_each():
    read = wellformed.read('{"a": 1, "b": {"a": 2}, "c": [{"a": 3}, {"a": 4}]}')
    assert not read.repeated and not read.mended
    assert wellformed.read('{"t": "done \\ud83d"}').mended


def test_a_document_that_gives_a_key_twice_is_written_anew_with_the_copy_read():
    """When nothing in it is masked, a document read one to one leaves byte for byte; one that gives a key twice is
    written anew from what was read, so that the copy Latet did not read never leaves."""
    same = '{"a": "x",  "b": "y"}'
    assert map_json_values(same, lambda value, path, number: value) == same
    assert map_json_values(TWICE, lambda value, path, number: value) == '{"token":"redacted","owner":"nobody"}'
    mended = _object(("k" + chr(0xD83D), SECRET), ("k" + chr(0xDE00), "x"))  # two keys as written, one once read
    assert "\\ud83d" in mended and SECRET not in map_json_values(mended, lambda value, path, number: value)


@pytest.mark.parametrize("profile", ["balanced", "max"])
def test_a_tool_output_that_gives_a_key_twice_leaves_with_the_copy_latet_read_alone(profile):
    prepared = _mediator(profile).prepare(OPENAI, "POST", "/openai/v1/chat/completions", BEARER, _chat(TWICE))
    sent = _sent_chat(prepared)
    assert SECRET not in prepared.body.decode() and EMAIL not in prepared.body.decode()
    assert prepared.mode == REWRITE and prepared.block_reason is None  # sent, the copy it did not read left out
    assert sorted(json.loads(sent)) == ["owner", "token"] and sent.count('"token"') == 1
    if profile == "balanced":  # strict data mode masks every value of it in max
        assert json.loads(sent) == {"token": "redacted", "owner": "nobody"}


def test_a_request_body_that_gives_a_key_twice_is_written_anew_with_the_copy_latet_read():
    raw = _object(("model", "m"), *MESSAGES_TWICE).encode()
    prepared = _mediator().prepare(OPENAI, "POST", "/openai/v1/chat/completions", BEARER, raw)
    assert prepared.block_reason is None and EMAIL.encode() not in prepared.body
    assert json.loads(prepared.body) == {"model": "m", "messages": [{"role": "user", "content": "hello"}]}


def test_a_websocket_turn_that_gives_a_key_twice_is_written_anew_with_the_copy_latet_read():
    mediator = _mediator()
    frame = _object(("type", "response.create"), ("model", "m"), ("input", f"mail {EMAIL}"), ("input", "hello"))
    text, prepared = mediator.mask_ws_frame(frame, mediator.engine.session("session-id:s-1"))
    assert prepared is not None and prepared.block_reason is None
    assert json.loads(text) == {"type": "response.create", "model": "m", "input": "hello"} and EMAIL not in text


def test_a_claude_code_request_that_gives_a_key_twice_is_stopped():
    """A request Latet only checks leaves as it came: the copy it did not read would leave with it."""
    raw = _object(("model", "m"), ("max_tokens", 5), *MESSAGES_TWICE).encode()
    prepared = _mediator().prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY and "twice" in (prepared.block_reason or "")


def test_a_bedrock_token_count_whose_request_gives_a_key_twice_is_stopped():
    inner = _object(("max_tokens", 5), *MESSAGES_TWICE)
    raw = json.dumps({"input": {"invokeModel": {"body": base64.b64encode(inner.encode()).decode()}}}).encode()
    path = "/bedrock/us-east-1/model/us.anthropic.claude-opus-5-5/count-tokens"
    prepared = _mediator().prepare(ANTHROPIC, "POST", path, {"content-type": "application/json"}, raw)
    assert prepared.mode == VERIFY and prepared.block_reason is not None


# --- what a rewritten request carries, checked before it leaves --------------------------------------------------------

VALUES = json.dumps({"users": [{"who": value, "note": "ok"} for value in (EMAIL, PHONE, SECRET)]})
# a card number written as a JSON number (a test card's): the balanced profile masks no number
CARD = '{"customer": "c-101", "card": 4111111111111111}'
STOPPED = "sensitive values would leave in clear: "


def _responses_input(output: str) -> list[dict]:
    return [
        {"role": "user", "content": "list the users"},
        {"type": "function_call", "call_id": "c1", "name": "query", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": output},
    ]


def _responses(output: str) -> bytes:
    return json.dumps({"model": "m", "input": _responses_input(output)}).encode()


def _anthropic(output: str) -> bytes:
    """An Anthropic request with an API key, not Claude Code's: the gateway rewrites it."""
    call = {"type": "tool_use", "id": "t1", "name": "query", "input": {}}
    messages = [
        {"role": "user", "content": "list the users"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output}]},
    ]
    return json.dumps({"model": "m", "max_tokens": 10, "messages": messages}).encode()


ROUTES = {
    "chat completions": (OPENAI, "/openai/v1/chat/completions", BEARER, _chat),
    "responses": (OPENAI, "/openai/v1/responses", BEARER, _responses),
    "anthropic with an API key": (ANTHROPIC, "/anthropic/v1/messages", {"x-api-key": "sk-ant-api-test"}, _anthropic),
}


def _prepared(mediator: Mediator, route: str, output: str, headers: dict[str, str] | None = None):
    upstream, path, auth, body = ROUTES[route]
    return mediator.prepare(upstream, "POST", path, {**auth, **(headers or {})}, body(output))


@pytest.mark.parametrize("route", list(ROUTES))
def test_a_card_number_written_as_a_json_number_stops_the_request(route):
    """The balanced profile masks no JSON number: a card number written as one is left in clear by masking, and the
    check stops the request before the local reviewer or you are asked anything."""
    prepared = _prepared(_mediator(), route, CARD)
    assert prepared.mode == REWRITE and prepared.block_reason == f"{STOPPED}CREDIT_CARD[16]"
    assert prepared.reviews == []
    assert {leak.preview for leak in prepared.leaks if leak.blocks} == {"CREDIT_CARD[16]"}


def test_a_card_number_written_as_a_json_number_stops_a_websocket_turn():
    mediator = _mediator()
    frame = json.dumps({"type": "response.create", "model": "m", "input": _responses_input(CARD)})
    _, prepared = mediator.mask_ws_frame(frame, mediator.engine.session("session-id:s-1"))
    assert prepared is not None and prepared.block_reason == f"{STOPPED}CREDIT_CARD[16]"


@pytest.mark.parametrize("route", list(ROUTES))
def test_the_same_values_as_the_values_of_a_tool_output_are_masked_and_sent(route):
    prepared = _prepared(_mediator(), route, VALUES)
    assert prepared.block_reason is None and prepared.leaks == []
    assert not any(value in prepared.body.decode() for value in (EMAIL, PHONE, SECRET))


@pytest.mark.parametrize("profile", ["balanced", "max"])
@pytest.mark.parametrize("route", list(ROUTES))
def test_a_value_too_long_to_keep_leaves_redacted_and_the_check_lets_its_request_out(route, profile):
    """A password of 20,000 characters in a tool's output is redacted, too long for a vault to keep: the check of what
    leaves takes neither the marker nor the value it replaced for a value left in clear, and the request leaves."""
    password = (SECRET * 500)[:20_000]
    prepared = _prepared(_mediator(profile), route, json.dumps({"user": "svc", "password": password}))
    assert prepared.mode == REWRITE and prepared.block_reason is None and prepared.leaks == []
    sent = prepared.body.decode()
    assert password not in sent and SECRET not in sent and "[REDACTED:PASSWORD]" in sent


# a field rule of your own that says its values are secrets, and a value of it too long for a vault to keep
SECRET_FIELD = {"fields": [{"name": "private_key", "secret": True}]}
KEY = ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" * 500)[:20_000]


@pytest.mark.parametrize("route", list(ROUTES))
def test_a_value_of_your_own_secret_field_too_long_to_keep_leaves_redacted_and_so_does_the_next_request(route):
    """The re-review's probe: a field rule of your own, ``secret: true``, read the marker that took the place of a value
    too long to keep (``[REDACTED:FIELD]``) as a secret left in clear, and the check stopped the request and every later
    one of its conversation, saying ``FIELD[16]``. The marker is no value, as it is none for the built-in names."""
    mediator = _mediator(**SECRET_FIELD)
    output = json.dumps({"user": "svc", "private_key": KEY})
    for _ in range(2):  # and the conversation's next request, which carries the same output
        prepared = _prepared(mediator, route, output, {"session-id": "s-1"})
        assert prepared.mode == REWRITE and prepared.block_reason is None and prepared.leaks == []
        sent = prepared.body.decode()
        assert KEY not in sent and KEY[:64] not in sent and "[REDACTED:FIELD]" in sent


def test_claude_codes_request_carrying_the_marker_of_your_own_secret_field_leaves():
    """The plugin redacts the same value in Claude Code's tool output: the daemon's final check lets the marker out."""
    output = json.dumps({"user": "svc", "private_key": "[REDACTED:FIELD]"})
    raw = _anthropic(output)
    prepared = _mediator(**SECRET_FIELD).prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY and prepared.block_reason is None and prepared.leaks == []
    stopped = _mediator(**SECRET_FIELD).prepare(
        ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, _anthropic(json.dumps({"private_key": KEY[:200]}))
    )
    assert (stopped.block_reason or "").startswith(STOPPED)  # a value of the field in clear is still stopped


# A secret field rule of your own whose type is written in any characters (accents, a slash): its marker,
# ``[REDACTED:<the type in capitals>]``, is Latet's all the same.
E_ACUTE, CAPITAL_E_ACUTE = "\N{LATIN SMALL LETTER E WITH ACUTE}", "\N{LATIN CAPITAL LETTER E WITH ACUTE}"
TYPED_SECRET_FIELD = {"fields": [{"name": "private_key", "secret": True, "type": f"Cl{E_ACUTE} priv{E_ACUTE}e/RSA"}]}
TYPED_MARKER = f"[REDACTED:CL{CAPITAL_E_ACUTE} PRIV{CAPITAL_E_ACUTE}E/RSA]"


@pytest.mark.parametrize("route", list(ROUTES))
def test_a_value_of_your_own_secret_field_of_any_type_too_long_to_keep_leaves_redacted(route):
    """The gateway's re-check (RG2, a Minor): the marker was skipped only for a type of capitals, digits and a few
    marks, so the marker of a type of yours with an accent or a slash was taken for a secret left in clear, and its
    request stopped, and the next. Whatever its type, ``[REDACTED:`` up to the ``]`` that closes it is Latet's."""
    mediator = _mediator(**TYPED_SECRET_FIELD)
    output = json.dumps({"user": "svc", "private_key": KEY})
    for _ in range(2):  # and the conversation's next request, which carries the same output
        prepared = _prepared(mediator, route, output, {"session-id": "s-1"})
        assert prepared.mode == REWRITE and prepared.block_reason is None and prepared.leaks == []
        assert KEY not in prepared.body.decode() and KEY[:64] not in prepared.body.decode()


def test_claude_codes_request_carrying_the_marker_of_your_own_secret_field_of_any_type_leaves():
    raw = _anthropic(json.dumps({"user": "svc", "private_key": TYPED_MARKER}))
    prepared = _mediator(**TYPED_SECRET_FIELD).prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY and prepared.block_reason is None and prepared.leaks == []
    for value in ("[REDACTED:]", "[REDACTED:CL]E]", "x[REDACTED:FIELD]"):  # no marker: a value of yours, stopped
        raw = _anthropic(json.dumps({"user": "svc", "private_key": value}))
        stopped = _mediator(**TYPED_SECRET_FIELD).prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
        assert (stopped.block_reason or "").startswith(STOPPED), value


# Values of a secret field of yours that look like placeholders to the built-in names (``_PLACEHOLDER``): a password that
# starts with `$`, one that starts with `your-`, one in angle or square brackets. Each is a value of yours all the same.
LOOK_LIKE_PLACEHOLDERS = ["$Pa55word", "your-token", "<secret>", "[secret]"]


@pytest.mark.parametrize("value", LOOK_LIKE_PLACEHOLDERS)
@pytest.mark.parametrize("route", list(ROUTES))
def test_a_value_of_your_own_secret_field_that_looks_like_a_placeholder_is_masked(route, value):
    """The residuals' check: the skip of the marker took the built-in names' whole pattern of placeholders, so these
    values of a secret field rule of your own were neither masked nor checked. Latet's own marker alone is skipped."""
    prepared = _prepared(_mediator(**SECRET_FIELD), route, json.dumps({"user": "svc", "private_key": value}))
    assert prepared.mode == REWRITE and prepared.block_reason is None and prepared.masked_count >= 1
    assert value not in prepared.body.decode() and json.dumps(value) not in prepared.body.decode()


@pytest.mark.parametrize("value", LOOK_LIKE_PLACEHOLDERS)
def test_claude_codes_request_carrying_a_value_of_your_own_secret_field_that_looks_like_a_placeholder_is_stopped(value):
    raw = _anthropic(json.dumps({"user": "svc", "private_key": value}))
    prepared = _mediator(**SECRET_FIELD).prepare(ANTHROPIC, "POST", "/anthropic/v1/messages", CLAUDE_CODE, raw)
    assert prepared.mode == VERIFY and (prepared.block_reason or "").startswith(STOPPED)


def test_a_tool_output_that_left_before_is_said_and_not_stopped_when_its_conversation_sends_it_again():
    """A running conversation is not stopped by its past: a text recorded as left (``Mediator.left``, once its request
    reached its provider) leaves again, the value in clear in it said, never stopped."""
    mediator = _mediator()
    first = _prepared(mediator, "chat completions", CARD, {"session-id": "s-1"})
    assert (first.block_reason or "").startswith(STOPPED)
    mediator.record_left(first)  # as a request that left before the check existed
    again = _prepared(mediator, "chat completions", CARD, {"session-id": "s-1"})
    assert again.block_reason is None and again.leaks and not any(leak.new for leak in again.leaks)


def test_the_check_records_nothing_as_left_before_the_request_has_left():
    """The gateway records what a request carried once its provider has answered (``Mediator.left``): the check before
    it leaves records nothing, or a text whose request never left would leave again as it was kept."""
    mediator = _mediator()
    prepared = _prepared(mediator, "chat completions", "the status page is up", {"session-id": "s-1"})
    assert prepared.block_reason is None
    assert not mediator.verifier.sent("session-id:s-1", "the status page is up")
    mediator.record_left(prepared)
    assert mediator.verifier.sent("session-id:s-1", "the status page is up")


def test_a_type_your_policy_lets_through_is_not_stopped_as_a_key_either():
    mediator = _mediator(policies={"EMAIL": "allow"})
    output = json.dumps({"users": {EMAIL: {"note": "ok"}}})
    prepared = _prepared(mediator, "chat completions", output)
    assert prepared.block_reason is None and EMAIL in _sent_chat(prepared)


def test_a_word_inside_a_fake_is_not_taken_for_a_value_left_in_clear():
    """A fake drawn for one value can hold a word that is also one of your terms, or your user name (Daniel Jordan for
    Robert Johnson, beside the term Jordan and the user daniel): nothing of yours is in it, and the request leaves."""
    config = Config(
        terms=[TermConfig(value="Jordan", type="CLIENT")],
        identity=IdentityConfig(usernames=["daniel"], username="daniel"),
    )
    vault = Vault(ROOT_KEY)
    built = build(config, vault, identity=config.identity)
    vault.intern("PERSON", EntityClass.PII, "Robert Johnson", generate=lambda original, drbg: "Daniel Jordan")
    prepared = _prepared(Mediator(built.engine, built.verifier), "chat completions", "opened by Robert Johnson")
    assert _sent_chat(prepared) == "opened by Daniel Jordan" and prepared.block_reason is None


def test_what_the_model_wrote_is_not_stopped_when_its_conversation_sends_it_back():
    """The model's own words come back as it wrote them (the provider's own bytes, restored for the agent and found
    again): a password it made up for an example is not a value of yours, and its conversation goes on."""
    mediator = _mediator()
    headers = {**BEARER, "session-id": "s-1"}
    asked = {"model": "m", "messages": [{"role": "user", "content": f"write to {EMAIL}"}]}
    first = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", headers, json.dumps(asked).encode())
    fake = json.loads(first.body)["messages"][0]["content"].removeprefix("write to ")
    wrote = f'I wrote to {fake}, and the test reads:\npassword = "hunter2"\n'
    mediator.engine.remember_model_output(wrote, wrote.replace(fake, EMAIL))  # as the answer's restore does
    history = [*asked["messages"], {"role": "assistant", "content": wrote.replace(fake, EMAIL)}]
    body = {"model": "m", "messages": [*history, {"role": "user", "content": "thanks"}]}
    again = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", headers, json.dumps(body).encode())
    assert again.block_reason is None and json.loads(again.body)["messages"][1]["content"] == wrote


def test_the_two_checks_read_the_models_own_text_each_its_way():
    """The check of a rewritten request reads none of the model's texts (each is masked again, or sent back as the
    model wrote it). The final check of a request Latet only checks reads the texts in the model's place as a client's
    texts, but for what the model wrote, exactly so, in that conversation (``written``)."""
    vault = Vault(ROOT_KEY)
    built = build(Config(), vault)
    vault.intern("GITHUB_PAT", EntityClass.SECRET, SECRET)
    text = [(f"TOKEN = {SECRET}", SlotKind.TOOL_INPUT, True)]
    assert not built.verifier.verify(text, "s-1", record=False, rewritten=True).leaks
    # one value, found three ways (the vault's, the pair's, gitleaks' rules): one leak, named by the specific rule
    assert [leak.preview for leak in built.verifier.verify(text, "s-2").blocking] == ["GITHUB_PAT[40]"]
    built.verifier.wrote("cc:s-3", [f"TOKEN = {SECRET}"])
    mine = partial(built.verifier.written, "cc:s-3")
    assert not built.verifier.verify(text, "s-3", written=mine).leaks
    elsewhere = partial(built.verifier.written, "cc:s-4")  # another conversation: the model did not write it there
    assert built.verifier.verify(text, "s-4", written=elsewhere).blocking
    yours = [(f"TOKEN = {SECRET}", SlotKind.TOOL_RESULT, False)]  # the same text, a tool's output: a client's text
    assert built.verifier.verify(yours, "s-5", written=mine).blocking


def test_the_memory_of_texts_found_clean_is_weighed():
    """The final check now reads every request the gateway rewrites, and keeps what it found clean: a server weighs
    that memory with the space (``Mediator.checked_bytes``), its entries all of one size, not less than they take."""
    mediator = _mediator()
    _prepared(mediator, "chat completions", VALUES)
    memory = mediator.verifier._clean._data
    found, one = len(memory), mediator.verifier.cached_bytes
    assert found > 0 and one % found == 0
    assert mediator.checked_bytes == len(mediator._as_it_left) * _CHECK_BYTES + one
    for n in range(20_000):  # as full as a busy space's: keys of 32 bytes, vault versions past the small numbers
        mediator.verifier._clean.put(("", True, n.to_bytes(32, "big")), 1_000_000 + n)
    taken = sys.getsizeof(memory) + sum(
        sys.getsizeof(key) + sys.getsizeof(key[2]) + sys.getsizeof(value) for key, value in memory.items()
    )
    weighed = mediator.verifier.cached_bytes
    assert weighed == one // found * len(memory) and weighed >= taken


# --- the reviewers' masks, checked gone ---------------------------------------------------------------------------------


async def test_what_the_local_reviewer_found_is_checked_gone_once_masked_again():
    """The local reviewer reads a JSON output's strings: an amount it finds there is masked in them, and the same amount
    written as a JSON number, which the balanced profile does not mask, would leave in clear. The request is stopped,
    as one is for a value masked in the Live Reviewer. (A name it finds is masked in the keys too: test_json_keys.)"""
    output = json.dumps({"salary": 95000, "note": "a salary of 95000 was confirmed"})
    server = FakeServer({"95000": "financial"})
    mediator, raw = reviewing(server), reviewed_request(output, prompt="read the log")
    for attempt in range(3):  # sent again, its review remembered: the text never left, the answer is applied again
        prepared = await send(mediator, raw)
        assert prepared.block_reason == "masking failed: a value the local reviewer found would have left in clear"
        assert attempt == 0 or len(server.texts) == 2, attempt  # read once: what it found, it found then


async def test_what_the_local_reviewer_found_in_clear_once_masked_again_is_withheld_from_the_plugin():
    """The plugin's path: the tool's output it has the daemon mask, the amount found in its strings and written as a
    JSON number too. The plugin gets the output withheld, never with the amount in clear; and so again when it has the
    same output masked again, its review remembered."""
    output = json.dumps({"salary": 95000, "note": "a salary of 95000 was confirmed"})
    server = FakeServer({"95000": "financial"})
    mediator = reviewing(server)
    bridge = Bridge(mediator, "token")
    data = {"session": "s-1", "items": [{"text": output, "kind": "tool_result", "tool": "Bash"}]}
    for attempt in range(2):
        session, out, degraded, reviews, _ = await mediator.run_bridge(bridge._mask, data)
        await bridge._review(session, reviews, out, degraded)
        assert out[0].get("blocked") == "a value the local reviewer found would have left in clear", attempt
        assert "95000" not in out[0]["text"] and len(server.texts) == 1, attempt


async def test_what_the_local_reviewer_found_is_left_as_it_is_where_your_policy_lets_its_type_through():
    config = Config(llm_review={"enabled": True, "model": "fake-model"}, policies={"PERSON": "allow"})
    built = build(config, Vault(ROOT_KEY))
    mediator = Mediator(built.engine, built.verifier)
    mediator.llm_review = config.llm_review
    mediator.reviewer = reviewer_of(FakeServer({"Ferri": "person"}), config.llm_review)
    output = json.dumps({"Ferri": {"note": "the customer Ferri called"}})
    prepared = await send(mediator, reviewed_request(output, prompt="read the log"))
    assert prepared.block_reason is None


async def test_a_secret_the_local_reviewer_finds_is_checked_gone_from_the_rest_of_the_request():
    """With a time limit, this turn is reviewed before it leaves and the history after: a secret found in this turn,
    in clear in a text of the history that never left, stops the request."""
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "the code word is tiramisu42"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "now use tiramisu42"},
        ],
    }
    mediator = reviewing(FakeServer({"tiramisu42": "secret"}), budget_ms=5000)
    prepared = await send(mediator, json.dumps(body).encode())
    assert prepared.block_reason == f"{STOPPED}SECRET[10]"


async def test_a_value_you_mask_in_the_live_reviewer_is_checked_gone_from_the_rest_of_the_request():
    """What waits for you is this turn's: a value you mask there, in clear in a text of the history that never left,
    stops the request."""
    mediator = _mediator()
    mediator.live_review = LiveReview(enabled=True)
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "the code word is tiramisu42"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "now use tiramisu42"},
        ],
    }
    prepared = mediator.prepare(OPENAI, "POST", "/openai/v1/chat/completions", BEARER, json.dumps(body).encode())
    assert prepared.block_reason is None
    feed = await mediator.approvals.watch()  # as a dashboard watches the queue
    waiting = asyncio.ensure_future(mediator.live_prepared(prepared, "codex"))
    while (told := await asyncio.wait_for(feed.get(), 10))[0] != "pending":
        pass
    batch = told[1]
    assert [item["original"] for item in batch["items"]] == ["now use tiramisu42"]  # this turn's text alone
    mask = {"item": 0, "start": 8, "end": 18, "category": "secret"}
    assert await mediator.approvals.decide(batch["id"], True, [mask])
    decided = await asyncio.wait_for(waiting, 10)
    assert decided.block_reason == f"{STOPPED}SECRET[10]"
