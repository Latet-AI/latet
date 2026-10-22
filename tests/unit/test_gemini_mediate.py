"""A Gemini request through the mediator: masked where Latet reads it, refused where it does not, one conversation's
requests kept together, and its project known from Gemini CLI's own words."""

import json

import pytest

from latet.gateway.mediate import PASSTHROUGH, REWRITE, UNREAD_HERE, _session_key, cwd_from
from latet.gateway.outbound import server_calls
from latet.gateway.routes import Upstream
from latet.protocols import gemini

from .test_what_leaves import EMAIL, OPENAI, SECRET, _mediator

GEMINI = Upstream("gemini", "/gemini", "http://127.0.0.1:1", refuses_unread=True)
CALL = "/gemini/v1beta/models/gemini-3-pro:generateContent"
KEY = {"x-goog-api-key": "AIzaSyFAKE"}


def _request(*contents: dict, system: str = "You are Gemini CLI.", **extra) -> bytes:
    return json.dumps(
        {"systemInstruction": {"parts": [{"text": system}]}, "contents": list(contents), **extra}
    ).encode()


def _user(text: str) -> dict:
    return {"role": "user", "parts": [{"text": text}]}


def test_a_gemini_request_is_masked():
    body = _request(
        _user(f"write to {EMAIL}"),
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"path": "notes.txt"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "read_file", "response": {"output": SECRET}}}]},
        tools=[{"functionDeclarations": [{"name": "send", "description": f"Sends mail as {EMAIL}"}]}],
    )
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, body)
    assert (prepared.mode, prepared.protocol, prepared.block_reason) == (REWRITE, "gemini", None)
    assert EMAIL not in prepared.body.decode() and SECRET not in prepared.body.decode()
    sent = json.loads(prepared.body)
    assert sent["contents"][2]["parts"][0]["functionResponse"]["name"] == "read_file"


@pytest.mark.parametrize(
    "path",
    [
        "/gemini/v1beta/models/g%3AstreamGenerateContent",
        "/gemini/v1beta/models/g:countTokens/",
        "/vertex/v1beta1/projects/p/locations/us/publishers/google/models/g:generateContent",
        "/gemini/v1beta/cachedContents",
    ],
)
def test_every_call_latet_reads_is_masked(path):
    prepared = _mediator().prepare(GEMINI, "POST", path, KEY, _request(_user(f"write to {EMAIL}")))
    assert prepared.protocol == "gemini" and EMAIL not in prepared.body.decode()


def test_an_update_of_a_cache_is_read():
    prepared = _mediator().prepare(GEMINI, "PATCH", "/gemini/v1beta/cachedContents/abc", KEY, b'{"ttl": "600s"}')
    assert prepared.protocol == "gemini" and prepared.block_reason is None


def test_embeddings_are_masked():
    body = json.dumps({"content": {"parts": [{"text": f"mail {EMAIL}"}]}, "title": EMAIL}).encode()
    prepared = _mediator().prepare(GEMINI, "POST", "/gemini/v1beta/models/e:embedContent", KEY, body)
    assert prepared.protocol == "gemini" and EMAIL not in prepared.body.decode()


def test_a_body_latet_does_not_read_is_refused_on_gemini_even_on_a_computer():
    body = json.dumps({"model": "gemini-3-pro", "input": f"write to {EMAIL}"}).encode()
    refused = _mediator().prepare(GEMINI, "POST", "/gemini/v1beta/interactions", KEY, body)
    assert refused.block_reason == UNREAD_HERE
    listed = _mediator().prepare(GEMINI, "GET", "/gemini/v1beta/models", KEY, b"")
    assert (listed.mode, listed.block_reason) == (PASSTHROUGH, None)
    elsewhere = _mediator().prepare(OPENAI, "POST", "/openai/v1/files", {}, body)
    assert (elsewhere.mode, elsewhere.block_reason) == (PASSTHROUGH, None)  # today's rule, on the other routes


def test_a_server_refuses_it_in_its_own_words():
    mediator = _mediator()
    mediator.on_server = True
    body = json.dumps({"input": "x"}).encode()
    refused = mediator.prepare(GEMINI, "POST", "/gemini/v1beta/interactions", KEY, body)
    assert refused.block_reason is not None and refused.block_reason != UNREAD_HERE


def test_a_conversation_keeps_its_session():
    mediator = _mediator()
    first = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user("hello")))
    longer = _request(_user("hello"), {"role": "model", "parts": [{"text": "hi"}]}, _user("more"))
    second = mediator.prepare(GEMINI, "POST", CALL, KEY, longer)
    other = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user("another opening")))
    assert first.session is not None and second.session is not None and other.session is not None
    assert first.session.key == second.session.key != other.session.key


def test_the_other_protocols_keys_do_not_move():
    body = {"system": "s", "messages": [{"role": "user", "content": "hello"}]}
    assert _session_key("anthropic", {}, body, lambda _: None) == "conv:f1eb1d00d1c844273ba2c1ef"
    responses = {"instructions": "i", "input": [{"role": "user", "content": "hello"}]}
    assert _session_key("responses", {}, responses, lambda _: None) == "conv:b1292276feb765dcd9a59c95"


def test_gemini_clis_working_directory_names_the_project():
    context = (
        "This is the Gemini CLI. We are setting up the context for our chat.\n"
        "Today's date is Monday, October 20, 2026.\n"
        "My operating system is: win32\n"
        "I'm currently working in the directory: C:\\work\\acme\n"
    )
    scan = gemini.scan_request({"contents": [_user(context)]})
    assert cwd_from({}, scan) == "C:\\work\\acme"


def test_a_server_refuses_an_unreadable_part():
    body = _request({"role": "user", "parts": [{"text": f"to {EMAIL}"}, {"weird": 1}]})
    mediator = _mediator()
    mediator.on_server = True
    refused = mediator.prepare(GEMINI, "POST", CALL, KEY, body)
    assert (
        refused.block_reason is not None and "a part of content of a kind Latet does not read" in refused.block_reason
    )
    sent = _mediator().prepare(GEMINI, "POST", CALL, KEY, body)
    assert sent.block_reason is None and EMAIL not in sent.body.decode()


def test_a_tool_the_provider_ran_with_a_stand_in_is_said():
    mediator = _mediator()
    first = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"find {EMAIL}")))
    fake = json.loads(first.body)["contents"][0]["parts"][0]["text"].removeprefix("find ")
    assert fake != EMAIL
    searched = {"role": "model", "parts": [{"toolCall": {"toolName": "google_search", "args": {"query": fake}}}]}
    prepared = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"find {EMAIL}"), searched))
    assert prepared.outbound == ["google_search"]
    assert [tool for tool, _ in server_calls("gemini", json.loads(prepared.body))] == ["google_search"]


@pytest.mark.parametrize(
    "path",
    [
        "/gemini/v1beta/models/gemini-3-pro:generateContent",
        "/gemini/v1beta/models/gemini-3-pro%3AstreamGenerateContent",
        "/vertex/v1beta1/projects/p/locations/us/publishers/google/models/gemini-3-pro:streamGenerateContent",
    ],
)
def test_the_model_named_in_the_path_is_the_calls_model(path):
    prepared = _mediator().prepare(GEMINI, "POST", path, KEY, _request(_user("hello")))
    assert prepared.model == "gemini-3-pro"


def test_a_token_count_of_a_whole_request_is_of_its_conversation():
    mediator = _mediator()
    talk = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user("hello")))
    counted = json.dumps({"generateContentRequest": json.loads(_request(_user("hello"), model="models/g"))}).encode()
    count = mediator.prepare(GEMINI, "POST", "/gemini/v1beta/models/g:countTokens", KEY, counted)
    assert talk.session is not None and count.session is not None and count.session.key == talk.session.key
