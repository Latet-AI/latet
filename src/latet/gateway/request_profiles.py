"""Versioned, immutable HTTP envelope profiles. Body/opaque/WS policy is deliberately unchanged in P2-1.

Selection uses a configured route identity and exact method/path template. Neither a host suffix nor an agent
marker widens an envelope. Values with no finite protocol grammar are checked by the final verifier, never masked.
Arbitrary resource IDs require exact construction-injected configuration; a missing expectation loses permission.
An agent's per-session and per-request correlation ids are not resources: each passes only within the closed id
grammar its agent's source fixes, and selects nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit

from latet.engine.response_guard import safe_text
from latet.gateway.cloud import BedrockUpstream, VertexUpstream, vertex_host
from latet.gateway.coverage import EnvelopeRefused, Inventory
from latet.gateway.headers import DROP_PREFIXES, REQUEST_DROP, WS_HANDSHAKE_DROP
from latet.gateway.model_ids import OPENAI, path_model
from latet.gateway.routes import Upstream, readable_path

REVISION = "http-envelope-1"
# The earlier revisions whose request exceptions carry over to this one unchanged (R-100, RC-REC-6): listed by the
# revision that keeps every component an exception may name (a header or query parameter no profile names, on the same
# profile id) at the same path, type and treatment. An exception approved under any other revision is suspended: it
# refuses, and is shown as needing approval again. Nothing widens by an upgrade.
CARRIES_FROM: frozenset[str] = frozenset()
WS_REVISION = "ws-envelope-1"


def opaque_scope(upstream: Upstream, method: str, path: str, protocol: str, query: str = ""):
    """Bind opaque evidence to the selected destination/resources, without credential selectors."""
    from latet.gateway.provenance import EchoScope
    from latet.gateway.request_policy import REVISION as policy_revision

    try:
        profile = _profile(upstream, method, path)
        resolved = upstream.resolve(method, path, "")
    except (EnvelopeRefused, ValueError):
        return None
    if resolved is None:
        return None
    origin = upstream.provider_origin
    # Native clouds are closed constructors, not a configured name or host suffix. Their test endpoint overrides
    # deliberately lose fallback authority; exact observed provenance remains usable on their own destination.
    if type(upstream) is BedrockUpstream and upstream.runtime == BedrockUpstream().runtime:
        origin = "bedrock"
    elif type(upstream) is VertexUpstream and upstream.base is None:
        origin = "vertex"
    eligible = {
        "anthropic": {"anthropic", "bedrock", "vertex"},
        "responses": {"openai", "chatgpt"},
        "gemini": {"gemini", "vertex"},
    }.get(protocol, set())
    selectors = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(query, keep_blank_values=True)
            if k.lower() not in {"key", "api_key", "api-key", "access_token"}
        ]
    )
    logical = {"anthropic": {"anthropic", "bedrock"}, "responses": {"openai", "chatgpt"}, "gemini": {"gemini"}}.get(
        protocol, set()
    )
    return EchoScope(
        (upstream.name, upstream.base_url, resolved.url, selectors, profile.id, policy_revision),
        protocol,
        origin in eligible and profile.family in logical and not profile.bodyless,
    )


# Explicit revision snapshot, not REGION_NAME/_LOCATION's open grammars. Additions require fixtures/review.
BEDROCK_REGIONS = frozenset(
    {
        "af-south-1",
        "ap-east-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-south-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-southeast-5",
        "ap-southeast-6",
        "ap-southeast-7",
        "ca-central-1",
        "ca-west-1",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "mx-central-1",
        "sa-east-1",
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "us-gov-east-1",
        "us-gov-west-1",
    }
)
VERTEX_LOCATIONS = frozenset(
    {
        "global",
        "us",
        "eu",
        "africa-south1",
        "asia-east1",
        "asia-east2",
        "asia-northeast1",
        "asia-northeast2",
        "asia-northeast3",
        "asia-south1",
        "asia-south2",
        "asia-southeast1",
        "asia-southeast2",
        "australia-southeast1",
        "australia-southeast2",
        "europe-central2",
        "europe-north1",
        "europe-north2",
        "europe-southwest1",
        "europe-west1",
        "europe-west2",
        "europe-west3",
        "europe-west4",
        "europe-west6",
        "europe-west8",
        "europe-west9",
        "europe-west10",
        "europe-west12",
        "me-central1",
        "me-central2",
        "me-west1",
        "northamerica-northeast1",
        "northamerica-northeast2",
        "northamerica-south1",
        "southamerica-east1",
        "southamerica-west1",
        "us-central1",
        "us-east1",
        "us-east4",
        "us-east5",
        "us-south1",
        "us-west1",
        "us-west2",
        "us-west3",
        "us-west4",
    }
)

_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BAD_ESCAPE = re.compile(rb"%(?![0-9a-fA-F]{2})")
_VERSION = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
# A model list's cursors (D1, pagination bounded; G2 review M-1), which no owner can list in advance. Anthropic's
# after_id and before_id are the ids of the models it lists: Claude's model-id grammar (model_ids). Google's pageToken
# and AWS's nextToken are opaque tokens, bounded here and checked as text by the final check: AWS documents nextToken
# as 1 to 2048 characters of no whitespace (ListInferenceProfiles: \S*), read here within printable ASCII; Google
# documents neither the characters nor the length of its page tokens, which are base64 (standard or URL-safe), bounded
# as AWS's.
_MODEL_CURSORS = frozenset({"after_id", "before_id"})
_OPAQUE_CURSORS = {
    "pageToken": re.compile(r"[A-Za-z0-9+/=_-]{1,2048}"),
    "nextToken": re.compile(r"[!-~]{1,2048}"),
}
# Selection authority (billing organization and project) and ids without a source-fixed grammar: exact owner rows only.
_RESOURCE_HEADERS = frozenset({"openai-organization", "openai-project", "x-session-id"})
_UUID = "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
# Hermes Agent 25ce7ccf: its session id, new_session_id() = f"{now:%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
# (hermes_state_ids.py:53-60), and its content cache key "pck_" + sha256[:24] (agent/transports/codex.py:428-449).
HERMES_SESSION = "[0-9]{8}_[0-9]{6}_[0-9a-f]{6}"
HERMES_CACHE_KEY = "pck_[0-9a-f]{24}"
# Correlation ids, fresh per session or request, each with a closed grammar taken from its agent's source. A grammar of
# hex and fixed punctuation carries no free text; the id never selects a project, an identity or an exception (D7).
ID_HEADERS = {
    # Claude Code 2.1.312-2.1.316: "Invalid session ID. Must be a valid UUID." (--session-id), crypto.randomUUID().
    "x-claude-code-session-id": re.compile(_UUID),
    # Codex c1382380: responses_session_id (core/src/client.rs:597-603) is the session's ThreadId (Uuid::now_v7,
    # protocol/src/thread_id.rs), or prompt_cache_key's "{InternalSessionSource}:{parent}" (memory_consolidation,
    # guardian; protocol.rs:3100-3107) or the review override "guardian:{parent}" (guardian/review_session.rs:286-298).
    "session-id": re.compile(f"(?:(?:memory_consolidation|guardian):)?{_UUID}"),
    # Codex c1382380: thread-id and x-client-request-id are the ThreadId (codex-api/src/endpoint/responses.rs:86-90,
    # requests/headers.rs:5-14; core/src/client.rs:1312-1318 for the WebSocket handshake).
    "thread-id": re.compile(_UUID),
    # Hermes Agent 25ce7ccf agent/transports/codex.py:864-872 sends x-client-request-id as its prompt_cache_key, else
    # its bounded cache scope, the session id (codex.py:31-55, 798-806, 869).
    "x-client-request-id": re.compile(f"{_UUID}|{HERMES_CACHE_KEY}|{HERMES_SESSION}"),
    # Hermes Agent: ChatGPT's session_id header is its session id (codex.py:864-872); a cron job's free name is not.
    "session_id": re.compile(HERMES_SESSION),
    # Codex c1382380: "{thread_id}:{window_number}", a u64 (core/src/session/mod.rs:4523-4533).
    "x-codex-window-id": re.compile(f"{_UUID}:[0-9]{{1,20}}"),
    # Codex c1382380: the ChatGPT id token's chatgpt_account_id (login/src/token_data.rs:85-120), a workspace UUID
    # (config/src/config_toml.rs:130, forced_chatgpt_workspace_id).
    "chatgpt-account-id": re.compile(_UUID),
    # Codex c1382380 core/src/responses_metadata.rs:394-400: a subagent's parent ThreadId.
    "x-codex-parent-thread-id": re.compile(_UUID),
    # Claude Code 2.1.316 (gateway hint headers): sent only when its validator /^[0-9a-f]{8}-...-[0-9a-f]{12}$/i passes.
    "x-claude-code-prompt-id": re.compile(_UUID),
}
_FREE_HEADERS = frozenset({"user-agent", "accept-language"})
# Anthropic's beta feature names (not user data): a bounded token list in place of a fixed set of names.
_BETA = re.compile(r"[a-z0-9-]{1,64}")
_BETAS = 32
# Copilot CLI 1.0.103 package/schemas/api.schema.json declares X-Interaction-Type an open string (conversation-agent,
# -subagent, -sampling, -background, -compaction, -user); VS Code's Copilot adds conversation-panel and others.
_INTERACTION = re.compile(r"[a-z0-9_-]{1,32}")
# Codex c1382380 login/src/auth/default_client.rs:42-150: DEFAULT_ORIGINATOR and the first-party originators, else a
# product id set by its embedder or CODEX_INTERNAL_ORIGINATOR_OVERRIDE (checked by the final check, never inspected).
# One token, no spaces, except Codex's own first-party "Codex <app>" form (is_first_party_originator).
_ORIGINATOR = re.compile(r"(?=.{1,64}\Z)(?:Codex )?[A-Za-z0-9][A-Za-z0-9._-]*")
_FIRST_PARTY_ORIGINATORS = frozenset(
    {"codex_cli_rs", "codex-tui", "codex_vscode", "codex_atlas", "codex_chatgpt_desktop"}
)
# Codex c1382380 core/src/session/mod.rs:1245-1258: the enabled experimental FeatureSpec keys joined by commas
# (features/src/lib.rs keys are snake_case; one has a dot).
_FEATURE = re.compile(r"[a-z0-9_.]{1,64}")
_FEATURES = 32
_CODINGS = frozenset({"identity", "gzip", "deflate", "br", "zstd"})
_WEIGHT = re.compile(r"q=(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)")
# Declared removable header families per protocol family (D4), beside the SDK telemetry and tracing every profile
# declares. Never a prefix that holds a credential or signing material (x-amz-, x-goog-).
_CLAUDE_CODE_AGENT_IDS = ("x-claude-code-agent-id", "x-claude-code-parent-agent-id")
# Claude Code 2.1.316 copies these from environment values (CLAUDE_CODE_REMOTE_SESSION_ID, CLAUDE_CODE_CONTAINER_ID,
# CLAUDE_CODE_SESSION_ORIGIN, CLAUDE_AGENT_SDK_CLIENT_APP): whatever text they hold, so removed.
_CLAUDE_CODE_ENVIRONMENT = (
    "x-claude-remote-session-id",
    "x-claude-remote-container-id",
    "x-claude-remote-session-origin",
    "x-client-app",
)
# Codex c1382380: x-openai-subagent is a subagent's kind or SubAgentSource::Other's free label (responses_metadata.rs:
# 458-475), so removed.
_CODEX_FREE = ("x-openai-subagent",)
# Codex c1382380 client.rs:2337-2352: x-codex-turn-state echoes the provider's own opaque sticky-routing token from the
# turn's first answer, never the user's content: a base64url, hex or dotted token, at most 1024 characters.
_TURN_STATE = re.compile(r"[A-Za-z0-9_.=-]{1,1024}")
FAMILY_REMOVABLE = {
    # Claude Code 2.1.316 on a subagent's calls: observed as "a" and 16 lowercase hex digits, but its id function
    # makes f"a{label}-{hex}" given a label, taken from an agent name hint ([\w-]{1,63}): free text, so removed.
    "anthropic": ((), (*_CLAUDE_CODE_AGENT_IDS, *_CLAUDE_CODE_ENVIRONMENT)),
    # AWS SDK for JavaScript v3 client telemetry and retry metadata (Claude Code's Bedrock control-plane reads).
    "bedrock": (("amz-sdk-",), ("x-amz-user-agent", *_CLAUDE_CODE_AGENT_IDS, *_CLAUDE_CODE_ENVIRONMENT)),
    # Google client library telemetry (google-genai-sdk/<v> gl-python/<v>, gl-node/<v>).
    "gemini": ((), ("x-goog-api-client",)),
    # Codex c1382380 responses_metadata.rs:377-405: a compatibility copy of the body's client_metadata, holding the
    # repository's absolute path, its git remote URLs and HEAD (turn_metadata.rs:573-597). Never forwarded.
    "openai": ((), ("x-codex-turn-metadata", *_CODEX_FREE)),
    "chatgpt": ((), ("x-codex-turn-metadata", *_CODEX_FREE)),
}
# Claude Code 2.1.316's gateway hint headers (CLAUDE_CODE_GATEWAY_HINT_HEADERS): a request class from its own mapping
# (main, subagent, auxiliary, compaction, workflow) and a built-in agent's name, "custom" or "teammate".
_REQUEST_CLASS = re.compile(r"[a-z_]{1,32}")
_AGENT_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
# Codex c1382380 core/src/client.rs:1164-1180 (Codex backend only): model={model}[;tier={service tier}].
_ROUTING_HINT = re.compile(rf"model=(?:{OPENAI.pattern})(?:;tier=(?:auto|default|flex|priority))?")


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    family: str
    method: str
    template: str
    query: tuple[str, ...] = ()
    bodyless: bool = False
    required_query: tuple[str, ...] = ()
    removable: tuple[str, ...] = ("x-stainless-", "x-datadog-", "x-b3-")
    selection_inputs: tuple[str, ...] = ("configured destination", "method", "exact route template")
    credential_headers: tuple[str, ...] | None = None
    removable_exact: tuple[str, ...] = ("traceparent", "tracestate", "baggage")


@dataclass(frozen=True, slots=True, repr=False)
class Admission:
    inventory: Inventory
    profile: Profile
    headers: tuple[tuple[str, str], ...]
    checked: tuple[str, ...]
    treatments: tuple[str, ...]
    removed: int
    # Each header or query parameter no profile names that a stored exception lets through: the exceptions that name
    # it (the identity's first) and what it would have been refused as without them. Which one applies, its lifetime
    # and its project are judged once the request's binding is known (``request_policy.granted``), before anything is
    # decided.
    exceptions: tuple[tuple[tuple[Any, ...], EnvelopeRefused], ...] = ()
    # The headers sent as they came, never read (``authorized``: a destination's credential, a profile's or one an
    # exception names, D4), by their lowered names: never recorded in clear, whatever their names (I-3).
    unread: frozenset[str] = frozenset()


def _excepted(upstream: Upstream, profile: Profile, kind: str, name: str) -> tuple[tuple[Any, ...], str] | None:
    """The approved exceptions of this destination and this very profile (its method, route and transport) that name
    a ``kind`` component ``name`` exactly, the identity's first (what applies without a binding is never displaced by
    what needs one), every project's after them (I-5: which applies is judged once the request's project is known),
    and the treatment their roles give: ``authorized`` (a destination's credential, never read) only when each of them
    says so, ``checked`` (its value passes the final check) otherwise, so that the one that applies never lets more
    through than it names. None: no exception names it."""
    identity: list[Any] = []
    projects: list[Any] = []
    roles: set[str] = set()
    for row in upstream.envelope_exceptions:
        if row.profile != profile.id:
            continue
        for component in row.components:
            if component.kind == kind and component.name == name:
                (identity if row.project is None else projects).append(row)
                roles.add(component.role)
                break
    if not identity and not projects:
        return None
    return (*identity, *projects), "authorized" if roles == {"credential"} else "checked"


def _expected(upstream: Upstream, role: str, value: str) -> None:
    if (role, value) not in upstream.envelope_expected:
        raise EnvelopeRefused("envelope-resource")


def _model(upstream: Upstream, kind: str, value: str, role: str = "model") -> None:
    # A provider's documented model id passes by its grammar (model_ids); anything else, an ARN included, needs a row.
    if not path_model(kind, value):
        _expected(upstream, role, value)


def _decoded(raw: bytes, *, plus: bool = False) -> str:
    if _BAD_ESCAPE.search(raw):
        raise EnvelopeRefused("envelope-escape")
    try:
        text = unquote_to_bytes(raw.replace(b"+", b" ") if plus else raw).decode("utf-8")
    except UnicodeError:
        raise EnvelopeRefused("envelope-encoding") from None
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise EnvelopeRefused("envelope-control")
    return text


def _profile(upstream: Upstream, method: str, path: str) -> Profile:
    profile = _selected(upstream, method, path)
    prefixes, exact = FAMILY_REMOVABLE.get(profile.family, ((), ()))
    if not prefixes and not exact:
        return profile
    return replace(profile, removable=profile.removable + prefixes, removable_exact=profile.removable_exact + exact)


def refused_template(upstream: Upstream, method: str, wire: str) -> str:
    """The route template a request refused for its envelope is recorded under (D4, R-79): its profile's, which no
    request-supplied token is part of, or the unsupported route's placeholder when no profile names its route."""
    try:
        return _profile(upstream, method, readable_path(wire)).template
    except (ValueError, LookupError):  # EnvelopeRefused, or a path no profile reads
        return "/{destination}/{unsupported-route}"


def _selected(upstream: Upstream, method: str, path: str) -> Profile:
    rest = path[len(upstream.prefix) :] or "/"
    family = upstream.name
    if upstream.request_profile != "standard":
        return _destination_profile(upstream, method, rest)
    if family == "bedrock":
        region, _, rest = rest.lstrip("/").partition("/")
        if region not in BEDROCK_REGIONS:
            raise EnvelopeRefused("envelope-region")
        rest = "/" + rest
        control = re.match(r"/(?:profile/[A-Za-z0-9_.+=,@-]{1,64}/[0-9a-f]{32}|session/[0-9a-f]{32})(?=/)", rest)
        if control:
            rest = rest[control.end() :]
        if method == "POST" and (
            call := re.fullmatch(r"/model/([^/]+)/(invoke|invoke-with-response-stream|count-tokens)", rest)
        ):
            _model(upstream, "bedrock", _decoded(call[1].encode("ascii")))
            return Profile(f"bedrock.{call[2]}.1", family, method, "/bedrock/{region}/model/{model}/{operation}")
        if method == "GET" and rest == "/inference-profiles":
            return Profile(
                "bedrock.inference-profiles.1",
                family,
                method,
                "/bedrock/{region}/inference-profiles",
                ("type", "maxResults", "nextToken"),
                True,
            )
        if method == "GET" and rest.startswith("/inference-profiles/") and rest.count("/") == 2:
            _model(upstream, "bedrock-profile", _decoded(rest.rsplit("/", 1)[1].encode("ascii")), "inference-profile")
            return Profile(
                "bedrock.inference-profile.1",
                family,
                method,
                "/bedrock/{region}/inference-profiles/{id}",
                bodyless=True,
            )
    elif family == "vertex":
        if rest.startswith("/projects/"):
            rest = "/v1" + rest
        located = re.match(r"/(v1|v1beta1)/projects/([^/]+)/locations/([^/]+)(/.*)$", rest)
        if located:
            if located[3] not in VERTEX_LOCATIONS:
                raise EnvelopeRefused("envelope-location")
            _expected(upstream, "project", _decoded(located[2].encode("ascii")))
            version, rest = located[1], located[4]
        else:
            version, _, tail = rest.lstrip("/").partition("/")
            rest = "/" + tail
            if version not in {"v1", "v1beta1"}:
                raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")
        anthropic = re.fullmatch(
            r"/publishers/anthropic/models/([^/:]+)(?::|%3[Aa])(rawPredict|streamRawPredict)", rest
        )
        if anthropic and method == "POST" and version == "v1" and located:
            _model(upstream, "vertex", _decoded(anthropic[1].encode("ascii")))
            return Profile(
                f"vertex.anthropic.{anthropic[2]}.1",
                "anthropic",
                method,
                "/vertex/v1/projects/{project}/locations/{location}/publishers/anthropic/models/{model}:{operation}",
            )
        if rest.startswith("/publishers/google/"):
            rest = rest[len("/publishers/google") :]
        return _google(upstream, method, rest, version, vertex=True)
    elif family == "vertex-express":
        # GenAI's Vertex AI express mode (an API key, no project), under latet run's Gemini base URL: Vertex's own
        # publishers/google/models/{model}:{operation}, a model id by grammar, the key a credential (VERTEX_EXPRESS).
        if rest.startswith("/google/"):
            return _google(upstream, method, rest[len("/google") :], "v1beta1", vertex=True)
    elif family == "chatgpt":
        if method == "GET" and rest == "/models":
            return Profile(
                "chatgpt.codex.models.1",
                family,
                method,
                "/backend-api/codex/models",
                ("client_version",),
                True,
                ("client_version",),
            )
        if method == "POST" and rest == "/responses":
            return Profile("chatgpt.codex.responses.1", family, method, "/backend-api/codex/responses")
    else:
        if family not in {"anthropic", "openai", "openrouter", "gemini"}:
            company_cloud = _company_cloud(upstream, method, rest)
            if company_cloud is not None:
                return company_cloud
        base_path = urlsplit(upstream.base_url).path.rstrip("/")
        if base_path in {"/api/v1", "/models"} or re.fullmatch(r"/openai/deployments/[^/]+", base_path):
            # Deployment is fixed by owner configuration, never a free path selector supplied by the request.
            rest = "/v1" + rest
        else:
            rest = base_path + rest
        # Configured base paths are fixed owner input (Azure deployments and proxy prefixes, for example).
        if family == "openrouter" and rest.startswith("/api/"):
            rest = rest[4:]
        if family == "gemini" or (
            family not in {"anthropic", "openai", "openrouter"}
            and re.match(r"/(v1beta|v1beta1)/(models|cachedContents)(?:/|$)", rest)
        ):
            version, _, tail = rest.lstrip("/").partition("/")
            return _google(upstream, method, "/" + tail, version)
        selected = {
            ("POST", "/v1/messages"): ("anthropic", "messages"),
            ("POST", "/v1/messages/count_tokens"): ("anthropic", "count_tokens"),
            ("POST", "/v1/chat/completions"): ("openai", "chat"),
            ("POST", "/v1/completions"): ("openai", "completions"),
            ("POST", "/v1/responses"): ("openai", "responses"),
            ("POST", "/v1/embeddings"): ("openai", "embeddings"),
        }.get((method, rest))
        if selected:
            protocol, operation = selected
            if family in {"anthropic", "openai", "openrouter"} and (family == "anthropic") != (protocol == "anthropic"):
                raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")
            return Profile(
                f"{protocol}.{operation}.1",
                protocol,
                method,
                f"/{{destination}}{rest}",
                ("beta",) if protocol == "anthropic" else _configured_query(upstream),
            )
        if method == "GET" and rest == "/v1/models":
            protocol = "anthropic" if family == "anthropic" else "openai"
            # Codex 0.162.0 lists a command-auth provider's models with ?client_version= (models-manager/src/
            # manager.rs:675-679, codex-api/src/endpoint/models.rs:33-45), the ChatGPT profile's grammar. Anthropic's
            # SDK lists them with ?beta=true too (client.beta.models.list(); P2-8a, root's ruling 6), the same
            # bounded value as on messages.
            query = (
                ("beta", "limit", "before_id", "after_id")
                if protocol == "anthropic"
                else (*_configured_query(upstream), "client_version")
            )
            return Profile(f"{protocol}.models.1", protocol, method, "/{destination}/v1/models", query, True)
        if method == "GET" and (model := re.fullmatch(r"/v1/models/([^/]+)", rest)):
            protocol = "anthropic" if family == "anthropic" else "openai"
            _model(upstream, protocol, _decoded(model[1].encode("ascii")))
            return Profile(f"{protocol}.model.1", protocol, method, "/{destination}/v1/models/{model}", bodyless=True)
    raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")


def _configured_query(upstream: Upstream) -> tuple[str, ...]:
    # Exact resource expectations fill declared roles; they cannot declare a query/header policy.
    return ("api-version",) if upstream.request_profile in {"azure-openai-1", "foundry-1"} else ()


def _destination_profile(upstream: Upstream, method: str, rest: str) -> Profile:
    """A closed trusted settings selector, never inferred from resources, request data or a host suffix."""
    selected = upstream.request_profile
    base_path = urlsplit(upstream.base_url).path.rstrip("/")
    operations = {"/chat/completions": "chat", "/embeddings": "embeddings"}
    if upstream.name in {"anthropic", "openai", "openrouter", "chatgpt", "gemini", "bedrock", "vertex"}:
        raise EnvelopeRefused("envelope-profile")
    if selected == "azure-openai-1":
        if re.fullmatch(r"/openai/deployments/[^/]+", base_path):
            operations["/completions"] = "completions"
        elif base_path == "/openai":
            operations = {"/responses": "responses"}
        else:
            raise EnvelopeRefused("envelope-profile")
    elif selected == "foundry-1":
        if base_path not in {"", "/models"}:
            raise EnvelopeRefused("envelope-profile")
    else:
        raise EnvelopeRefused("envelope-profile")
    if method != "POST" or rest not in operations:
        raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")
    prefix = "azure-openai" if selected == "azure-openai-1" else "foundry"
    return Profile(
        f"{prefix}.{operations[rest]}.1",
        "openai",
        method,
        f"/{{destination}}{rest}",
        query=("api-version",),
        required_query=("api-version",),
        selection_inputs=(
            "exact configured destination and base URL",
            "trusted Destination.request_profile",
            "method",
            "exact route template",
        ),
        credential_headers=("authorization", "api-key"),
    )


def _company_cloud(upstream: Upstream, method: str, rest: str) -> Profile | None:
    """Exact existing cloud inference paths on a configured destination; they confer no cloud-host authority."""
    if method != "POST":
        return None
    call = re.fullmatch(r"/model/([^/]+)/(invoke|invoke-with-response-stream|count-tokens)", rest)
    if call:
        _model(upstream, "bedrock", _decoded(call[1].encode("ascii")))
        return Profile(f"company.bedrock.{call[2]}.1", "bedrock", method, "/{destination}/model/{model}/{operation}")
    vertex = re.fullmatch(
        r"/v1/projects/([^/]+)/locations/([^/]+)/publishers/anthropic/models/([^/:]+):(rawPredict|streamRawPredict)",
        rest,
    )
    if vertex:
        if vertex[2] not in VERTEX_LOCATIONS:
            raise EnvelopeRefused("envelope-location")
        _expected(upstream, "project", _decoded(vertex[1].encode("ascii")))
        _model(upstream, "vertex", _decoded(vertex[3].encode("ascii")))
        return Profile(
            f"company.vertex.{vertex[4]}.1",
            "anthropic",
            method,
            "/{destination}/v1/projects/{project}/locations/{location}/publishers/anthropic/models/{model}:{operation}",
        )
    return None


def _google(upstream: Upstream, method: str, rest: str, version: str, *, vertex: bool = False) -> Profile:
    if version not in ({"v1", "v1beta1"} if vertex else {"v1", "v1beta"}):
        raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")
    query = ("key", "alt")
    if method == "GET" and rest == "/models":
        return Profile(
            f"gemini.{version}.models.1",
            "gemini",
            method,
            "/{destination}/{version}/models",
            (*query, "pageSize", "pageToken"),
            True,
        )
    if method == "GET" and (model := re.fullmatch(r"/models/([^/:]+)", rest)):
        _model(upstream, "google", _decoded(model[1].encode("ascii")))
        return Profile(
            f"gemini.{version}.model.1", "gemini", method, "/{destination}/{version}/models/{model}", query, True
        )
    call = re.fullmatch(
        r"/models/([^/:]+)(?::|%3[Aa])(generateContent|streamGenerateContent|countTokens|embedContent|batchEmbedContents)",
        rest,
    )
    if method == "POST" and call:
        _model(upstream, "google", _decoded(call[1].encode("ascii")))
        return Profile(
            f"gemini.{version}.{call[2]}.1",
            "gemini",
            method,
            "/{destination}/{version}/models/{model}:{operation}",
            query,
        )
    if method == "POST" and rest == "/cachedContents":
        return Profile(
            f"gemini.{version}.cache-create.1", "gemini", method, "/{destination}/{version}/cachedContents", query
        )
    if method == "PATCH" and (cache := re.fullmatch(r"/cachedContents/([^/]+)", rest)):
        _expected(upstream, "cache", _decoded(cache[1].encode("ascii")))
        return Profile(
            f"gemini.{version}.cache-patch.1",
            "gemini",
            method,
            "/{destination}/{version}/cachedContents/{id}",
            (*query, "updateMask"),
        )
    # Gemini's OpenAI-compatible API has separately enumerated inference routes.
    if method == "POST" and rest in {"/openai/chat/completions", "/openai/embeddings"}:
        return Profile(
            f"gemini.{version}.openai.{rest.rsplit('/', 1)[1]}.1",
            "openai",
            method,
            "/{destination}/{version}/openai/{operation}",
            query,
        )
    raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")


def _query(upstream: Upstream, profile: Profile, name: str, value: str) -> str:
    if name not in profile.query:
        # Its name alone reaches the agent, as a header's does (D4; P2-8a, root's ruling 5): never its value.
        raise EnvelopeRefused("envelope-query-unknown", query=name.encode("utf-8", "replace"))
    if name in {"limit", "pageSize", "maxResults"}:
        if not re.fullmatch(r"[1-9][0-9]{0,3}", value) or int(value) > 1000:
            raise EnvelopeRefused("envelope-query-value")
    elif name == "client_version":
        if not _VERSION.fullmatch(value):
            raise EnvelopeRefused("envelope-query-value")
    elif name == "beta":
        if value != "true":
            raise EnvelopeRefused("envelope-query-value")
    elif name == "alt":
        if value not in {"json", "sse"}:
            raise EnvelopeRefused("envelope-query-value")
    elif name == "type":
        if value not in {"SYSTEM_DEFINED", "APPLICATION"}:
            raise EnvelopeRefused("envelope-query-value")
    elif name in _MODEL_CURSORS:
        if not path_model("anthropic", value):  # its name alone reaches the agent (D4), never its value
            raise EnvelopeRefused("envelope-query-value", query=name.encode("ascii"))
    elif name in _OPAQUE_CURSORS:
        if not _OPAQUE_CURSORS[name].fullmatch(value):
            raise EnvelopeRefused("envelope-query-value", query=name.encode("ascii"))
        return "checked"
    elif name == "key":
        if not value or len(value) > 8192:
            raise EnvelopeRefused("envelope-credential")
        return "authorized"
    else:
        if not value or len(value) > 2048:
            raise EnvelopeRefused("envelope-query-value")
        _expected(upstream, name, value)
    return "inspected"


def _value_refused(name: str) -> EnvelopeRefused:
    # The header's name alone reaches the agent (D4); its value never leaves the error, logs or history.
    return EnvelopeRefused("envelope-header-value", header=name.encode("latin-1"))


def _tokens(value: str, token: re.Pattern[str], most: int, *, spaces: bool) -> bool:
    """A comma list of 1..``most`` tokens, each matching ``token``; ``spaces``: optional whitespace around commas."""
    parts = value.split(",")
    if len(parts) > most:
        return False
    return all(token.fullmatch(part.strip(" \t") if spaces else part) for part in parts)


def _accept_encoding(value: str) -> bool:
    """Known content codings in any order, each at most once, with an optional q-value (RFC 9110 12.5.3)."""
    seen: set[str] = set()
    for part in value.lower().split(","):
        coding, separator, weight = part.partition(";")
        coding = coding.strip(" \t")
        if coding not in _CODINGS or coding in seen:
            return False
        if separator and not _WEIGHT.fullmatch(weight.strip(" \t")):
            return False
        seen.add(coding)
    return True


def _header(upstream: Upstream, profile: Profile, name: str, value: str) -> str:
    if name in REQUEST_DROP or name.startswith(DROP_PREFIXES):
        return "removed"
    credentials = (
        profile.credential_headers
        if profile.credential_headers is not None
        else {
            "anthropic": {"authorization", "x-api-key"},
            "openai": {"authorization"},
            "chatgpt": {"authorization", "x-oai-attestation"},
            "gemini": {"authorization", "x-goog-api-key"},
            "bedrock": {"authorization", "x-amz-security-token"},
        }[profile.family]
    )
    # A credential is decided before any removable family, so no declared family can ever strip one.
    if name in credentials:
        if not value or len(value) > 8192:
            raise EnvelopeRefused("envelope-credential")
        return "authorized"
    if name.startswith(profile.removable) or name in profile.removable_exact:
        return "removed"
    # Latet's own client on its fixture route (the tutorial): its id by its source's grammar, on that route alone.
    if (fixture := upstream.fixture_ids.get(name)) is not None:
        if not fixture.fullmatch(value):
            raise _value_refused(name)
        return "inspected"
    if name in _RESOURCE_HEADERS:
        _expected(upstream, name, value)
        return "authorized"
    if name in ID_HEADERS:
        if not ID_HEADERS[name].fullmatch(value):
            raise _value_refused(name)
        return "inspected"
    if name in _FREE_HEADERS:
        if len(value) > (1024 if name == "user-agent" else 256):
            raise _value_refused(name)
        return "checked"
    if name == "accept-encoding":
        if not _accept_encoding(value):
            raise _value_refused(name)
        return "inspected"
    family = profile.family
    enums = {
        "content-type": {"application/json", "application/json; charset=utf-8", "application/json;charset=utf-8"},
        "content-encoding": {"identity", "gzip", "deflate", "br", "zstd"},
        "accept": {"*/*", "application/json", "text/event-stream", "application/json, text/event-stream"},
        "sec-fetch-mode": {"cors"},
    }
    # Anthropic's protocol headers, on its own family (Vertex's Claude routes included) and on Bedrock's model calls,
    # where Claude Code sends the same SDK headers; never on Bedrock's control-plane reads.
    if family == "anthropic" or (family == "bedrock" and not profile.bodyless):
        enums |= {
            "anthropic-version": {"2023-06-01"},
            "anthropic-dangerous-direct-browser-access": {"true"},
            # Claude Code 2.1.316: "x-app": background session ? "cli-bg" : "cli".
            "x-app": {"cli", "cli-bg"},
            # Claude Code 2.1.316, when CLAUDE_CODE_ADDITIONAL_PROTECTION is set.
            "x-anthropic-additional-protection": {"true"},
        }
        if name == "anthropic-beta":
            if not _tokens(value, _BETA, _BETAS, spaces=True):
                raise _value_refused(name)
            return "inspected"
        if name in {"x-claude-code-request-class", "x-claude-code-agent-type"}:
            grammar = _REQUEST_CLASS if name == "x-claude-code-request-class" else _AGENT_TYPE
            if not grammar.fullmatch(value):
                raise _value_refused(name)
            return "inspected"
    # Copilot CLI's BYOK providers (openai, azure, anthropic): VS Code Copilot chatMLFetcher.ts:1527 sets X-Initiator
    # to user or agent; networking.ts:498-531 X-Interaction-Type.
    if family in {"openai", "anthropic"}:
        enums |= {"x-initiator": {"user", "agent"}}
        if name == "x-interaction-type":
            if not _INTERACTION.fullmatch(value):
                raise _value_refused(name)
            return "inspected"
    if family in {"chatgpt", "openai"}:
        enums |= {"openai-beta": {"responses=v1", "responses_websockets=2025-02-19", "responses_websockets=2026-02-06"}}
        # Codex's built-in provider sends version: CARGO_PKG_VERSION (model-provider-info/src/lib.rs:526-543).
        if name == "version":
            if not _VERSION.fullmatch(value):
                raise _value_refused(name)
            return "inspected"
        if name == "originator":
            if not _ORIGINATOR.fullmatch(value):
                raise _value_refused(name)
            return "inspected" if value in _FIRST_PARTY_ORIGINATORS else "checked"
        if name == "x-codex-beta-features":
            if not _tokens(value, _FEATURE, _FEATURES, spaces=False):
                raise _value_refused(name)
            return "inspected"
        if name == "x-codex-turn-state":
            if not _TURN_STATE.fullmatch(value):
                raise _value_refused(name)
            return "inspected"
        if name == "x-codex-routing-hint":
            if len(value) > 160 or not _ROUTING_HINT.fullmatch(value):
                raise _value_refused(name)
            return "inspected"
        # Codex c1382380 client.rs:167-177, 797-800, 1331-1337, 2355-2362: flags it sets to "true" alone.
        enums |= {
            "x-openai-memgen-request": {"true"},
            "x-responsesapi-include-timing-metrics": {"true"},
            "x-openai-internal-codex-responses-lite": {"true"},
        }
    if family == "chatgpt":
        enums |= {"x-openai-fedramp": {"true"}}
    if family == "bedrock" and name in {"x-amz-date", "x-amz-content-sha256"}:
        grammar = r"[0-9]{8}T[0-9]{6}Z" if name == "x-amz-date" else r"[a-f0-9]{64}"
        if not re.fullmatch(grammar, value):
            raise _value_refused(name)
        return "inspected"
    if name in enums:
        if value.lower() not in enums[name]:
            raise _value_refused(name)
        return "inspected"
    raise EnvelopeRefused("envelope-header-unknown", header=name.encode("latin-1"))


def admit(raw: Inventory, upstream: Upstream) -> Admission:
    envelope = raw.envelope
    websocket = envelope.transport == "ws-handshake"
    if envelope.transport not in {"http", "ws-handshake"} or envelope.destination != upstream.name:
        raise EnvelopeRefused("envelope-binding")
    path = envelope.raw_path
    if any(c > 127 for c in path + envelope.query):
        raise EnvelopeRefused("envelope-encoding")
    decoded = _decoded(path)
    if not path.startswith(upstream.prefix.encode("ascii") + b"/"):
        raise EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}")
    if any(part in {".", ".."} for part in decoded.split("/")) or b"\\" in path or b"//" in path:
        raise EnvelopeRefused("envelope-path")
    # A cloud's exact authority is selected only from this revision's finite location table, before resolve.
    authority = upstream.base_url
    if isinstance(upstream, BedrockUpstream):
        region = path[len(upstream.prefix) + 1 :].split(b"/", 1)[0].decode("ascii")
        if region not in BEDROCK_REGIONS:
            raise EnvelopeRefused("envelope-region")
        authority = (upstream.control if envelope.method == "GET" else upstream.runtime).format(region=region)
    elif isinstance(upstream, VertexUpstream):
        location = re.search(rb"/locations/([^/]+)", path)
        name = location[1].decode("ascii") if location else "global"
        if name not in VERTEX_LOCATIONS:
            raise EnvelopeRefused("envelope-location")
        authority = upstream.base or f"https://{vertex_host(name)}"
    if upstream.envelope_resource_sources:
        expected = frozenset(
            (role, value) for url, role, value in upstream.envelope_resource_sources if url == authority
        )
        # Every row is tied to this destination identity and exact configured/selected authority.
        upstream = replace(upstream, envelope_expected=expected)
    # Reserved escapes remain wire bytes for exact profile matching (Bedrock ARN and Gemini colon).
    try:
        profile = _profile(upstream, "POST" if websocket else envelope.method, readable_path(path.decode("ascii")))
        if websocket:
            if (
                envelope.method != "GET"
                or type(upstream) is not Upstream
                or profile.family not in {"openai", "chatgpt"}
                or "responses" not in profile.id
                or upstream.request_profile != "standard"
            ):
                raise EnvelopeRefused("envelope-route")
            profile = replace(profile, id=profile.id + "." + WS_REVISION, method="GET", bodyless=True)
    except UnicodeError:
        raise EnvelopeRefused("envelope-path") from None
    if profile.bodyless and envelope.body:
        raise EnvelopeRefused("envelope-bodyless")
    treatments: list[str] = []
    checked: list[str] = []
    headers: list[tuple[str, str]] = []
    seen_query: set[str] = set()
    seen_headers: set[str] = set()
    query_treatments: dict[int, str] = {}
    header_treatments: dict[int, str] = {}
    removed = 0
    granted: list[tuple[tuple[Any, ...], EnvelopeRefused]] = []
    unread: set[str] = set()
    query_names = {
        part.index: _decoded(envelope.query[part.start : part.end], plus=True)
        for part in raw.entries
        if part.region == "query-name"
    }
    for part in raw.entries:
        if part.region == "query-value":
            name = query_names[part.index]
            if name in seen_query:
                raise EnvelopeRefused("envelope-query-duplicate")
            seen_query.add(name)
            value = _decoded(envelope.query[part.start : part.end], plus=True)
            try:
                query_treatments[part.index] = _query(upstream, profile, name, value)
            except EnvelopeRefused as error:
                excepted = (
                    _excepted(upstream, profile, "query", name) if error.code == "envelope-query-unknown" else None
                )
                if excepted is None or not value or len(value) > 2048:
                    raise
                query_treatments[part.index] = excepted[1]
                granted.append((excepted[0], error))
            if query_treatments[part.index] == "checked":  # an exception's, or an opaque cursor's (G2 review M-1)
                checked.append(value)
            if not safe_text(name) or not safe_text(value):
                raise EnvelopeRefused("envelope-sensitive")
        elif part.region == "header-value":
            key, value = envelope.headers[part.index]
            if not _TOKEN.fullmatch(key) or any((c < 32 and c != 9) or c == 127 for c in value):
                raise EnvelopeRefused("envelope-header-syntax")
            name, text = key.decode("ascii").lower(), value.decode("latin-1")
            if name in seen_headers and (
                websocket or (name not in REQUEST_DROP and not name.startswith(DROP_PREFIXES))
            ):
                raise EnvelopeRefused("envelope-header-duplicate")
            # Framing duplicates are ambiguous even though each occurrence would otherwise be removed.
            if name in seen_headers and name in {"host", "content-length", "transfer-encoding"}:
                raise EnvelopeRefused("envelope-header-duplicate")
            seen_headers.add(name)
            try:
                treatment = header_treatments[part.index] = (
                    "removed" if websocket and name in WS_HANDSHAKE_DROP else _header(upstream, profile, name, text)
                )
            except EnvelopeRefused as error:
                excepted = (
                    _excepted(upstream, profile, "header", name) if error.code == "envelope-header-unknown" else None
                )
                if excepted is None or not text or len(text) > 8192:
                    raise
                treatment = header_treatments[part.index] = excepted[1]
                granted.append((excepted[0], error))
            if not safe_text(name) or not safe_text(text):
                raise EnvelopeRefused("envelope-sensitive")
            if treatment == "removed":
                if name not in REQUEST_DROP and not name.startswith(DROP_PREFIXES):
                    removed += 1
            else:
                headers.append((key.decode("ascii"), text))
                if treatment == "checked":
                    checked.append(text)
                elif treatment == "authorized":
                    unread.add(name)
    if not set(profile.required_query) <= seen_query:
        raise EnvelopeRefused("envelope-query-value")
    for part in raw.entries:
        if part.region.startswith("query-"):
            treatments.append("inspected" if part.region == "query-name" else query_treatments[part.index])
        elif part.region.startswith("header-"):
            treatments.append(header_treatments[part.index])
        elif part.region == "path":
            value = _decoded(path[part.start : part.end])
            if not safe_text(value):
                raise EnvelopeRefused("envelope-sensitive")
            if part.end <= len(upstream.prefix) and not upstream.keep_prefix:
                treatments.append("removed")
            else:
                treatments.append(
                    "authorized"
                    if any(value == expected for _, expected in upstream.envelope_expected)
                    else "inspected"
                )
        else:
            treatments.append("inspected")
    return Admission(
        raw, profile, tuple(headers), tuple(checked), tuple(treatments), removed, tuple(granted), frozenset(unread)
    )
