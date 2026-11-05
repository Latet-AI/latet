"""P2-8's deterministic fixtures, shared by a computer and a server (`notes/server-request-coverage-plan.md`, P2-8):

- ``NEGATIVE``: for each request family a routed agent uses, its main call, its ancillary calls (the model list, the
  cache, the cancel) and one unsupported sibling, each with the outcome it must have;
- ``DRIFT``: for each family, one added top-level body member, one added nested member, one added header, one added
  query name (and a removable header where the family declares one), each with its documented outcome: a refusal
  naming the path's category, D4's header rule, D6 where a request is checked only.

Every case goes through the actual ingress of a cell (``tests/request_coverage.py``: a computer's runtime, or a server's
gate and an API key's space) to the recording transport at S1-S4: no provider is called. A case that carries the canary
(``EMAIL``) asserts it absent from every request and frame that left. A case that needs an owner-listed resource
(``RESOURCES``: a Vertex project, a Gemini cache) runs on a computer alone: a server's key identity takes its settings
from the administration, which these cells do not drive (the report says so)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .request_coverage import EMAIL, Case

UUID = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
CODEX_SESSION = "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"
ACCOUNT = "7c1e2d3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
JSON = ("content-type", "application/json")

ANTHROPIC_KEY = (("x-api-key", "sk-ant-api03-rc"), ("anthropic-version", "2023-06-01"), JSON)
CLAUDE_CODE = (
    ("authorization", "Bearer sk-ant-oat01-rc"),
    ("anthropic-version", "2023-06-01"),
    JSON,
    ("user-agent", "claude-cli/2.1.312 (external, cli)"),
    ("x-claude-code-session-id", UUID),
    ("x-app", "cli"),
)
CLAUDE_CODE_KEY = (("x-api-key", "sk-ant-api03-rc"), *CLAUDE_CODE[1:])
CLAUDE_CODE_GET = (
    ("authorization", "Bearer sk-ant-oat01-rc"),
    ("anthropic-version", "2023-06-01"),
    ("user-agent", "claude-cli/2.1.312 (external, cli)"),
    ("x-app", "cli"),
)
OPENAI = (("authorization", "Bearer sk-proj-rc"), JSON)
OPENAI_GET = (("authorization", "Bearer sk-proj-rc"),)
CODEX_HTTP = (
    ("authorization", "Bearer eyJ-rc"),
    JSON,
    ("originator", "codex_cli_rs"),
    ("session-id", CODEX_SESSION),
    ("chatgpt-account-id", ACCOUNT),
    ("user-agent", "codex_cli_rs/0.162.0 (Windows 10.0.19045; x86_64) WindowsTerminal"),
)
CODEX_GET = (("authorization", "Bearer eyJ-rc"), ("originator", "codex_cli_rs"), ("chatgpt-account-id", ACCOUNT))
CODEX_WS = (("authorization", "Bearer eyJ-rc"), ("originator", "codex_cli_rs"), ("session-id", CODEX_SESSION))
GEMINI = (("x-goog-api-key", "AIza-rc"), JSON)
GEMINI_GET = (("x-goog-api-key", "AIza-rc"),)
VERTEX_EXPRESS = (("x-goog-api-key", "AQ.rc"), JSON)
OPENROUTER = (("authorization", "Bearer sk-or-v1-rc"), JSON)
OPENROUTER_GET = (("authorization", "Bearer sk-or-v1-rc"),)
BEDROCK = (("authorization", "Bearer ABSK-rc"), JSON)
BEDROCK_GET = (("authorization", "Bearer ABSK-rc"),)
VERTEX = (("authorization", "Bearer ya29.rc"), JSON)
VERTEX_GET = (("authorization", "Bearer ya29.rc"),)
COMPANY = (("authorization", "Bearer sk-company-rc"), JSON)
COMPANY_GET = (("authorization", "Bearer sk-company-rc"),)

CLAUDE = "claude-sonnet-4-5-20250929"
BEDROCK_MODEL = "/bedrock/us-east-1/model/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
VERTEX_MODEL = "/vertex/v1/projects/acme-prod/locations/us-east5/publishers/anthropic/models/claude-sonnet-4-5@20250929"
GEMINI_CALL = "/gemini/v1beta/models/gemini-2.5-pro"
SAID = f"write to {EMAIL}"

# The owner-listed resources a computer's cases name (applied once to its runtime): never on a server (module doc).
RESOURCES = (
    {
        "destination": "vertex",
        "url": "https://us-east5-aiplatform.googleapis.com",
        "role": "project",
        "value": "acme-prod",
    },
    {"destination": "gemini", "url": "https://generativelanguage.googleapis.com", "role": "cache", "value": "abc123"},
    {"destination": "gemini", "url": "https://generativelanguage.googleapis.com", "role": "updateMask", "value": "ttl"},
)


@dataclass(frozen=True)
class Expect:
    """What a case must give: ``status`` (None: whatever the provider's fake answered, or a WebSocket), how many
    requests left at S1 (``sends``), the history record's category (``code``), words the agent is told (``words``: in
    the error, an error frame or the close reason), a WebSocket's ``close`` code and ``frames_up``; ``canary``: the
    case carries ``EMAIL``, which must be absent from everything that left; ``removed``: a header that must not leave."""

    status: int | None = 200
    sends: int = 1
    code: str | None = None
    words: str | None = None
    close: int | None = None
    frames_up: int | None = None
    canary: bool = False
    removed: str | None = None


@dataclass(frozen=True)
class Fixture:
    kind: str  # NEGATIVE: main, ancillary, sibling; DRIFT: top, nested, header, query, removable
    case: Case
    expect: Expect


SENT = Expect()
SENT_MASKED = Expect(canary=True)
LEAK_REFUSED = Expect(400, 0, words="sensitive values would leave in clear", canary=True)
ROUTE_REFUSED = Expect(400, 0, "envelope-route", "does not support this call")
HEADER_REFUSED = Expect(400, 0, "envelope-header-unknown", "header x-drift-header")
QUERY_REFUSED = Expect(400, 0, "envelope-query-unknown", "query parameter drift")


def unknown(where: str) -> Expect:
    return Expect(400, 0, "body-unknown-member", f"unknown member in {where}")


def _body(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode()


def anthropic(text: str = "hello", *, extra: dict[str, Any] | None = None, message: dict[str, Any] | None = None):
    return _body(
        {"model": CLAUDE, "max_tokens": 64, "messages": [{"role": "user", "content": text, **(message or {})}]}
        | (extra or {})
    )


def chat(text: str = "hello", *, model: str = "gpt-4o", extra=None, message=None) -> bytes:
    return _body({"model": model, "messages": [{"role": "user", "content": text, **(message or {})}]} | (extra or {}))


def responses(text: str = "hello", *, model: str = "gpt-5", extra=None, content=None, codex: bool = False) -> bytes:
    item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text, **(content or {})}]}
    doc: dict[str, Any] = {"model": model, "input": [item]}
    if codex:
        doc |= {"instructions": "You are Codex.", "stream": True, "store": False}
    return _body(doc | (extra or {}))


def turn(text: str = "hello", *, extra=None, item=None) -> str:
    message = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}], **(item or {})}
    return json.dumps(
        {"type": "response.create", "model": "gpt-5.2-codex", "input": [message], "store": False} | (extra or {})
    )


# A turn that continues a response with ``previous_response_id`` and sends the new item alone: the output of a call that
# response made, which the body does not hold (Codex 0.162.0 over a WebSocket after a tool call, core/src/client.rs's
# incremental ResponseCreateWsRequest; a Responses client over HTTP, as the OpenAI Agents SDK can).
PREVIOUS = "resp_68d3a1f0c4b88190a2b5c7d9e1f3a5b7"


def continued(text: str = "done") -> dict[str, Any]:
    output = {"type": "function_call_output", "call_id": "call_rq1", "output": text}
    return {"previous_response_id": PREVIOUS, "input": [output]}


def gemini(text: str = "hello", *, extra=None, content=None, part=None) -> bytes:
    contents = [{"role": "user", "parts": [{"text": text, **(part or {})}], **(content or {})}]
    return _body({"contents": contents} | (extra or {}))


def bedrock(text: str = "hello", *, extra=None, message=None, version: str = "bedrock-2023-05-31") -> bytes:
    doc = {
        "anthropic_version": version,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": text, **(message or {})}],
    }
    return _body(doc | (extra or {}))


def _http(name: str, target: str, headers, body: bytes = b"", method: str = "POST", *, computer: bool = False) -> Case:
    return Case(
        name, (), method, target, tuple(headers), body, machines=("computer",) if computer else ("computer", "server")
    )


def _ws(name: str, headers, *frames: str, query: str = "") -> Case:
    return Case(name, (), "GET", "/backend-api/codex/responses" + query, tuple(headers), transport="ws", frames=frames)


DRIFT_HEADER = ("x-drift-header", "1")
BEDROCK_COUNT = (
    b'{"input":{"invokeModel":{"body":"eyJhbnRocm9waWNfdmVyc2lvbiI6ImJlZHJvY2stMjAyMy0wNS0zMSIsIm1lc3NhZ2VzIjpbeyJyb2xlIj'
    b'oidXNlciIsImNvbnRlbnQiOiJoZWxsbyJ9XX0="}}}'
)

NEGATIVE: dict[str, tuple[Fixture, ...]] = {
    "anthropic-key": (
        Fixture(
            "main", _http("anthropic-messages", "/anthropic/v1/messages", ANTHROPIC_KEY, anthropic(SAID)), SENT_MASKED
        ),
        Fixture(
            "ancillary",
            _http(
                "anthropic-count-tokens",
                "/anthropic/v1/messages/count_tokens",
                ANTHROPIC_KEY,
                _body({"model": CLAUDE, "messages": [{"role": "user", "content": SAID}]}),
            ),
            SENT_MASKED,
        ),
        Fixture(
            "ancillary",
            _http("anthropic-models", "/anthropic/v1/models?limit=20", ANTHROPIC_KEY[:2], method="GET"),
            SENT,
        ),
        Fixture(
            "ancillary",
            _http("anthropic-models-beta", "/anthropic/v1/models?beta=true", ANTHROPIC_KEY[:2], method="GET"),
            SENT,
        ),
        Fixture(
            "ancillary",
            _http("anthropic-models-drift", "/anthropic/v1/models?limit=20&drift=1", ANTHROPIC_KEY[:2], method="GET"),
            QUERY_REFUSED,
        ),
        Fixture(
            "sibling",
            _http("anthropic-batches", "/anthropic/v1/messages/batches", ANTHROPIC_KEY, _body({"requests": []})),
            ROUTE_REFUSED,
        ),
    ),
    "claude-code": (
        Fixture("main", _http("cc-messages", "/anthropic/v1/messages?beta=true", CLAUDE_CODE, anthropic()), SENT),
        Fixture(
            "main",
            _http("cc-messages-canary", "/anthropic/v1/messages?beta=true", CLAUDE_CODE, anthropic(SAID)),
            LEAK_REFUSED,
        ),
        Fixture(
            "main",
            _http("cc-key-canary", "/anthropic/v1/messages?beta=true", CLAUDE_CODE_KEY, anthropic(SAID)),
            LEAK_REFUSED,
        ),
        Fixture(
            "ancillary",
            _http(
                "cc-count-tokens",
                "/anthropic/v1/messages/count_tokens?beta=true",
                CLAUDE_CODE,
                _body({"model": CLAUDE, "messages": [{"role": "user", "content": "hello"}]}),
            ),
            SENT,
        ),
        Fixture(
            "ancillary",
            _http("cc-models", "/anthropic/v1/models?beta=true&limit=1000", CLAUDE_CODE_GET, method="GET"),
            SENT,
        ),
        Fixture("sibling", _http("cc-files", "/anthropic/v1/files", CLAUDE_CODE_GET, method="GET"), ROUTE_REFUSED),
    ),
    "openai-chat": (
        Fixture("main", _http("chat", "/openai/v1/chat/completions", OPENAI, chat(SAID)), SENT_MASKED),
        Fixture("ancillary", _http("openai-models", "/openai/v1/models", OPENAI_GET, method="GET"), SENT),
        Fixture(
            "ancillary",
            _http("openai-models-drift", "/openai/v1/models?drift=1", OPENAI_GET, method="GET"),
            QUERY_REFUSED,
        ),
        Fixture(
            "sibling", _http("openai-files", "/openai/v1/files", OPENAI, _body({"purpose": "batch"})), ROUTE_REFUSED
        ),
        Fixture(
            "sibling",
            _http("openai-moderations", "/openai/v1/moderations", OPENAI, _body({"input": "hello"})),
            ROUTE_REFUSED,
        ),
    ),
    "openai-responses": (
        Fixture("main", _http("responses", "/openai/v1/responses", OPENAI, responses(SAID)), SENT_MASKED),
        Fixture(
            "main",
            _http("responses-continued", "/openai/v1/responses", OPENAI, responses(extra=continued(SAID))),
            SENT_MASKED,
        ),
        Fixture(
            "sibling",
            _http("responses-get", "/openai/v1/responses/resp_abc123", OPENAI_GET, method="GET"),
            ROUTE_REFUSED,
        ),
        Fixture(
            "sibling",
            _http("responses-items", "/openai/v1/responses/resp_abc123/input_items", OPENAI_GET, method="GET"),
            ROUTE_REFUSED,
        ),
    ),
    "embeddings": (
        Fixture(
            "main",
            _http(
                "embeddings", "/openai/v1/embeddings", OPENAI, _body({"model": "text-embedding-3-small", "input": SAID})
            ),
            SENT_MASKED,
        ),
        Fixture(
            "sibling",
            _http(
                "audio-speech",
                "/openai/v1/audio/speech",
                OPENAI,
                _body({"model": "tts-1", "input": "hello", "voice": "alloy"}),
            ),
            ROUTE_REFUSED,
        ),
    ),
    "codex-http": (
        Fixture(
            "main",
            _http(
                "codex-http",
                "/backend-api/codex/responses",
                CODEX_HTTP,
                responses(SAID, model="gpt-5.2-codex", codex=True),
            ),
            SENT_MASKED,
        ),
        Fixture(
            "ancillary",
            _http("codex-models", "/backend-api/codex/models?client_version=0.162.0", CODEX_GET, method="GET"),
            SENT,
        ),
        Fixture(
            "sibling",
            _http("codex-get", "/backend-api/codex/responses/resp_abc", CODEX_GET[:2], method="GET"),
            ROUTE_REFUSED,
        ),
    ),
    "codex-ws": (
        Fixture("main", _ws("codex-ws", CODEX_WS, turn(SAID)), Expect(None, 0, frames_up=1, canary=True)),
        Fixture(
            "main",
            _ws("codex-ws-continued", CODEX_WS, turn(extra=continued(SAID))),
            Expect(None, 0, frames_up=1, canary=True),
        ),
        Fixture(
            "ancillary",
            _ws("codex-ws-cancel", CODEX_WS, json.dumps({"type": "response.cancel", "response_id": "resp_abc123"})),
            Expect(None, 0, words="cancel target", frames_up=0),
        ),
        Fixture(
            "sibling",
            _ws("codex-ws-other", CODEX_WS, json.dumps({"type": "session.update", "session": {}})),
            Expect(None, 0, words="another type", frames_up=0),
        ),
    ),
    "gemini": (
        Fixture("main", _http("gemini-generate", f"{GEMINI_CALL}:generateContent", GEMINI, gemini(SAID)), SENT_MASKED),
        Fixture(
            "main",
            _http("gemini-stream", f"{GEMINI_CALL}:streamGenerateContent?alt=sse", GEMINI, gemini(SAID)),
            SENT_MASKED,
        ),
        Fixture(
            "main",
            _http(
                "vertex-express",
                "/gemini/v1beta1/publishers/google/models/gemini-2.5-pro:generateContent",
                VERTEX_EXPRESS,
                gemini(SAID),
            ),
            SENT_MASKED,
        ),
        Fixture("ancillary", _http("gemini-count", f"{GEMINI_CALL}:countTokens", GEMINI, gemini(SAID)), SENT_MASKED),
        Fixture(
            "ancillary", _http("gemini-models", "/gemini/v1beta/models?pageSize=50", GEMINI_GET, method="GET"), SENT
        ),
        Fixture(
            "ancillary",
            _http(
                "gemini-cache",
                "/gemini/v1beta/cachedContents",
                GEMINI,
                _body({"model": "models/gemini-2.5-pro", "contents": [{"role": "user", "parts": [{"text": SAID}]}]}),
            ),
            SENT_MASKED,
        ),
        Fixture(
            "ancillary",
            _http(
                "gemini-cache-patch",
                "/gemini/v1beta/cachedContents/abc123?updateMask=ttl",
                GEMINI,
                _body({"ttl": "600s"}),
                "PATCH",
                computer=True,
            ),
            Expect(None),  # sent once; the provider's fake answers it as it answers a PATCH
        ),
        Fixture("sibling", _http("gemini-files", "/gemini/v1beta/files", GEMINI_GET, method="GET"), ROUTE_REFUSED),
        Fixture(
            "sibling",
            _http("gemini-cache-get", "/gemini/v1beta/cachedContents/abc123", GEMINI_GET, method="GET"),
            ROUTE_REFUSED,
        ),
    ),
    "openrouter": (
        Fixture(
            "main",
            _http(
                "openrouter-chat",
                "/openrouter/api/v1/chat/completions",
                OPENROUTER,
                chat(SAID, model="anthropic/claude-sonnet-4.5"),
            ),
            SENT_MASKED,
        ),
        Fixture(
            "main",
            _http(
                "openrouter-other-vendor",
                "/openrouter/api/v1/chat/completions",
                OPENROUTER,
                chat(model="meta-llama/llama-3.1-70b-instruct"),
            ),
            Expect(400, 0, words="model resource"),
        ),
        Fixture(
            "ancillary", _http("openrouter-models", "/openrouter/api/v1/models", OPENROUTER_GET, method="GET"), SENT
        ),
        Fixture(
            "sibling",
            _http("openrouter-credits", "/openrouter/api/v1/credits", OPENROUTER_GET, method="GET"),
            ROUTE_REFUSED,
        ),
    ),
    "bedrock": (
        Fixture("main", _http("bedrock-invoke", f"{BEDROCK_MODEL}/invoke", BEDROCK, bedrock()), SENT),
        Fixture("main", _http("bedrock-canary", f"{BEDROCK_MODEL}/invoke", BEDROCK, bedrock(SAID)), LEAK_REFUSED),
        Fixture(
            "ancillary",
            _http(
                "bedrock-profiles",
                "/bedrock/us-east-1/inference-profiles?type=SYSTEM_DEFINED&maxResults=100",
                BEDROCK_GET,
                method="GET",
            ),
            SENT,
        ),
        Fixture("ancillary", _http("bedrock-count", f"{BEDROCK_MODEL}/count-tokens", BEDROCK, BEDROCK_COUNT), SENT),
        Fixture(
            "sibling",
            _http("bedrock-converse", f"{BEDROCK_MODEL}/converse", BEDROCK, _body({"messages": []})),
            ROUTE_REFUSED,
        ),
    ),
    "vertex": (
        Fixture(
            "main",
            _http(
                "vertex-raw", f"{VERTEX_MODEL}:rawPredict", VERTEX, bedrock(version="vertex-2023-10-16"), computer=True
            ),
            SENT,
        ),
        Fixture(
            "main",
            _http(
                "vertex-stream",
                f"{VERTEX_MODEL}:streamRawPredict",
                VERTEX,
                bedrock(version="vertex-2023-10-16", extra={"stream": True}),
                computer=True,
            ),
            SENT,
        ),
        Fixture(
            "main",
            _http(
                "vertex-canary",
                f"{VERTEX_MODEL}:rawPredict",
                VERTEX,
                bedrock(SAID, version="vertex-2023-10-16"),
                computer=True,
            ),
            LEAK_REFUSED,
        ),
        Fixture(
            "ancillary",
            _http("vertex-models", "/vertex/v1/publishers/google/models?alt=json", VERTEX_GET, method="GET"),
            SENT,
        ),
        Fixture(
            "sibling",
            _http(
                "vertex-predict",
                "/vertex/v1/projects/acme-prod/locations/us-east5/endpoints/e:predict",
                VERTEX,
                _body({"instances": []}),
                computer=True,  # its project is listed first, so that the route alone refuses it
            ),
            ROUTE_REFUSED,
        ),
    ),
    "company": (
        Fixture("main", _http("company-chat", "/llm/v1/chat/completions", COMPANY, chat(SAID)), SENT_MASKED),
        Fixture("ancillary", _http("company-models", "/llm/v1/models", COMPANY_GET, method="GET"), SENT),
        Fixture("sibling", _http("company-files", "/llm/v1/files", COMPANY_GET, method="GET"), ROUTE_REFUSED),
    ),
}

DRIFT: dict[str, tuple[Fixture, ...]] = {
    "anthropic-key": (
        Fixture(
            "top",
            _http("d-anthropic-top", "/anthropic/v1/messages", ANTHROPIC_KEY, anthropic(extra={"drift_member": "x"})),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http(
                "d-anthropic-nested", "/anthropic/v1/messages", ANTHROPIC_KEY, anthropic(message={"drift_member": "x"})
            ),
            unknown("message"),
        ),
        Fixture(
            "header",
            _http("d-anthropic-header", "/anthropic/v1/messages", (*ANTHROPIC_KEY, DRIFT_HEADER), anthropic()),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http("d-anthropic-query", "/anthropic/v1/messages?drift=1", ANTHROPIC_KEY, anthropic()),
            QUERY_REFUSED,
        ),
        Fixture(
            "removable",
            _http(
                "d-anthropic-removable",
                "/anthropic/v1/messages",
                (*ANTHROPIC_KEY, ("x-stainless-drift", "1")),
                anthropic(),
            ),
            Expect(removed="x-stainless-drift"),
        ),
    ),
    "claude-code": (
        # D6: a request checked only lets a new member through when its value is a text or a list of texts, read by the
        # final check; an object, or a text carrying a sensitive value, is refused.
        Fixture(
            "top",
            _http("d-cc-top", "/anthropic/v1/messages?beta=true", CLAUDE_CODE, anthropic(extra={"drift_member": "x"})),
            SENT,
        ),
        Fixture(
            "top",
            _http(
                "d-cc-top-list",
                "/anthropic/v1/messages?beta=true",
                CLAUDE_CODE,
                anthropic(extra={"drift_member": ["a", "b"]}),
            ),
            SENT,
        ),
        Fixture(
            "top",
            _http(
                "d-cc-top-object",
                "/anthropic/v1/messages?beta=true",
                CLAUDE_CODE,
                anthropic(extra={"drift_member": {"a": 1}}),
            ),
            unknown("top-level"),
        ),
        Fixture(
            "top",
            _http(
                "d-cc-top-canary",
                "/anthropic/v1/messages?beta=true",
                CLAUDE_CODE,
                anthropic(extra={"drift_member": SAID}),
            ),
            LEAK_REFUSED,
        ),
        Fixture(
            "nested",
            _http(
                "d-cc-nested", "/anthropic/v1/messages?beta=true", CLAUDE_CODE, anthropic(message={"drift_member": "x"})
            ),
            SENT,
        ),
        Fixture(
            "nested",
            _http(
                "d-cc-nested-block",
                "/anthropic/v1/messages?beta=true",
                CLAUDE_CODE,
                _body(
                    {
                        "model": CLAUDE,
                        "max_tokens": 64,
                        "messages": [
                            {"role": "user", "content": [{"type": "text", "text": "hello", "drift_member": "x"}]}
                        ],
                    }
                ),
            ),
            SENT,
        ),
        Fixture(
            "header",
            _http("d-cc-header", "/anthropic/v1/messages?beta=true", (*CLAUDE_CODE, DRIFT_HEADER), anthropic()),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http("d-cc-query", "/anthropic/v1/messages?beta=true&drift=1", CLAUDE_CODE, anthropic()),
            QUERY_REFUSED,
        ),
        Fixture(
            "removable",
            _http(
                "d-cc-removable",
                "/anthropic/v1/messages?beta=true",
                (*CLAUDE_CODE, ("x-stainless-drift", "1")),
                anthropic(),
            ),
            Expect(removed="x-stainless-drift"),
        ),
    ),
    "openai-chat": (
        Fixture(
            "top",
            _http("d-chat-top", "/openai/v1/chat/completions", OPENAI, chat(extra={"drift_member": "x"})),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http("d-chat-nested", "/openai/v1/chat/completions", OPENAI, chat(message={"drift_member": "x"})),
            unknown("message"),
        ),
        Fixture(
            "nested",
            _http(
                "d-chat-stream-options",
                "/openai/v1/chat/completions",
                OPENAI,
                chat(extra={"stream_options": {"include_usage": True, "drift_member": "x"}}),
            ),
            Expect(400, 0, words="stream_options"),
        ),
        Fixture(
            "header",
            _http("d-chat-header", "/openai/v1/chat/completions", (*OPENAI, DRIFT_HEADER), chat()),
            HEADER_REFUSED,
        ),
        Fixture("query", _http("d-chat-query", "/openai/v1/chat/completions?drift=1", OPENAI, chat()), QUERY_REFUSED),
    ),
    "openai-responses": (
        Fixture(
            "top",
            _http("d-responses-top", "/openai/v1/responses", OPENAI, responses(extra={"drift_member": "x"})),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http(
                "d-responses-nested",
                "/openai/v1/responses",
                OPENAI,
                responses(extra={"reasoning": {"effort": "low", "drift_member": "x"}}),
            ),
            unknown("reasoning control"),
        ),
        Fixture(
            "header",
            _http("d-responses-header", "/openai/v1/responses", (*OPENAI, DRIFT_HEADER), responses()),
            HEADER_REFUSED,
        ),
        Fixture(
            "query", _http("d-responses-query", "/openai/v1/responses?drift=1", OPENAI, responses()), QUERY_REFUSED
        ),
    ),
    "embeddings": (
        Fixture(
            "top",
            _http(
                "d-embeddings-top",
                "/openai/v1/embeddings",
                OPENAI,
                _body({"model": "text-embedding-3-small", "input": "hello", "drift_member": "x"}),
            ),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http(
                "d-embeddings-nested",
                "/openai/v1/embeddings",
                OPENAI,
                _body({"model": "text-embedding-3-small", "input": [{"drift_member": "x"}]}),
            ),
            Expect(400, 0, words="text input"),
        ),
        Fixture(
            "header",
            _http(
                "d-embeddings-header",
                "/openai/v1/embeddings",
                (*OPENAI, DRIFT_HEADER),
                _body({"model": "text-embedding-3-small", "input": "hello"}),
            ),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http(
                "d-embeddings-query",
                "/openai/v1/embeddings?drift=1",
                OPENAI,
                _body({"model": "text-embedding-3-small", "input": "hello"}),
            ),
            QUERY_REFUSED,
        ),
    ),
    "codex-http": (
        Fixture(
            "top",
            _http(
                "d-codex-top",
                "/backend-api/codex/responses",
                CODEX_HTTP,
                responses(model="gpt-5.2-codex", codex=True, extra={"drift_member": "x"}),
            ),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http(
                "d-codex-nested",
                "/backend-api/codex/responses",
                CODEX_HTTP,
                responses(model="gpt-5.2-codex", codex=True, content={"drift_member": "x"}),
            ),
            unknown("message content block"),
        ),
        Fixture(
            "header",
            _http(
                "d-codex-header",
                "/backend-api/codex/responses",
                (*CODEX_HTTP, DRIFT_HEADER),
                responses(model="gpt-5.2-codex", codex=True),
            ),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http(
                "d-codex-query",
                "/backend-api/codex/responses?drift=1",
                CODEX_HTTP,
                responses(model="gpt-5.2-codex", codex=True),
            ),
            QUERY_REFUSED,
        ),
    ),
    "codex-ws": (
        # A frame takes no request exception (F-2): its unknown member is refused in an error frame, nothing of the turn
        # sent; the handshake's header or query parameter closes the socket naming it (root's ruling 5).
        Fixture(
            "top",
            _ws("d-ws-top", CODEX_WS, turn(extra={"drift_member": "x"})),
            Expect(None, 0, words="unknown member in top-level", frames_up=0),
        ),
        Fixture(
            "nested",
            _ws("d-ws-nested", CODEX_WS, turn(item={"drift_member": "x"})),
            Expect(None, 0, words="unknown member in input item", frames_up=0),
        ),
        Fixture(
            "header",
            _ws("d-ws-header", (*CODEX_WS, DRIFT_HEADER), turn()),
            Expect(None, 0, "envelope-header-unknown", "header x-drift-header", close=1008, frames_up=0),
        ),
        Fixture(
            "query",
            _ws("d-ws-query", CODEX_WS, turn(), query="?drift=1"),
            Expect(None, 0, "envelope-query-unknown", "query parameter drift", close=1008, frames_up=0),
        ),
    ),
    "gemini": (
        Fixture(
            "top",
            _http("d-gemini-top", f"{GEMINI_CALL}:generateContent", GEMINI, gemini(extra={"drift_member": "x"})),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http("d-gemini-content", f"{GEMINI_CALL}:generateContent", GEMINI, gemini(content={"drift_member": "x"})),
            unknown("message"),
        ),
        Fixture(
            "nested",
            _http(
                "d-gemini-config",
                f"{GEMINI_CALL}:generateContent",
                GEMINI,
                gemini(extra={"generationConfig": {"temperature": 0.2, "drift_member": "x"}}),
            ),
            unknown("generation control"),
        ),
        Fixture(
            "nested",
            _http("d-gemini-part", f"{GEMINI_CALL}:generateContent", GEMINI, gemini(part={"drift_member": "x"})),
            unknown("message content block"),
        ),
        Fixture(
            "header",
            _http("d-gemini-header", f"{GEMINI_CALL}:generateContent", (*GEMINI, DRIFT_HEADER), gemini()),
            HEADER_REFUSED,
        ),
        Fixture(
            "query", _http("d-gemini-query", f"{GEMINI_CALL}:generateContent?drift=1", GEMINI, gemini()), QUERY_REFUSED
        ),
        Fixture(
            "removable",
            _http(
                "d-gemini-removable",
                f"{GEMINI_CALL}:generateContent",
                (*GEMINI, ("x-goog-api-client", "google-genai-sdk/1.50.0 gl-node/v24")),
                gemini(),
            ),
            Expect(removed="x-goog-api-client"),
        ),
    ),
    "openrouter": (
        Fixture(
            "top",
            _http(
                "d-openrouter-top",
                "/openrouter/api/v1/chat/completions",
                OPENROUTER,
                chat(model="openai/gpt-4o", extra={"drift_member": "x"}),
            ),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http(
                "d-openrouter-nested",
                "/openrouter/api/v1/chat/completions",
                OPENROUTER,
                chat(model="openai/gpt-4o", message={"drift_member": "x"}),
            ),
            unknown("message"),
        ),
        Fixture(
            "header",
            _http(
                "d-openrouter-header",
                "/openrouter/api/v1/chat/completions",
                (*OPENROUTER, DRIFT_HEADER),
                chat(model="openai/gpt-4o"),
            ),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http(
                "d-openrouter-query",
                "/openrouter/api/v1/chat/completions?drift=1",
                OPENROUTER,
                chat(model="openai/gpt-4o"),
            ),
            QUERY_REFUSED,
        ),
    ),
    "bedrock": (
        Fixture(
            "top",
            _http("d-bedrock-top", f"{BEDROCK_MODEL}/invoke", BEDROCK, bedrock(extra={"drift_member": "x"})),
            SENT,
        ),
        Fixture(
            "nested",
            _http(
                "d-bedrock-nested", f"{BEDROCK_MODEL}/invoke", BEDROCK, bedrock(message={"drift_member": {"a": "x"}})
            ),
            unknown("message"),
        ),
        Fixture(
            "header",
            _http("d-bedrock-header", f"{BEDROCK_MODEL}/invoke", (*BEDROCK, DRIFT_HEADER), bedrock()),
            HEADER_REFUSED,
        ),
        Fixture(
            "query", _http("d-bedrock-query", f"{BEDROCK_MODEL}/invoke?drift=1", BEDROCK, bedrock()), QUERY_REFUSED
        ),
        Fixture(
            "removable",
            _http(
                "d-bedrock-removable",
                f"{BEDROCK_MODEL}/invoke",
                (*BEDROCK, ("amz-sdk-invocation-id", "9b2f6c1e-3a4d-4e5f-8a7b-6c5d4e3f2a1b")),
                bedrock(),
            ),
            Expect(removed="amz-sdk-invocation-id"),
        ),
    ),
    "vertex": (
        Fixture(
            "top",
            _http(
                "d-vertex-top",
                f"{VERTEX_MODEL}:rawPredict",
                VERTEX,
                bedrock(version="vertex-2023-10-16", extra={"drift_member": "x"}),
                computer=True,
            ),
            SENT,
        ),
        Fixture(
            "nested",
            _http(
                "d-vertex-nested",
                f"{VERTEX_MODEL}:rawPredict",
                VERTEX,
                bedrock(version="vertex-2023-10-16", message={"drift_member": {"a": "x"}}),
                computer=True,
            ),
            unknown("message"),
        ),
        Fixture(
            "header",
            _http(
                "d-vertex-header",
                f"{VERTEX_MODEL}:rawPredict",
                (*VERTEX, DRIFT_HEADER),
                bedrock(version="vertex-2023-10-16"),
                computer=True,
            ),
            HEADER_REFUSED,
        ),
        Fixture(
            "query",
            _http(
                "d-vertex-query",
                f"{VERTEX_MODEL}:rawPredict?drift=1",
                VERTEX,
                bedrock(version="vertex-2023-10-16"),
                computer=True,
            ),
            QUERY_REFUSED,
        ),
    ),
    "company": (
        Fixture(
            "top",
            _http("d-company-top", "/llm/v1/chat/completions", COMPANY, chat(extra={"drift_member": "x"})),
            unknown("top-level"),
        ),
        Fixture(
            "nested",
            _http("d-company-nested", "/llm/v1/chat/completions", COMPANY, chat(message={"drift_member": "x"})),
            unknown("message"),
        ),
        Fixture(
            "header",
            _http("d-company-header", "/llm/v1/chat/completions", (*COMPANY, DRIFT_HEADER), chat()),
            HEADER_REFUSED,
        ),
        Fixture("query", _http("d-company-query", "/llm/v1/chat/completions?drift=1", COMPANY, chat()), QUERY_REFUSED),
    ),
}


async def apply_resources(cell: Any) -> None:
    """The owner-listed resources a computer's cases name, applied to its runtime as `latet config add` would."""
    from latet.config import RequestResource

    runtime = await cell.runtime()
    rows = [RequestResource.model_validate(row) for row in RESOURCES]
    runtime.apply_config(runtime.config.model_copy(update={"request_resources": rows}))


def problems(cell: Any, observation: dict[str, Any], expect: Expect) -> list[str]:
    """What ``observation`` (``run_case``'s) does otherwise than ``expect``; empty when it holds. Reads the cell's
    recording transport and WebSocket upstream raw for the canary and the removed header."""
    found: list[str] = []
    counts, client = observation["counts"], observation["client"]
    said = json.dumps(client, ensure_ascii=False)
    if expect.close is not None:
        close = client.get("close")
        if not close or close[0] != expect.close:
            found.append(f"close {close!r}, expected {expect.close}")
    elif expect.status is not None and client.get("status") != expect.status:
        found.append(f"status {client.get('status')!r}, expected {expect.status}")
    if counts["send"] != expect.sends:
        found.append(f"{counts['send']} sends, expected {expect.sends}")
    if expect.frames_up is not None and counts["frames_up"] != expect.frames_up:
        found.append(f"{counts['frames_up']} frames up, expected {expect.frames_up}")
    # Refused before anything was built or connected: an HTTP refusal (400), or a handshake closed before it opened.
    if (expect.status == 400 or expect.close is not None) and counts["build"] + counts["connect"]:
        found.append(f"built or connected: {counts}")
    if expect.words is not None and expect.words not in said:
        found.append(f"the agent was not told {expect.words!r}")
    if expect.code is not None:
        codes = [record.get("block_reason") for record in observation["records"]]
        if codes != [expect.code]:
            found.append(f"history {codes!r}, expected [{expect.code!r}]")
    if expect.canary:
        left = [sent.content for sent in cell.transport.sent]
        left += [
            frame.encode() if isinstance(frame, str) else frame
            for shake in cell.ws.handshakes
            for frame in shake.frames
        ]
        if any(EMAIL.encode() in item for item in left):
            found.append("the canary left in clear")
    if expect.removed is not None:
        names = {name.lower() for sent in cell.transport.sent for name, _ in sent.headers}
        if expect.removed.encode() in names:
            found.append(f"{expect.removed} left")
    return found


def fixtures(table: dict[str, tuple[Fixture, ...]], machine: str) -> list[tuple[str, Fixture]]:
    """Every fixture of ``table`` that runs on ``machine``, with its family."""
    return [(family, fixture) for family, rows in table.items() for fixture in rows if machine in fixture.case.machines]
