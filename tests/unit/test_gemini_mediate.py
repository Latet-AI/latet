"""A Gemini request through the mediator: masked where Latet reads it, refused where it does not, one conversation's
requests kept together, and its project known from Gemini CLI's own words."""

import json

import pytest

from latet.config import TermConfig
from latet.gateway.mediate import PASSTHROUGH, REWRITE, _session_key, cwd_from
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
    assert refused.block_reason is not None and "Google's Interactions API" in refused.block_reason
    assert "server" not in refused.block_reason  # true on a computer as on a server
    listed = _mediator().prepare(GEMINI, "GET", "/gemini/v1beta/models", KEY, b"")
    assert (listed.mode, listed.block_reason) == (PASSTHROUGH, None)
    elsewhere = _mediator().prepare(OPENAI, "POST", "/openai/v1/files", {}, body)
    assert elsewhere.block_reason == refused.block_reason  # one rule, in one wording, on every route


def test_a_server_refuses_it_in_the_same_words():
    """One code path decides an unread endpoint on both machines, so a server says what a computer says."""
    mediator = _mediator()
    mediator.on_server = True
    body = json.dumps({"input": "x"}).encode()
    refused = mediator.prepare(GEMINI, "POST", "/gemini/v1beta/interactions", KEY, body)
    computer = _mediator().prepare(GEMINI, "POST", "/gemini/v1beta/interactions", KEY, body)
    assert refused.block_reason is not None and refused.block_reason == computer.block_reason


def test_what_latet_does_not_read_is_refused_in_words_true_on_a_computer():
    """Each refusal of what Latet does not read (an endpoint, a method, a body that is no object, content of a kind it
    does not read, a WebSocket frame) is given on a computer too, so none of them speaks of a server alone."""
    mediator = _mediator()
    reasons = [
        mediator.prepare(GEMINI, "POST", "/gemini/v1beta/interactions", KEY, b'{"input": "x"}').block_reason,
        mediator.prepare(GEMINI, "PUT", "/gemini/v1beta/interactions", KEY, b'{"input": "x"}').block_reason,
        mediator.prepare(OPENAI, "POST", "/openai/v1/files", {}, b"[1]").block_reason,
        mediator.prepare(GEMINI, "POST", CALL, KEY, _request({"role": "user", "parts": [{"weird": 1}]})).block_reason,
        mediator.unread_frame("a binary frame", mediator.engine.session("frames")).block_reason,  # type: ignore[union-attr]
    ]
    assert all(reasons) and not [reason for reason in reasons if "server" in (reason or "")], reasons


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


def test_an_unknown_member_of_a_part_is_refused_on_either_machine():
    """One code path, the same on a computer and a server (R-116): the mediator has no machine to ask. A part's member
    no field names is refused as an unknown member, under its safe code, and nothing is sent (P2-8a, root's ruling 6)."""
    body = _request({"role": "user", "parts": [{"text": f"to {EMAIL}"}, {"weird": 1}]})
    refused = _mediator().prepare(GEMINI, "POST", CALL, KEY, body)
    assert refused.block_reason is not None and "unknown member" in refused.block_reason
    assert refused.envelope_refusal == "body-unknown-member"
    assert refused.body == b""


def test_a_tool_the_provider_ran_with_a_stand_in_is_said():
    mediator = _mediator()
    first = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"find {EMAIL}")))
    fake = json.loads(first.body)["contents"][0]["parts"][0]["text"].removeprefix("find ")
    assert fake != EMAIL
    searched = {"role": "model", "parts": [{"toolCall": {"toolName": "google_search", "args": {"query": fake}}}]}
    prepared = mediator.prepare(GEMINI, "POST", CALL, KEY, _request(_user(f"find {EMAIL}"), searched))
    assert prepared.outbound == ["google_search"]
    assert [tool for tool, _ in server_calls("gemini", json.loads(prepared.body))] == ["google_search"]


# --- the parts a provider ran: its built-in tools (Google Search, URL context, Maps, File Search) and its code -------

# Every field google-genai (a dev dependency: the SDK Google's own clients are built on) gives the parts the provider
# ran, each with a value of its kind; ``toolName`` is in the API's REST reference alone.
PROVIDER_PARTS = {
    "toolCall": {"id": "t1", "toolType": "GOOGLE_SEARCH_WEB", "toolName": "google_search", "args": {"query": EMAIL}},
    "toolResponse": {"id": "t1", "toolType": "GOOGLE_SEARCH_WEB", "response": {"results": [f"mail {EMAIL}"]}},
    "executableCode": {"id": "c1", "language": "PYTHON", "code": f"print('{EMAIL}')"},
    "codeExecutionResult": {"id": "c1", "outcome": "OUTCOME_OK", "output": f"{EMAIL}\n"},
}


def _snake(value):
    """A part as Google's Python SDK writes one inside a body: its fields under their snake_case names."""
    if isinstance(value, dict):
        return {gemini.snake(key): item if key in ("args", "response") else _snake(item) for key, item in value.items()}
    return value


def _ran(*parts: dict) -> bytes:
    return _request(_user("look it up"), {"role": "model", "parts": list(parts)})


def test_the_parts_a_provider_ran_are_the_ones_its_sdk_gives():
    """Drift: the parts the provider ran and their fields, as the SDK Latet is tested with declares them, are those the
    protocol module reads and the body's grammar declares, and every value their kinds take is a token of the grammar's
    bounded form (``gemini.KIND``)."""
    from google.genai import types

    models = {
        "toolCall": types.ToolCall,
        "toolResponse": types.ToolResponse,
        "executableCode": types.ExecutableCode,
        "codeExecutionResult": types.CodeExecutionResult,
    }
    assert set(gemini._PROVIDERS) == set(models) == set(PROVIDER_PARTS)
    assert {gemini.snake(part) for part in models} <= set(types.Part.model_fields)
    for part, model in models.items():
        documented = {gemini.snake(name) for name in PROVIDER_PARTS[part]} - {"tool_name"}
        assert set(model.model_fields) == documented, part
    kinds = [kind.value for enum in (types.ToolType, types.Language, types.Outcome) for kind in enum]
    assert kinds and [kind for kind in kinds if not gemini.KIND.fullmatch(kind)] == []


@pytest.mark.parametrize("written", ["camelCase", "snake_case"])
def test_the_parts_a_provider_ran_are_read_and_masked(written):
    """As a function's call and response are: the input and output of a tool the provider ran, its code and what the
    code printed are masked, and each part keeps its kind, its id and its tool."""
    parts = [{name: value} for name, value in json.loads(json.dumps(PROVIDER_PARTS)).items()]
    if written == "snake_case":
        parts = [_snake(part) for part in parts]
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, _ran(*parts))
    assert (prepared.mode, prepared.block_reason) == (REWRITE, None)
    assert EMAIL not in prepared.body.decode()
    sent = json.loads(prepared.body)["contents"][1]["parts"]
    kept = [{key: value for key, value in next(iter(part.values())).items() if key not in ("args", "response")}
            for part in sent]  # fmt: skip
    assert [list(part) for part in sent] == [list(part) for part in parts]
    assert [part.get("id") for part in kept] == ["t1", "t1", "c1", "c1"]
    assert prepared.outbound == ["google_search"]  # the search ran with the stand-in the model knew


@pytest.mark.parametrize(
    "part",
    [
        {"toolCall": {**PROVIDER_PARTS["toolCall"], "weird": "x"}},
        {"toolCall": {**PROVIDER_PARTS["toolCall"], "toolType": "google search"}},  # not a token of a kind
        {"toolResponse": {**PROVIDER_PARTS["toolResponse"], "toolType": "A" * 65}},
        {"toolCall": {**PROVIDER_PARTS["toolCall"], "args": "not an object"}},
        {"toolResponse": {**PROVIDER_PARTS["toolResponse"], "id": "t9"}},  # the response to no call
        {"executableCode": {**PROVIDER_PARTS["executableCode"], "language": "python"}},
        {"executableCode": {**PROVIDER_PARTS["executableCode"], "language": 1}},
        {"codeExecutionResult": {**PROVIDER_PARTS["codeExecutionResult"], "outcome": "_OK"}},
        {"codeExecutionResult": {**PROVIDER_PARTS["codeExecutionResult"], "output": 3}},
    ],
)
def test_a_part_a_provider_ran_outside_its_declaration_is_refused(part):
    call = {"toolCall": PROVIDER_PARTS["toolCall"]}
    code = {"executableCode": PROVIDER_PARTS["executableCode"]}
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, _ran(call, code, part))
    assert prepared.block_reason is not None and prepared.body == b""


@pytest.mark.parametrize(
    ("part", "field", "kind"),
    [
        ("toolCall", "toolType", "CODE_EXECUTION"),
        ("toolResponse", "toolType", "GOOGLE_FLIGHTS"),
        ("executableCode", "language", "JAVASCRIPT"),
        ("codeExecutionResult", "outcome", "OUTCOME_CANCELLED"),
        ("toolCall", "toolType", "A" * 64),
    ],
)
def test_a_kind_google_adds_later_passes_as_inspected(part, field, kind):
    """A part's kind is a token the provider issues, holding no user content: one of the grammar's bounded form that
    this version does not know passes, its part read and masked as any other, and never refuses the turn."""
    parts = {name: dict(value) for name, value in json.loads(json.dumps(PROVIDER_PARTS)).items()}
    parts[part][field] = kind
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, _ran(*({name: value} for name, value in parts.items())))
    assert (prepared.mode, prepared.block_reason) == (REWRITE, None)
    assert EMAIL not in prepared.body.decode()
    assert json.loads(prepared.body)["contents"][1]["parts"][list(parts).index(part)][part][field] == kind


KINDS = [("toolCall", "toolType"), ("toolResponse", "toolType"), ("executableCode", "language")]
KINDS += [("codeExecutionResult", "outcome")]


def _kind(part: str, field: str, kind: str) -> bytes:
    parts = {name: dict(value) for name, value in json.loads(json.dumps(PROVIDER_PARTS)).items()}
    parts[part][field] = kind
    return _ran(*({name: value} for name, value in parts.items()))


@pytest.mark.parametrize(("part", "field"), KINDS)
def test_an_iban_in_a_kind_never_leaves_in_clear(part, field):
    """A kind's grammar holds no digit (none of the values Google documents has one): an IBAN there is refused, and
    nothing is sent."""
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, _kind(part, field, "DE89370400440532013000"))
    assert prepared.block_reason is not None and prepared.body == b""


@pytest.mark.parametrize(("part", "field"), KINDS)
def test_a_kind_is_checked_as_text_before_it_leaves(part, field):
    """A kind of the grammar's form is checked by the final check as an id or a name is: one of your terms written
    there stops the request, saying why, as it would in the tool's name."""
    mediator = _mediator(terms=[TermConfig(value="FALCON_ACCOUNTS", type="PROJECT")])
    prepared = mediator.prepare(GEMINI, "POST", CALL, KEY, _kind(part, field, "FALCON_ACCOUNTS"))
    assert prepared.block_reason is not None and "sensitive values would leave in clear" in prepared.block_reason
    named = {name: dict(value) for name, value in json.loads(json.dumps(PROVIDER_PARTS)).items()}
    named["toolCall"]["toolName"] = "FALCON_ACCOUNTS"
    by_name = mediator.prepare(GEMINI, "POST", CALL, KEY, _ran(*({k: v} for k, v in named.items())))
    assert by_name.block_reason == prepared.block_reason


def test_a_signed_part_a_provider_ran_follows_the_rule_of_a_signed_function_call():
    """Its opaque signature is admitted as any other part's is, or refused alike (its rule, not its kind)."""
    searched = {"toolCall": PROVIDER_PARTS["toolCall"], "thoughtSignature": "c2ln"}
    called = {"functionCall": {"name": "read_file", "args": {"path": "notes.txt"}}, "thoughtSignature": "c2ln"}
    outcomes = [_mediator().prepare(GEMINI, "POST", CALL, KEY, _ran(part)) for part in (searched, called)]
    assert len({(one.mode, one.block_reason, one.envelope_refusal) for one in outcomes}) == 1, outcomes


def test_a_conversation_the_sdk_writes_after_its_built_in_tools_is_read():
    """Observed shape: the body google-genai builds for generateContent (``Models._generate_content``: the request's
    parameters to the Gemini API's form, then to plain JSON) once the model has searched and run code."""
    from google.genai import _common, models, types
    from google.genai._api_client import BaseApiClient

    tool = types.ToolType.GOOGLE_SEARCH_WEB
    parameters = types._GenerateContentParameters(
        model="gemini-3-pro",
        contents=[
            types.Content(role="user", parts=[types.Part(text=f"what does the web say about {EMAIL}?")]),
            types.Content(
                role="model",
                parts=[
                    types.Part(tool_call=types.ToolCall(id="t1", tool_type=tool, args={"queries": [EMAIL]})),
                    types.Part(tool_response=types.ToolResponse(id="t1", tool_type=tool, response={"hits": [EMAIL]})),
                    types.Part(
                        executable_code=types.ExecutableCode(id="c1", language="PYTHON", code=f"len('{EMAIL}')")
                    ),
                    types.Part(
                        code_execution_result=types.CodeExecutionResult(id="c1", outcome="OUTCOME_OK", output="21")
                    ),
                    types.Part(text=f"{EMAIL} appears once"),
                ],
            ),
            types.Content(role="user", parts=[types.Part(text="thanks")]),
        ],
    )
    body = models._GenerateContentParameters_to_mldev(BaseApiClient(api_key="fake-gemini-key"), parameters)
    body.pop("_url", None)
    body = _common.encode_unserializable_types(_common.convert_to_dict(body))
    assert {key for part in body["contents"][1]["parts"] for key in part} >= set(PROVIDER_PARTS)
    prepared = _mediator().prepare(GEMINI, "POST", CALL, KEY, json.dumps(body).encode())
    assert (prepared.mode, prepared.block_reason) == (REWRITE, None)
    assert EMAIL not in prepared.body.decode()


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
