"""P1-0's observed agent calls through the real envelope admission, and the grammars that name what agents send.

Every inbound HTTP call the routed agents made (``observed-calls.json``) is rebuilt from its recorded names, in wire
order, with values taken from each agent's source or the SDK it ships (the fixture records no value), and goes through
``preflight_scope``, ``envelope_of``, ``inventory``, ``admit`` and ``Upstream.resolve`` as ``forward.py`` runs them. An
agent's own call is forwarded unchanged, or altered only by the families its profile declares removable. A header the
user configured for the agent (Claude Code ``ANTHROPIC_CUSTOM_HEADERS``, Codex ``http_headers`` and
``env_http_headers``, ``COPILOT_PROVIDER_HEADERS``) stays refused, naming it (D4): a destination names its own headers
through P2-6's approval.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from latet.config import Destination
from latet.gateway import request_profiles as rp
from latet.gateway.cloud import CLOUD_UPSTREAMS, BedrockUpstream, VertexUpstream
from latet.gateway.coverage import EnvelopeRefused, envelope_of, inventory, preflight_scope
from latet.gateway.headers import DROP_PREFIXES, REQUEST_DROP
from latet.gateway.routes import DEFAULT_UPSTREAMS, RouteTable, Upstream, destination_upstream, readable_path

OBSERVED = Path(__file__).parents[1] / "data/request-coverage/observed-calls.json"
CALLS = json.loads(OBSERVED.read_text("utf-8"))["calls"]
AGENTS = {"claude-code", "codex", "copilot-cli", "qwen-code"}
SDKS = {"sdk", "sdk:openai", "sdk:anthropic", "sdk:genai"}
# Names the P1-0 probes and the Codex agent test configured for the agent; each is the user's own choice.
USER_CONFIGURED = {"x-rc-probe", "x-fake-text-only"}

# The agent tests' destinations (tests/agents/test_real_agents.py): acme-claude at a root URL, acme-codex at /v1.
DESTINATIONS = {"claude-code": "acme-claude", "codex": "acme-codex", "copilot-cli": "acme-codex"}
TABLE = RouteTable(
    [
        *DEFAULT_UPSTREAMS,
        *CLOUD_UPSTREAMS,
        destination_upstream(Destination(name="acme-claude", url="https://gateway.acme.invalid")),
        destination_upstream(Destination(name="acme-codex", url="https://gateway.acme.invalid/v1")),
    ]
)

CLAUDE_SESSION = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"  # crypto.randomUUID(), a lowercase UUIDv4
CODEX_THREAD = "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"  # ThreadId::new(), Uuid::now_v7() displayed lowercase
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1%3A0"
BEDROCK_ARN = "arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Aapplication-inference-profile%2Fabc123def456"
# Codex's turn metadata (responses_metadata.rs, turn_metadata.rs at c1382380): the repository's local path, remotes, HEAD.
TURN_METADATA = json.dumps(
    {
        "session_id": CODEX_THREAD,
        "thread_id": CODEX_THREAD,
        "turn_id": "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a20",
        "request_kind": "turn",
        "sandbox": "workspace-write",
        "workspaces": {
            "C:\\Users\\mario.rossi\\src\\acme-payroll": {
                "associated_remote_urls": {"origin": "git@github.com:acme-private/payroll.git"},
                "latest_git_commit_hash": "4c1f0e9b8a7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b",
                "has_changes": True,
            }
        },
    },
    separators=(",", ":"),
)
VALUES = {
    "host": "127.0.0.1:5283",
    "connection": "keep-alive",
    "content-type": "application/json",
    "accept-encoding": "gzip, deflate",  # Node's undici over http; the https orders are probed below
    "authorization": "Bearer fake-provider-credential",
    "x-api-key": "fake-provider-credential",
    "x-goog-api-key": "fake-provider-credential",
    "anthropic-version": "2023-06-01",
    # Claude Code 2.1.316's own betas, several outside the former 18-entry list (audit scan_betas.py).
    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,prompt-caching-scope-2026-01-05,"
    "tool-search-tool-2025-10-19",
    "anthropic-dangerous-direct-browser-access": "true",
    "x-app": "cli",
    "accept-language": "*",
    "sec-fetch-mode": "cors",
    "originator": "codex_cli_rs",
    "x-claude-code-session-id": CLAUDE_SESSION,
    "session-id": CODEX_THREAD,
    "thread-id": CODEX_THREAD,
    "x-codex-window-id": CODEX_THREAD + ":0",
    "x-client-request-id": CODEX_THREAD,
    "x-codex-beta-features": "remote_compaction_v2,network_proxy",
    "x-codex-turn-metadata": TURN_METADATA,
    "x-goog-api-client": "google-genai-sdk/1.50.0 gl-python/3.13.0",
    "x-amz-user-agent": "aws-sdk-js/3.920.0",
    "amz-sdk-invocation-id": "9b2f6c1e-3a4d-4e5f-8a7b-6c5d4e3f2a1b",
    "amz-sdk-request": "attempt=1; max=3",
    "x-initiator": "user",
    "x-interaction-type": "conversation-agent",
    "x-rc-probe": "1",
    "x-fake-text-only": "1",
}
USER_AGENTS = {
    "claude-code": "claude-cli/2.1.312 (external, cli)",
    "codex": "codex_cli_rs/0.162.0 (Windows 10.0.19045; x86_64) WindowsTerminal",
    "copilot-cli": "copilot/1.0.103 (win32 v24) term/unknown",
    "qwen-code": "QwenCode/0.22.0 (win32; x64)",
    "sdk:genai": "google-genai-sdk/1.50.0 gl-python/3.13.0",
}
QUERY = {"beta": "true", "type": "SYSTEM_DEFINED", "alt": "sse", "client_version": "0.162.0"}
PATH_ROLES = {"model", "project", "inference-profile"}


def _account_bound(resource: tuple[str, str]) -> bool:
    # A model id passes by its provider's grammar; an ARN (it carries the account) and a project stay listed.
    return resource[0] == "project" or resource[1].startswith("arn:")


def _indices(agents) -> list[int]:
    return [
        index
        for index, call in enumerate(CALLS)
        if call["side"] == "inbound" and call.get("transport") == "http" and call["agent"] in agents
    ]


def _records(agents, keep=lambda call: True):
    return [
        pytest.param(index, id=f"{index}-{CALLS[index]['agent']}-{CALLS[index]['test'].rsplit('::', 1)[-1]}")
        for index in _indices(agents)
        if keep(CALLS[index])
    ]


def _path(call: dict) -> tuple[str, set[tuple[str, str]]]:
    """The raw path with placeholders and the path resources it carries."""
    template = call["path_template"].removeprefix("/_k/{launch_key}")  # take_local_launch strips the carrier first
    resources: set[tuple[str, str]] = set()
    parts = []
    for segment in template.split("/"):
        segment = segment.replace("{destination}", DESTINATIONS.get(call["agent"], ""))
        if "{model}" in segment:
            if template.startswith("/bedrock"):
                raw = BEDROCK_ARN if "model_arn" in call["test"] else BEDROCK_MODEL
            elif template.startswith("/vertex") and "anthropic" in template:
                raw = "claude-sonnet-4-5@20250929"
            else:
                raw = "gemini-3-pro"
            resources.add(("model", raw.replace("%3A", ":").replace("%2F", "/")))
            segment = segment.replace("{model}", raw)
        if "{profile_id}" in segment:
            resources.add(("inference-profile", BEDROCK_ARN.replace("%3A", ":").replace("%2F", "/")))
            segment = segment.replace("{profile_id}", BEDROCK_ARN)
        if "{project}" in segment:
            resources.add(("project", "acme-project"))
            segment = segment.replace("{project}", "acme-project")
        segment = segment.replace("{location}", "us-central1" if call["agent"] == "sdk:genai" else "us-east5")
        segment = segment.replace("{region}", "us-east-1").replace("{profile}", "default")
        parts.append(segment.replace("{profile_key}", "0123456789abcdef0123456789abcdef"))
    return "/".join(parts), resources


def _value(agent: str, name: str) -> str:
    if name == "user-agent":
        return USER_AGENTS.get(agent, "OpenAI/Python 2.8.0")
    if name == "accept":
        return {"codex": "text/event-stream", "sdk:genai": "*/*"}.get(agent, "application/json")
    if name.startswith("x-stainless-"):
        return "js" if name == "x-stainless-lang" else "0"
    return VALUES[name]


def _admit(index: int, settings: str, *, values: dict[str, str] | None = None, without=frozenset()):
    """Admission of the rebuilt call: (Admission | EnvelopeRefused, the headers it carried, the upstream)."""
    call = CALLS[index]
    path, resources = _path(call)
    body = b"" if call["method"] == "GET" else b"{}"
    headers = []
    for name in call["header_names"]:
        if name in without:
            continue
        if name == "content-length":
            headers.append((name, str(len(body))))
        else:
            headers.append((name, (values or {}).get(name) or _value(call["agent"], name)))
    upstream = TABLE.match(path)
    assert upstream is not None, path
    expected = frozenset(filter(_account_bound, resources)) if settings == "realistic" else frozenset()
    assert {role for role, _ in expected} <= PATH_ROLES  # header values are never configured here
    upstream = replace(upstream, envelope_expected=expected, envelope_resource_sources=())
    query = "&".join(f"{name}={QUERY[name]}" for name in call["query_names"])
    scope = {
        "type": "http",
        "method": call["method"],
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
    }
    try:
        preflight_scope(scope)
        envelope = envelope_of(scope, "http", upstream.name, body)
        admission = rp.admit(inventory(envelope), upstream)
    except EnvelopeRefused as error:
        return error, headers, upstream
    assert upstream.resolve(call["method"], path, query) is not None, "admitted but not read"
    return admission, headers, upstream


def _framing(name: str) -> bool:
    return name in REQUEST_DROP or name.startswith(DROP_PREFIXES)


def _assert_forwarded(admission, headers) -> None:
    """Unchanged, or altered only by the families the selected profile declares removable."""
    assert isinstance(admission, rp.Admission), admission.agent_message()
    profile = admission.profile
    kept = [(name, value) for name, value in headers if not _framing(name)]
    removed = [name for name, _ in kept if name.startswith(profile.removable) or name in profile.removable_exact]
    assert not set(removed) & {*rp.ID_HEADERS, "authorization", "x-api-key", "x-goog-api-key", "x-amz-security-token"}
    assert [(k.lower(), v) for k, v in admission.headers] == [(n, v) for n, v in kept if n not in removed]
    assert admission.removed == len(removed)


def _has_path_resource(index: int) -> bool:
    return any(map(_account_bound, _path(CALLS[index])[1]))


@pytest.mark.parametrize("settings", ["realistic", "stock"])
@pytest.mark.parametrize("index", _records(AGENTS))
def test_every_observed_agent_call_is_forwarded_or_altered_only_by_removable_families(index, settings):
    call = CALLS[index]
    custom = [name for name in call["header_names"] if name in USER_CONFIGURED]
    if settings == "stock" and _has_path_resource(index):
        # An ARN or a project in the path is bound to an account or project and stays owner-listed: with nothing
        # listed the path refuses before any header is read; the same call with them listed is the realistic case.
        # Model ids (Bedrock's, a system-defined inference profile's, Vertex's, Gemini's) pass by grammar.
        result, _, _ = _admit(index, settings)
        assert isinstance(result, EnvelopeRefused) and result.code == "envelope-resource"
        result, headers, _ = _admit(index, settings, without=frozenset(custom))
        assert isinstance(result, EnvelopeRefused) and result.code == "envelope-resource"
        return
    result, headers, _ = _admit(index, settings)
    if custom:
        assert isinstance(result, EnvelopeRefused), "a header the user configured passed unnamed"
        assert result.code == "envelope-header-unknown"
        assert result.agent_message().endswith(f": header {custom[0]}")
        result, headers, _ = _admit(index, settings, without=frozenset(custom))
    _assert_forwarded(result, headers)


@pytest.mark.parametrize(
    "index",
    _records(AGENTS | SDKS, lambda call: "accept-encoding" in call["header_names"]),
)
@pytest.mark.parametrize(
    "encoding",
    [
        "br, gzip, deflate",  # Node 22's undici over https
        "gzip, deflate, br, zstd",
        "gzip, deflate, zstd",  # httpx with zstd and no brotli
        "gzip;q=1.0, identity;q=0.5",
    ],
)
def test_observed_calls_accept_every_known_content_coding_order(index, encoding):
    custom = frozenset(USER_CONFIGURED)
    result, headers, _ = _admit(index, "realistic", values={"accept-encoding": encoding}, without=custom)
    _assert_forwarded(result, headers)


@pytest.mark.parametrize("index", _records(SDKS))
def test_observed_sdk_calls_pass_vertex_express_included(index):
    result, headers, upstream = _admit(index, "realistic")
    _assert_forwarded(result, headers)
    if CALLS[index]["path_template"].startswith("/gemini/v1beta1/publishers/"):
        # Vertex AI express mode: its own route to Vertex AI's global endpoint, the API key a credential.
        assert isinstance(result, rp.Admission) and upstream.name == "vertex-express"
        assert result.profile.family == "gemini"
        resolved = upstream.resolve("POST", _path(CALLS[index])[0], "")
        assert resolved is not None and resolved.url.startswith(
            "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-3-pro:"
        )


def test_removed_families_are_exactly_the_agents_telemetry_and_codex_turn_metadata():
    removed: dict[str, set[str]] = {}
    for index in _indices(AGENTS | SDKS):
        result, headers, _ = _admit(index, "realistic", without=frozenset(USER_CONFIGURED))
        if not isinstance(result, rp.Admission):
            continue
        kept = {k.lower() for k, _ in result.headers}
        for name, _ in headers:
            if not _framing(name) and name not in kept:
                removed.setdefault(result.profile.family, set()).add(name.split("-")[0] + "-" + name.split("-")[1])
    assert removed == {
        "anthropic": {"x-stainless"},  # x-claude-code-agent-id too: see the subagent test below
        "openai": {"x-stainless", "x-codex"},
        "bedrock": {"x-stainless", "x-amz", "amz-sdk"},
        "gemini": {"x-goog"},
    }
    # The Vertex express call's profile is Gemini's family (the route is vertex-express), already counted above.


# --- Each grammar, with the values it refuses -------------------------------------------------------------------

OPENAI = Upstream("openai", "/openai", "https://api.openai.com")
ANTHROPIC = Upstream("anthropic", "/anthropic", "https://api.anthropic.com")
CHATGPT = Upstream("chatgpt", "/backend-api/codex", "https://chatgpt.com", keep_prefix=True)
BEDROCK = BedrockUpstream(envelope_expected=frozenset({("model", "approved")}))
GEMINI = Upstream("gemini", "/gemini", "https://generativelanguage.googleapis.com")
TARGETS = {
    "openai": (OPENAI, "POST", "/openai/v1/responses"),
    "anthropic": (ANTHROPIC, "POST", "/anthropic/v1/messages"),
    "chatgpt": (CHATGPT, "POST", "/backend-api/codex/responses"),
    "bedrock": (BEDROCK, "POST", "/bedrock/us-east-1/model/approved/invoke"),
    "bedrock-list": (BedrockUpstream(), "GET", "/bedrock/us-east-1/inference-profiles"),
    "gemini": (
        replace(GEMINI, envelope_expected=frozenset({("model", "m")})),
        "POST",
        "/gemini/v1beta/models/m:generateContent",
    ),
    "openai-models": (OPENAI, "GET", "/openai/v1/models"),
    "vertex": (
        VertexUpstream(envelope_expected=frozenset({("model", "m"), ("project", "p")})),
        "POST",
        "/vertex/v1/projects/p/locations/us-east5/publishers/anthropic/models/m:rawPredict",
    ),
}


def _one(target: str, headers: list[tuple[str, str]], query: str = ""):
    upstream, method, path = TARGETS[target]
    from latet.gateway.coverage import Envelope

    body = b"" if method == "GET" else b"{}"
    envelope = Envelope(
        "http",
        method,
        path.encode(),
        query.encode(),
        tuple((k.encode("latin-1"), v.encode("latin-1")) for k, v in headers),
        body,
        upstream.name,
    )
    return rp.admit(inventory(envelope), upstream)


UUID = "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"
BAD_COMMON = ["has a space", "mario.rossi@acme.com", "C:\\Users\\mario.rossi\\repo", "/home/mario/acme", ""]
GRAMMARS = [
    # (target, header, accepted values, refused values)
    ("openai", "x-initiator", ["user", "agent"], ["system", "user agent", "mario.rossi@acme.com", "", "u" * 300]),
    ("anthropic", "x-initiator", ["user", "agent"], ["users", "/home/mario"]),
    (
        "openai",
        "x-interaction-type",
        ["conversation-agent", "conversation-subagent", "conversation-compaction", "responses-proxy", "a" * 32],
        [*BAD_COMMON, "a" * 33, "Conversation-Agent"],
    ),
    (
        "openai",
        "originator",
        ["codex_cli_rs", "codex-tui", "codex_vscode", "codex_atlas", "Codex Desktop", "Codex", "a" * 64],
        ["a" * 65, " leading", "Mario Rossi", "Codex  Desktop", "Codex Mario Rossi", "codex_cli_rs "],
    ),
    ("chatgpt", "originator", ["codex_cli_rs", "Codex Desktop"], ["mario.rossi@acme.com", "a" * 65, "/home/mario"]),
    (
        "openai",
        "x-codex-beta-features",
        [
            "remote_compaction_v2",
            "remote_compaction_v2,network_proxy",
            "guardianv2.thread_context",
            ",".join(["k"] * 32),
        ],
        [*BAD_COMMON, "a" * 65, ",".join(["k"] * 33), "remote_compaction_v2,", "a, b", "Remote"],
    ),
    ("chatgpt", "x-codex-beta-features", ["remote_compaction_v2"], ["mario.rossi@acme.com"]),
    ("openai", "version", ["0.162.0"], ["0.162", "latest", "0.162.0 (mario)", "1" * 7 + ".0.0"]),
    (
        "anthropic",
        "anthropic-beta",
        ["claude-code-20250219", "a,b", "a, b", "x" * 64, ",".join(["t"] * 32), "prompt-caching-scope-2026-01-05"],
        [*BAD_COMMON, "x" * 65, ",".join(["t"] * 33), "a,,b", "UPPER-2025", "a_b"],
    ),
    ("bedrock", "anthropic-beta", ["interleaved-thinking-2025-05-14"], ["mario.rossi@acme.com", "x" * 65]),
    ("bedrock", "anthropic-version", ["2023-06-01"], ["bedrock-2023-05-31", "2023-06-01 mario"]),
    ("bedrock", "x-app", ["cli", "cli-bg"], ["mario", "cli bg"]),
    ("bedrock", "anthropic-dangerous-direct-browser-access", ["true"], ["false", "mario"]),
    ("vertex", "anthropic-beta", ["context-1m-2025-08-07"], ["mario.rossi@acme.com"]),
    (
        "openai",
        "accept-encoding",
        ["gzip", "br, gzip, deflate", "zstd, br", "identity", "gzip;q=1.0, br;q=0.8", "gzip ; q=0.5", "GZIP"],
        ["gzip, compress", "*", "gzip, gzip", "gzip;q=2", "gzip;level=9", "gzip,,br", "mario.rossi@acme.com", ""],
    ),
    (
        "anthropic",
        "x-claude-code-session-id",
        [UUID, UUID.upper()],
        [*BAD_COMMON, UUID + "0", UUID[:-1], UUID.replace("-", ""), "cc-mario-rossi", "{" + UUID + "}"],
    ),
    ("bedrock", "x-claude-code-session-id", [UUID], ["mario.rossi@acme.com"]),
    (
        "openai",
        "session-id",
        [UUID, "guardian:" + UUID, "memory_consolidation:" + UUID],
        [*BAD_COMMON, "mario:" + UUID, UUID + ":0", "s-1"],
    ),
    ("openai", "thread-id", [UUID], [*BAD_COMMON, "guardian:" + UUID, UUID + "a"]),
    ("chatgpt", "thread-id", [UUID], ["mario.rossi@acme.com"]),
    ("openai", "x-client-request-id", [UUID], [*BAD_COMMON, UUID + ":0"]),
    (
        "openai",
        "x-codex-window-id",
        [UUID + ":0", UUID + ":18446744073709551615"],
        [*BAD_COMMON, UUID, UUID + ":", UUID + ":-1", UUID + ":1" + "0" * 20, "mario:0"],
    ),
    ("chatgpt", "chatgpt-account-id", [UUID], [*BAD_COMMON, "account-mario-rossi", "a" * 64]),
]


@pytest.mark.parametrize(("target", "header", "accepted", "refused"), GRAMMARS)
def test_each_named_header_has_a_bounded_grammar_and_refuses_naming_itself(target, header, accepted, refused):
    for value in accepted:
        admission = _one(target, [(header, value)])
        assert (header, value) in admission.headers, (header, value)
        assert admission.removed == 0
    # Every grammar refuses spaces, an email, a path, an empty value and anything over its length, beside its own cases.
    for value in dict.fromkeys([*BAD_COMMON, "a" * 4096, *refused]):
        with pytest.raises(EnvelopeRefused) as refused_error:
            _one(target, [(header, value)])
        assert refused_error.value.code == "envelope-header-value", (value, refused_error.value.code)
        message = refused_error.value.agent_message()
        assert message.endswith(f": header {header}"), (value, message)
        assert not value or value not in message


@pytest.mark.parametrize(
    ("target", "header"),
    [
        ("gemini", "x-initiator"),  # Copilot's headers are named on its BYOK families only
        ("chatgpt", "x-interaction-type"),
        ("anthropic", "originator"),  # Codex's on the OpenAI and ChatGPT families
        ("anthropic", "x-codex-beta-features"),
        ("bedrock-list", "anthropic-beta"),  # Anthropic's on Bedrock's model calls, not its control plane
        ("gemini", "anthropic-version"),
        ("anthropic", "version"),
    ],
)
def test_a_named_header_outside_its_families_is_still_unknown(target, header):
    value = {"x-initiator": "user", "anthropic-version": "2023-06-01", "version": "0.162.0"}.get(header, "a")
    with pytest.raises(EnvelopeRefused) as refused:
        _one(target, [(header, value)])
    assert refused.value.code == "envelope-header-unknown"
    assert refused.value.agent_message().endswith(f": header {header}")


def test_the_session_and_request_ids_need_no_configured_value_but_no_other_resource_header_is_trusted():
    for name in ("openai-organization", "openai-project", "x-session-id"):
        with pytest.raises(EnvelopeRefused) as refused:
            _one("openai", [(name, UUID)])
        assert refused.value.code == "envelope-resource"
    # Hermes Agent's session_id is named by its own form (test_request_hermes.py), a UUID is not that form.
    with pytest.raises(EnvelopeRefused) as refused:
        _one("openai", [("session_id", UUID)])
    assert refused.value.code == "envelope-header-value"
    assert _one("openai", [("session_id", "20261103_143000_a1b2c3")]).profile.id == "openai.responses.1"
    for name in ("session-id", "thread-id", "x-client-request-id"):
        assert _one("openai", [(name, UUID)]).profile.id == "openai.responses.1"


@pytest.mark.parametrize(
    ("target", "header", "value"),
    [
        ("openai", "x-codex-turn-metadata", TURN_METADATA),
        ("chatgpt", "x-codex-turn-metadata", TURN_METADATA),
        ("bedrock", "amz-sdk-invocation-id", "9b2f6c1e-3a4d-4e5f-8a7b-6c5d4e3f2a1b"),
        ("bedrock-list", "amz-sdk-request", "attempt=1; max=3"),
        ("bedrock-list", "x-amz-user-agent", "aws-sdk-js/3.920.0 ua/2.1 os/win32"),
        ("gemini", "x-goog-api-client", "google-genai-sdk/1.50.0 gl-python/3.13.0"),
    ],
)
def test_declared_removable_families_are_removed_and_counted(target, header, value):
    admission = _one(target, [(header, value)])
    assert admission.removed == 1
    assert all(name.lower() != header for name, _ in admission.headers)


@pytest.mark.parametrize(
    ("target", "header", "value"),
    [
        ("bedrock", "x-amz-security-token", "FwoGZXIvYXdzEJr-fake-session-token"),
        ("bedrock-list", "x-amz-security-token", "FwoGZXIvYXdzEJr-fake-session-token"),
        ("bedrock", "authorization", "Bearer fake-bedrock-api-key"),
        ("bedrock", "x-amz-date", "20261103T120000Z"),
        ("bedrock", "x-amz-content-sha256", "a" * 64),
        ("gemini", "x-goog-api-key", "fake-gemini-key"),
        ("gemini", "authorization", "Bearer fake-google-token"),
        ("openai", "authorization", "Bearer fake-openai-key"),
        ("chatgpt", "authorization", "Bearer fake-chatgpt-token"),
        ("anthropic", "x-api-key", "fake-anthropic-key"),
    ],
)
def test_removable_families_never_strip_a_credential_or_signing_header(target, header, value):
    admission = _one(target, [(header, value)])
    assert admission.removed == 0
    assert (header, value) in admission.headers
    upstream, method, path = TARGETS[target]
    profile = rp._profile(upstream, method, readable_path(path))
    assert not header.startswith(profile.removable) and header not in profile.removable_exact


@pytest.mark.parametrize(
    ("target", "header"),
    [
        ("bedrock", "x-amz-target"),  # never the x-amz- prefix
        ("bedrock-list", "x-amz-fake-signing-material"),
        ("gemini", "x-goog-user-project"),  # never the x-goog- prefix: it selects the billing project
        ("gemini", "x-goog-request-params"),
        ("openai", "x-codex-turn-metadata-extra"),  # only the exact names of Codex's removable headers
        ("openai", "x-codex-installation-id"),
        ("vertex", "x-goog-api-client"),  # Vertex's Claude routes speak Anthropic's protocol: no Google family
        ("anthropic", "x-codex-turn-metadata"),
    ],
)
def test_neighbours_of_a_removable_family_are_refused_not_removed(target, header):
    with pytest.raises(EnvelopeRefused) as refused:
        _one(target, [(header, "value")])
    assert refused.value.code == "envelope-header-unknown"


def test_codex_model_list_names_client_version_on_command_auth_provider_routes():
    from latet.gateway.coverage import Envelope

    codex = [("authorization", "Bearer fake"), ("originator", "codex_cli_rs"), ("version", "0.162.0")]
    for upstream, path in (
        (OPENAI, "/openai/v1/models"),
        (
            destination_upstream(Destination(name="acme-codex", url="https://gateway.acme.invalid/v1")),
            "/acme-codex/models",
        ),
    ):
        for query, passes in (
            ("client_version=0.162.0", True),
            ("", True),
            ("client_version=private-value", False),
            ("client_version=0.162.0&client_version=0.162.1", False),
            ("client_version=0.162.0&unknown=1", False),
        ):
            envelope = Envelope(
                "http",
                "GET",
                path.encode(),
                query.encode(),
                tuple((k.encode(), v.encode()) for k, v in codex),
                b"",
                upstream.name,
            )
            if passes:
                assert rp.admit(inventory(envelope), upstream).profile.id == "openai.models.1"
            else:
                with pytest.raises(EnvelopeRefused):
                    rp.admit(inventory(envelope), upstream)


def test_the_manifest_declares_the_code_s_header_families():
    manifest = json.loads((Path(__file__).parents[1] / "data/request-coverage/profiles.json").read_text("utf-8"))
    policy = manifest["envelope"]["header_policy"]
    assert policy["removable_by_family"] == {
        family: {"prefixes": list(prefixes), "exact": list(exact)}
        for family, (prefixes, exact) in rp.FAMILY_REMOVABLE.items()
    }
    assert set(policy["id_grammars"]) == set(rp.ID_HEADERS)
    assert set(policy["resources_exact"]) == set(rp._RESOURCE_HEADERS)


# Claude Code 2.1.316's calls with one subagent and one nested subagent, against a fake Messages API (scratchpad
# rc\profiles-fix\cc_agent_probe.py, 2026-11-03): the header names in wire order; the agent ids were observed as "a"
# and 16 lowercase hex digits, and their source also allows a free-text label, so they are removed, never forwarded.
SUBAGENT_NAMES = [
    "accept", "content-type", "user-agent", "x-claude-code-session-id", "x-stainless-arch", "x-stainless-lang",
    "x-stainless-os", "x-stainless-package-version", "x-stainless-retry-count", "x-stainless-runtime",
    "x-stainless-runtime-version", "x-stainless-timeout", "anthropic-beta", "anthropic-dangerous-direct-browser-access",
    "anthropic-version", "x-api-key", "x-app", "x-claude-code-agent-id", "x-claude-code-parent-agent-id", "connection",
    "host", "accept-encoding", "content-length",
]  # fmt: skip


@pytest.mark.parametrize(
    ("target", "agent_id"),
    [
        ("anthropic", "a0f1e2d3c4b5a6978"),
        ("anthropic", "aresearcher-mario-rossi-0f1e2d3c4b5a6978"),  # the labeled form the source allows
        ("bedrock", "a0f1e2d3c4b5a6978"),
        ("vertex", "a0f1e2d3c4b5a6978"),
    ],
)
def test_claude_code_subagent_calls_pass_with_their_agent_ids_removed(target, agent_id):
    values = {
        **VALUES,
        "user-agent": USER_AGENTS["claude-code"],
        "accept": "application/json",
        "x-claude-code-agent-id": agent_id,
        "x-claude-code-parent-agent-id": "a9e8d7c6b5a4f3e2d",
    }
    headers = [
        (name, "0" if name.startswith("x-stainless-") else values[name])
        for name in SUBAGENT_NAMES
        if name not in REQUEST_DROP and (target == "anthropic" or name != "x-api-key")
    ]
    admission = _one(target, headers)
    sent = {name.lower() for name, _ in admission.headers}
    assert "x-claude-code-agent-id" not in sent and "x-claude-code-parent-agent-id" not in sent
    assert admission.removed == 2 + sum(name.startswith("x-stainless-") for name, _ in headers)
    assert "x-claude-code-session-id" in sent and "anthropic-beta" in sent


@pytest.mark.parametrize(
    ("target", "path_model"),
    [
        ("bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1%3A0"),
        ("bedrock", "anthropic.claude-opus-4-6-v1"),
        ("bedrock", "global.anthropic.claude-haiku-4-5-20251001-v1%3A0"),
        ("vertex", "claude-sonnet-4-5@20250929"),
        ("gemini", "gemini-2.5-flash"),
    ],
)
def test_documented_model_ids_in_a_path_pass_with_stock_settings(target, path_model):
    from latet.gateway.coverage import Envelope

    upstream, method, path = TARGETS[target]
    upstream = replace(upstream, envelope_expected=frozenset({("project", "p")}) if target == "vertex" else frozenset())
    original = {"bedrock": "approved", "vertex": "m", "gemini": "m"}[target]
    path = path.replace(f"/{original}/", f"/{path_model}/").replace(f"/{original}:", f"/{path_model}:")
    envelope = Envelope("http", method, path.encode(), b"", (), b"{}", upstream.name)
    assert rp.admit(inventory(envelope), upstream).profile.method == method


@pytest.mark.parametrize(
    ("upstream", "path"),
    [
        (BedrockUpstream(), "/bedrock/us-east-1/model/" + BEDROCK_ARN + "/invoke"),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles/" + BEDROCK_ARN),
        (BedrockUpstream(), "/bedrock/us-east-1/model/anthropic.claude-mario-rossi-v1%3A0/invoke"),
        (BedrockUpstream(), "/bedrock/us-east-1/inference-profiles/anthropic.claude-sonnet-4-5-20250929-v1%3A0"),
        (
            VertexUpstream(),
            "/vertex/v1/projects/acme-project/locations/us-east5/publishers/anthropic/models/"
            "claude-sonnet-4-5@20250929:rawPredict",
        ),
        (VertexUpstream(envelope_expected=frozenset({("project", "acme-project")})),
         "/vertex/v1/projects/acme-project/locations/us-east5/publishers/anthropic/models/claude-x:rawPredict"),
        (GEMINI, "/gemini/v1beta/models/your-model:generateContent"),
        (OPENAI, "/openai/v1/models/arbitrary-model"),
    ],
)  # fmt: skip
def test_account_bound_or_undocumented_path_resources_still_need_a_row(upstream, path):
    from latet.gateway.coverage import Envelope

    method = "GET" if "/inference-profiles/" in path or "/v1/models/" in path else "POST"
    body = b"" if method == "GET" else b"{}"
    envelope = Envelope("http", method, path.encode(), b"", (), body, upstream.name)
    with pytest.raises(EnvelopeRefused) as refused:
        rp.admit(inventory(envelope), upstream)
    assert refused.value.code == "envelope-resource"


# --- The agents' remaining headers, built from their sources' shapes (round 3) ---------------------------------

# (target, header, a value of the shape its source makes, "named" or "removed", anchor)
SOURCE_ROWS = [
    ("anthropic", "x-claude-remote-session-id", "session_01AbCdEf", "removed", "env CLAUDE_CODE_REMOTE_SESSION_ID"),
    ("anthropic", "x-claude-remote-container-id", "container-7", "removed", "env CLAUDE_CODE_CONTAINER_ID"),
    ("anthropic", "x-claude-remote-session-origin", "web", "removed", "env CLAUDE_CODE_SESSION_ORIGIN"),
    ("anthropic", "x-client-app", "acme-internal-tool/1.2", "removed", "env CLAUDE_AGENT_SDK_CLIENT_APP"),
    ("bedrock", "x-client-app", "acme-internal-tool/1.2", "removed", "env CLAUDE_AGENT_SDK_CLIENT_APP"),
    ("anthropic", "x-claude-code-prompt-id", "4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11", "named", "Kt(): UUID only"),
    ("anthropic", "x-claude-code-request-class", "subagent", "named", "Uvi(): main/subagent/auxiliary/compaction"),
    ("anthropic", "x-claude-code-request-class", "compaction", "named", "Uvi()"),
    ("anthropic", "x-claude-code-agent-type", "general-purpose", "named", "Bvi(): a built-in agent's name"),
    ("anthropic", "x-claude-code-agent-type", "teammate", "named", "Bvi()"),
    ("bedrock", "x-claude-code-agent-type", "custom", "named", "Bvi()"),
    ("anthropic", "x-anthropic-additional-protection", "true", "named", "env CLAUDE_CODE_ADDITIONAL_PROTECTION"),
    # Claude Code's gateway compatibility guide (code.claude.com/docs/en/llm-gateway-protocol, read 2026-11-05): the
    # hint headers it sends with CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 beside the three above.
    ("anthropic", "x-claude-code-compaction", "auto", "named", "guide: auto, manual or reactive"),
    ("anthropic", "x-claude-code-context-compacted", "reactive", "named", "guide: the compaction's values"),
    ("bedrock", "x-claude-code-compaction", "manual", "named", "guide: Bedrock with the hint headers on"),
    ("anthropic", "x-claude-code-prev-tool-durations", "Bash=742;Read=9", "removed", "guide: <name>=<ms>;..."),
    ("bedrock", "x-claude-code-prev-tool-durations", "mcp__acme-payroll__get_salary=12", "removed", "MCP names"),
    ("openai", "x-openai-subagent", "review", "removed", "subagent_header_value(): kinds or Other(label)"),
    ("chatgpt", "x-openai-subagent", "codex-guardian-reviewer", "removed", "SubAgentSource::Other(label)"),
    ("openai", "x-codex-parent-thread-id", "019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f", "named", "ThreadId"),
    ("chatgpt", "x-codex-turn-state", "eyJzdGlja3kiOiJyZWdpb24tMyJ9.opaque", "named", "provider's echoed token"),
    ("openai", "x-openai-memgen-request", "true", "named", "client.rs:797-800"),
    ("chatgpt", "x-codex-routing-hint", "model=gpt-5.2-codex", "named", "client.rs:1164-1180"),
    ("chatgpt", "x-codex-routing-hint", "model=gpt-5.2-codex;tier=priority", "named", "client.rs:1164-1180"),
    ("chatgpt", "x-responsesapi-include-timing-metrics", "true", "named", "client.rs:1331-1337"),
    ("openai", "x-openai-internal-codex-responses-lite", "true", "named", "client.rs:2355-2362"),
]


@pytest.mark.parametrize(("target", "header", "value", "treatment", "anchor"), SOURCE_ROWS)
def test_the_agents_remaining_headers_are_named_or_removed_never_refused(target, header, value, treatment, anchor):
    admission = _one(target, [(header, value)])
    if treatment == "removed":
        assert admission.removed == 1 and all(name.lower() != header for name, _ in admission.headers), anchor
    else:
        assert admission.removed == 0 and (header, value) in admission.headers, anchor


NAMED_GRAMMARS = [
    ("anthropic", "x-claude-code-prompt-id", ["4f6c1a52-0d1e-4b8e-9a37-2c5e8d0b7a11"], ["4f6c1a52", "prompt-1"]),
    ("anthropic", "x-claude-code-request-class", ["main", "auxiliary", "workflow"], ["Main", "a" * 33, "sub agent"]),
    ("anthropic", "x-claude-code-agent-type", ["Explore", "statusline-setup"], ["-x", "a" * 65, "Mario Rossi"]),
    ("anthropic", "x-anthropic-additional-protection", ["true"], ["false", "yes"]),
    ("anthropic", "x-claude-code-compaction", ["auto", "manual", "reactive"], ["idle", "auto,manual", "Mario Rossi"]),
    ("bedrock", "x-claude-code-context-compacted", ["auto", "manual", "reactive"], ["true", "yes"]),
    ("openai", "x-codex-parent-thread-id", ["019a4c2e-7d1b-7c3e-9f2a-6b5d4e3c2a1f"], ["019a4c2e", "guardian:x"]),
    ("chatgpt", "x-openai-memgen-request", ["true"], ["false"]),
    (
        "chatgpt",
        "x-codex-turn-state",
        ["eyJzdGlja3kiOiJyZWdpb24tMyJ9.opaque", "0f1e2d3c4b5a6978", "a" * 1024, "dG9rZW4="],
        ["a" * 1025, "token with space", "a/b", "a:b", "mario.rossi@acme.com"],
    ),
    (
        "chatgpt",
        "x-codex-routing-hint",
        ["model=o4-mini;tier=flex", "model=gpt-5"],
        ["model=mario-rossi", "model=gpt-5;tier=mario", "tier=flex", "model=gpt-5;tier=flex;x=1"],
    ),
    ("chatgpt", "x-responsesapi-include-timing-metrics", ["true"], ["1"]),
    ("openai", "x-openai-internal-codex-responses-lite", ["true"], ["false"]),
]


@pytest.mark.parametrize(("target", "header", "accepted", "refused"), NAMED_GRAMMARS)
def test_each_new_named_header_refuses_free_text_naming_itself(target, header, accepted, refused):
    test_each_named_header_has_a_bounded_grammar_and_refuses_naming_itself(target, header, accepted, refused)


@pytest.mark.parametrize(
    ("target", "header"),
    [
        ("openai", "x-claude-code-request-class"),  # Claude Code's on the Anthropic and Bedrock families only
        ("openai", "x-claude-code-compaction"),
        ("gemini", "x-claude-code-prev-tool-durations"),
        ("gemini", "x-claude-remote-session-id"),
        ("anthropic", "x-codex-routing-hint"),  # Codex's on the OpenAI and ChatGPT families only
        ("gemini", "x-openai-subagent"),
    ],
)
def test_the_new_headers_outside_their_families_stay_unknown(target, header):
    with pytest.raises(EnvelopeRefused) as refused:
        _one(target, [(header, "true")])
    assert refused.value.code == "envelope-header-unknown"


# A Codex subagent turn and a Claude Code call with its gateway hint headers on, as their sources build them.
CODEX_SUBAGENT_NAMES = [
    "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata", "x-codex-parent-thread-id",
    "x-openai-subagent", "x-codex-turn-state", "x-codex-routing-hint", "x-openai-memgen-request",
    "x-client-request-id", "session-id", "thread-id", "accept", "content-type", "authorization", "originator",
    "user-agent",
]  # fmt: skip
CLAUDE_HINT_NAMES = [
    "accept", "content-type", "user-agent", "x-claude-code-session-id", "anthropic-version", "x-api-key", "x-app",
    "x-claude-code-agent-id", "x-claude-code-request-class", "x-claude-code-agent-type", "x-claude-code-prompt-id",
    "x-claude-remote-session-id", "x-client-app", "x-anthropic-additional-protection", "x-claude-code-compaction",
    "x-claude-code-context-compacted", "x-claude-code-prev-tool-durations",
]  # fmt: skip


@pytest.mark.parametrize(
    ("target", "names", "removed"),
    [
        ("chatgpt", CODEX_SUBAGENT_NAMES, {"x-codex-turn-metadata", "x-openai-subagent"}),
        (
            "anthropic",
            CLAUDE_HINT_NAMES,
            {
                "x-claude-code-agent-id",
                "x-claude-remote-session-id",
                "x-client-app",
                "x-claude-code-prev-tool-durations",
            },
        ),
    ],
)
def test_source_shaped_agent_calls_pass_with_only_their_free_text_headers_removed(target, names, removed):
    shaped = {**VALUES, **{row[1]: row[2] for row in SOURCE_ROWS}, "x-claude-code-agent-id": "a0f1e2d3c4b5a6978"}
    shaped |= {"user-agent": USER_AGENTS["codex" if target == "chatgpt" else "claude-code"]}
    shaped["accept"] = "text/event-stream" if target == "chatgpt" else "application/json"
    admission = _one(target, [(name, shaped[name]) for name in names])
    assert {name.lower() for name, _ in admission.headers} == set(names) - removed
    assert admission.removed == len(removed)


def test_vertex_express_takes_model_ids_by_grammar_and_its_key_as_a_credential():
    from latet.gateway.cloud import VERTEX_EXPRESS
    from latet.gateway.coverage import Envelope

    def admit(path: str, headers=(), query: bytes = b""):
        envelope = Envelope("http", "POST", path.encode(), query, tuple(headers), b"{}", VERTEX_EXPRESS.name)
        return rp.admit(inventory(envelope), VERTEX_EXPRESS)

    path = "/gemini/v1beta1/publishers/google/models/gemini-2.5-flash:generateContent"
    admission = admit(path, [(b"x-goog-api-key", b"fake-vertex-express-key")])
    assert admission.profile.family == "gemini" and admission.treatments[-1] == "authorized"
    assert admit(path, query=b"key=fake-vertex-express-key").profile.id == "gemini.v1beta1.generateContent.1"
    resolved = VERTEX_EXPRESS.resolve("POST", path, "")
    assert resolved is not None and resolved.host == "aiplatform.googleapis.com"
    with pytest.raises(EnvelopeRefused) as refused:
        admit("/gemini/v1beta1/publishers/google/models/your-model:generateContent")
    assert refused.value.code == "envelope-resource"
    with pytest.raises(EnvelopeRefused) as refused:
        admit("/gemini/v1beta1/publishers/anthropic/models/claude-sonnet-4-5:rawPredict")
    assert refused.value.code == "envelope-route"
