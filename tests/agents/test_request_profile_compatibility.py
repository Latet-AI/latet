"""P2-8's routed-agent qualification ledger (`notes/server-request-coverage-plan.md`, "Complete routed-agent
qualification ledger to instantiate"): one row per routed configuration, each mapped to a row of the plan, with the
client and the version it was qualified on, where that version or the request shapes come from, the profiles its calls
are admitted by, its transports, its dummy credential's shape, its field coverage, what is masked and restored, its
status and the exact evidence.

- ``observed``: the agent ran against Latet's fake providers and its calls are in
  `tests/data/request-coverage/observed-calls.json` (P1-0), at the version named there;
- ``fixture``: no agent ran; the gateway's fixtures send the call's shape (a client in the test, an SDK of the head's
  lock file is ``observed``);
- ``untested``: neither. A skipped optional executable, a desktop app or an editor extension is untested, never green
  because a CLI shares its settings;
- ``outside``: a surface Latet has no route for, said in the agent's page.

The negative fixtures (`tests/request_qualification.py`'s ``NEGATIVE``: each family's main call, its ancillary calls
and one unsupported sibling) run here through a computer's actual ingress, and through a server's in
`tests/server/test_request_profile_qualification.py`. No agent and no live provider is called: an agent's run is the
optional `LATET_AGENT_TESTS` suites' (the evidence nodes below), and the recording transport answers here."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from latet.gateway.profile_ids import PROFILE_IDS
from latet.launch_carriers import CARRIERS

from ..request_coverage import computer_cell, run_case
from ..request_qualification import DRIFT, NEGATIVE, apply_resources, problems
from ..request_recorder import RecordingWSUpstream

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "notes" / "server-request-coverage-plan.md"
REPORT = ROOT / "notes" / "server-request-coverage-report.md"
OBSERVED = ROOT / "tests" / "data" / "request-coverage" / "observed-calls.json"
STATUSES = ("observed", "fixture", "untested", "outside")

# The plan's ledger rows, by their first column.
P_CLAUDE = "Claude Code CLI, VS Code/JetBrains extension, gateway-configured Desktop"
P_CODEX = "Codex CLI/Desktop"
P_OPENCODE = "OpenCode CLI/Desktop"
P_GEMINI = "Gemini CLI, GenAI Python/JS SDK"
P_HERMES = "Hermes"
P_OPENCLAW = "OpenClaw, Goose, OpenHands"
P_COPILOT = "Copilot CLI and VS Code custom endpoints"
P_JUNIE = "Junie, JetBrains AI Assistant, Zed"
P_CLINE = "Cline, Kilo Code, Continue"
P_QWEN = "Qwen Code"
P_CRUSH = "Crush, Prime Agent"
P_ANDROID = "Android Studio"
P_SDKS = "Official OpenAI/Anthropic SDKs/scripts, LangChain/LangGraph, AutoGen/CrewAI, Aider/LiteLLM"
P_COMPANY = "Company gateways, Azure OpenAI/Foundry, Bedrock/Vertex"
P_TUTORIAL = "Latet's tutorial"

ANTHROPIC = ("anthropic.messages.1", "anthropic.count_tokens.1", "anthropic.models.1")
CHAT = ("openai.chat.1", "openai.models.1")
RESPONSES = ("openai.responses.1", "openai.models.1")
CHATGPT = ("chatgpt.codex.responses.1", "chatgpt.codex.models.1")
GEMINI = (
    "gemini.v1beta.generateContent.1",
    "gemini.v1beta.streamGenerateContent.1",
    "gemini.v1beta.countTokens.1",
    "gemini.v1beta.embedContent.1",
    "gemini.v1beta.cache-create.1",
    "gemini.v1beta.models.1",
)
VERTEX_GEMINI = ("gemini.v1beta1.generateContent.1", "gemini.v1beta1.streamGenerateContent.1")
BEDROCK = (
    "bedrock.invoke.1",
    "bedrock.invoke-with-response-stream.1",
    "bedrock.count-tokens.1",
    "bedrock.inference-profiles.1",
)
VERTEX = ("vertex.anthropic.rawPredict.1", "vertex.anthropic.streamRawPredict.1")

MASKED = "masked, restored in the answer"
CHECKED = "checked only: sent byte for byte, refused when a sensitive value would leave (D6 for a new text member)"
EVERY = "every member by its profile; a member no profile names refused, naming its place"
NOT_RUN = "not run: no agent, desktop app or extension of this surface ran against Latet"
AGENTS = "tests/agents/test_agents_by_setting.py"
SETTING = "skipped in P1-0's run (not installed), observed-calls.json"


@dataclass(frozen=True)
class Row:
    plan: str  # the plan's ledger row
    surface: str  # the routed configuration
    clients: tuple[str, ...]  # as observed-calls.json names them (its agents' keys, or its skipped agents)
    version: str  # the version qualified (observed, or pinned from source), or why none
    pin: str  # where that version or the request shapes come from
    profiles: tuple[str, ...]  # the request profiles its calls are admitted by
    transports: tuple[str, ...]
    credential: str  # the dummy credential's shape in the fixtures
    coverage: str
    masks: str
    status: str
    evidence: tuple[str, ...]  # test nodes, path::function
    families: tuple[str, ...]  # its calls' NEGATIVE and DRIFT families
    result: str  # the exact result: what ran, or why nothing did
    carrier: str | None = None  # latet.launch_carriers' agent, for a surface a Latet launcher starts


LEDGER: tuple[Row, ...] = (
    Row(
        P_CLAUDE,
        "Claude Code CLI through a company gateway destination",
        ("claude-code",),
        "2.1.312",
        "P1-0's observed calls (observed-calls.json)",
        ANTHROPIC,
        ("http",),
        "Bearer sk-ant-oat01-… or x-api-key sk-ant-api03-…",
        EVERY,
        CHECKED,
        "observed",
        (
            "tests/agents/test_real_agents.py::test_claude_code_through_a_company_gateway",
            "tests/unit/test_request_profiles_observed.py::test_every_observed_agent_call_is_forwarded_or_altered_only_by_removable_families",
        ),
        ("claude-code",),
        "passed in P1-0's agent run (observed-calls.json runs.agents); its calls admitted by the head's profiles",
        "claude-code",
    ),
    Row(
        P_CLAUDE,
        "Claude Code CLI with an Anthropic API key or a Claude subscription",
        ("claude-code",),
        "2.1.312",
        "P1-0's observed headers; the gateway's own client in the fixtures",
        ANTHROPIC,
        ("http",),
        "Bearer sk-ant-oat01-… or x-api-key sk-ant-api03-…",
        EVERY,
        CHECKED,
        "fixture",
        ("tests/e2e/test_gateway_e2e.py::test_claude_code_is_verified_not_rewritten",),
        ("claude-code",),
        "fixtures: NEGATIVE and DRIFT claude-code on a computer and a server",
        "claude-code",
    ),
    Row(
        P_CLAUDE,
        "Claude Code CLI on Bedrock (its API key, signed by Latet, a model ARN)",
        ("claude-code",),
        "2.1.312",
        "P1-0's observed calls (observed-calls.json)",
        BEDROCK,
        ("http",),
        "Bearer ABSK…, or SigV4 by Latet",
        EVERY,
        CHECKED,
        "observed",
        (
            "tests/agents/test_real_agents.py::test_claude_code_on_bedrock_with_its_api_key",
            "tests/agents/test_real_agents.py::test_claude_code_on_bedrock_signed_by_latet",
            "tests/agents/test_real_agents.py::test_a_model_arn_reaches_bedrock_as_sent",
        ),
        ("bedrock",),
        "passed in P1-0's agent run",
        "claude-code",
    ),
    Row(
        P_CLAUDE,
        "Claude Code CLI on Vertex AI",
        ("claude-code",),
        "2.1.312",
        "P1-0's observed calls (observed-calls.json)",
        VERTEX,
        ("http",),
        "Bearer ya29…, the project listed in request_resources",
        EVERY,
        CHECKED,
        "observed",
        ("tests/agents/test_real_agents.py::test_claude_code_on_vertex",),
        ("vertex",),
        "passed in P1-0's agent run",
        "claude-code",
    ),
    Row(
        P_CLAUDE,
        "Claude Code in the VS Code or JetBrains extension",
        ("claude-code",),
        "none",
        "the CLI's settings, shared",
        ANTHROPIC,
        ("http",),
        "as the CLI",
        EVERY,
        CHECKED,
        "untested",
        (),
        ("claude-code",),
        NOT_RUN,
    ),
    Row(
        P_CLAUDE,
        "Claude Desktop with a gateway configured",
        ("claude-code",),
        "none",
        "the CLI's settings, shared",
        ANTHROPIC,
        ("http",),
        "as the CLI",
        EVERY,
        CHECKED,
        "untested",
        (),
        ("claude-code",),
        NOT_RUN,
    ),
    Row(
        P_CLAUDE,
        "Claude Desktop's Code tab with the plugin alone, and signed-in Chat and Cowork",
        ("claude-code",),
        "none",
        "docs/agents/claude-desktop",
        (),
        (),
        "none",
        "none: no route through Latet",
        "none",
        "outside",
        (),
        (),
        "no gateway route: the plugin's checks alone",
    ),
    Row(
        P_CODEX,
        "Codex CLI on a provider of model_providers (Responses over HTTP)",
        ("codex",),
        "0.162.0",
        "P1-0's observed calls; Codex c1382380 (0.162.0)",
        RESPONSES,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "observed",
        ("tests/agents/test_real_agents.py::test_codex_through_a_company_provider",),
        ("openai-responses",),
        "passed in P1-0's agent run",
        "codex",
    ),
    Row(
        P_CODEX,
        "Codex CLI signed in with ChatGPT: Responses over HTTP and SSE, and the model list",
        ("codex",),
        "0.162.0",
        "Codex c1382380 (0.162.0) source; a ChatGPT fixture, no account",
        CHATGPT,
        ("http",),
        "Bearer eyJ…, chatgpt-account-id",
        EVERY,
        MASKED,
        "fixture",
        (
            "tests/unit/test_request_profiles_observed.py::test_codex_model_list_names_client_version_on_command_auth_provider_routes",
        ),
        ("codex-http",),
        "fixtures: NEGATIVE and DRIFT codex-http on a computer and a server",
        "codex",
    ),
    Row(
        P_CODEX,
        "Codex CLI signed in with ChatGPT over a WebSocket (create, cancel)",
        ("codex",),
        "0.162.0",
        "Codex c1382380 (0.162.0) source; the gateway's own WebSocket client",
        ("chatgpt.codex.responses.1",),
        ("ws",),
        "Bearer eyJ…, session-id",
        EVERY + "; a frame takes no exception (F-2)",
        MASKED,
        "fixture",
        ("tests/e2e/test_gateway_e2e.py::test_codex_websocket",),
        ("codex-ws",),
        "fixtures: NEGATIVE and DRIFT codex-ws on a computer and a server",
        "codex",
    ),
    Row(
        P_CODEX,
        "Codex CLI on its built-in OpenAI provider with an API key",
        ("codex",),
        "none",
        "observed-calls.json agents.codex.untested",
        RESPONSES,
        ("http", "ws"),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-responses",),
        "its P1-0 probe hung and was stopped; no launch key (builtin-provider)",
        "codex",
    ),
    Row(
        P_CODEX,
        "Codex Desktop",
        ("codex",),
        "none",
        "the CLI's settings, shared",
        RESPONSES + CHATGPT,
        ("http", "ws"),
        "as the CLI",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-responses", "codex-http", "codex-ws"),
        NOT_RUN,
    ),
    Row(
        P_OPENCODE,
        "OpenCode CLI on Anthropic, OpenAI or OpenRouter with an API key",
        ("OpenCode",),
        "1.19.5 (carrier pinned from source)",
        "OpenCode 810c0eba (1.19.5); latet.launch_carriers",
        ANTHROPIC + CHAT,
        ("http",),
        "x-api-key sk-ant-api03-…, Bearer sk-…, Bearer sk-or-v1-…",
        EVERY,
        MASKED,
        "untested",
        (
            "tests/unit/test_launcher_carrier_pins.py::test_opencode_from_its_verified_carrier_passes_its_key",
            "tests/unit/test_launcher_carriers.py::test_remaining_carriers_cli_ack_before_publication",
        ),
        ("anthropic-key", "openai-chat", "openrouter"),
        SETTING,
        "opencode",
    ),
    Row(
        P_OPENCODE,
        "OpenCode Desktop",
        ("OpenCode",),
        "none",
        "the CLI's global settings, shared",
        ANTHROPIC + CHAT,
        ("http",),
        "as the CLI",
        EVERY,
        MASKED,
        "untested",
        (),
        ("anthropic-key", "openai-chat", "openrouter"),
        NOT_RUN,
    ),
    Row(
        P_OPENCODE,
        "OpenCode's ChatGPT plugin and its other providers",
        ("OpenCode",),
        "none",
        "docs/agents/opencode",
        (),
        (),
        "none",
        "none: their addresses are not Latet's",
        "none",
        "outside",
        (),
        (),
        "no route through Latet",
    ),
    Row(
        P_GEMINI,
        "Gemini CLI with an API key or Vertex AI",
        ("Gemini CLI",),
        "0.19.0-0.66.0 (carrier pinned from source)",
        "Gemini CLI 97041320 (0.66.0); latet.launch_carriers",
        GEMINI + VERTEX_GEMINI,
        ("http",),
        "x-goog-api-key AIza…",
        EVERY,
        MASKED,
        "untested",
        (
            "tests/agents/test_gemini_cli.py::test_gemini_cli_gets_its_answer_back_and_the_provider_reads_no_secret",
            "tests/unit/test_gemini_gateway.py::test_the_key_reaches_google_as_it_was_sent",
            "tests/unit/test_launcher_carrier_pins.py::test_the_key_entry_parses_the_same_under_both_header_splits",
        ),
        ("gemini",),
        SETTING,
        "gemini-cli",
    ),
    Row(
        P_GEMINI,
        "Gemini CLI in ACP mode, started by an editor",
        ("Gemini CLI",),
        "none",
        "Gemini CLI 97041320 packages/cli/src/acp/acpRpcDispatcher.ts:128-157",
        GEMINI,
        ("http",),
        "as the editor gives it",
        EVERY,
        MASKED,
        "untested",
        ("tests/unit/test_launcher_carrier_pins.py::test_latet_gemini_in_acp_mode_passes_no_key_and_says_why",),
        ("gemini",),
        "not run; latet gemini gives it no launch key (gemini-acp)",
        "gemini-cli",
    ),
    Row(
        P_GEMINI,
        "GenAI Python SDK on the Gemini API (generate, stream, count, embed, cache)",
        ("sdk:google-genai",),
        "uv.lock of the head",
        "observed-calls.json agents (sdk:google-genai)",
        GEMINI,
        ("http",),
        "x-goog-api-key AIza…",
        EVERY,
        MASKED,
        "observed",
        (
            "tests/e2e/test_sdks_e2e.py::test_the_genai_sdk_gets_its_answer_back_and_the_provider_reads_no_secret",
            "tests/unit/test_request_profile_findings.py::test_gemini_cache_creation_takes_the_genai_sdks_model_resource",
        ),
        ("gemini",),
        "passed in P1-0's e2e run (observed-calls.json runs.e2e)",
    ),
    Row(
        P_GEMINI,
        "GenAI Python SDK on Vertex AI (an API key, a project)",
        ("sdk:google-genai",),
        "uv.lock of the head",
        "observed-calls.json agents (sdk:google-genai)",
        VERTEX_GEMINI,
        ("http",),
        "x-goog-api-key AQ.…, or Bearer ya29…",
        EVERY,
        MASKED,
        "observed",
        (
            "tests/e2e/test_sdks_e2e.py::test_the_genai_sdk_on_vertex_with_an_api_key_gets_its_answer_back",
            "tests/e2e/test_sdks_e2e.py::test_the_genai_sdk_on_vertex_with_a_project_gets_its_answer_back",
        ),
        ("gemini",),
        "passed in P1-0's e2e run",
    ),
    Row(
        P_GEMINI,
        "GenAI JS SDK",
        ("sdk:google-genai-js",),
        "none",
        "none",
        GEMINI,
        ("http",),
        "x-goog-api-key AIza…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("gemini",),
        NOT_RUN,
    ),
    Row(
        P_GEMINI,
        "Gemini signed in with Google, the Live API, general batch and uploads",
        ("Gemini CLI",),
        "none",
        "docs/agents/gemini",
        (),
        ("http", "ws"),
        "none",
        "refused: latet gemini does not start signed in; the Live API's socket is refused",
        "none",
        "outside",
        ("tests/unit/test_gemini_gateway.py::test_the_live_apis_websocket_is_refused_and_nothing_reaches_google",),
        (),
        "refused, nothing sent",
    ),
    Row(
        P_HERMES,
        "Hermes Agent on Chat Completions and ChatGPT Responses",
        ("Hermes Agent",),
        "none",
        "its documented settings",
        CHAT + CHATGPT,
        ("http",),
        "Bearer sk-…, Bearer eyJ…",
        EVERY,
        MASKED,
        "untested",
        (
            f"{AGENTS}::test_hermes_agent",
            "tests/unit/test_untyped_stream.py::test_a_stream_with_no_content_type_is_restored",
        ),
        ("openai-chat", "codex-http"),
        SETTING,
    ),
    Row(
        P_OPENCLAW,
        "OpenClaw, Goose and OpenHands on Chat Completions",
        ("OpenClaw", "Goose", "OpenHands"),
        "none",
        "their documented settings",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (f"{AGENTS}::test_openclaw", f"{AGENTS}::test_goose", f"{AGENTS}::test_openhands"),
        ("openai-chat",),
        SETTING,
    ),
    Row(
        P_OPENCLAW,
        "Goose on OpenRouter",
        ("Goose",),
        "none",
        "its documented settings",
        CHAT,
        ("http",),
        "Bearer sk-or-v1-…",
        EVERY + "; OpenRouter's Anthropic, OpenAI and Google model ids by grammar",
        MASKED,
        "untested",
        (),
        ("openrouter",),
        NOT_RUN,
    ),
    Row(
        P_COPILOT,
        "Copilot CLI with a provider of one's own over Chat Completions",
        ("copilot-cli",),
        "1.0.103",
        "P1-0's observed calls (D-6)",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "observed",
        (
            f"{AGENTS}::test_copilot_cli",
            "tests/unit/test_copilot_wrapper.py::test_a_provider_of_your_own_goes_through_latet",
        ),
        ("openai-chat",),
        "passed in P1-0's agent run; its launch key carried (COPILOT_PROVIDER_HEADERS)",
        "copilot",
    ),
    Row(
        P_COPILOT,
        "Copilot CLI with a provider of one's own over Responses or Anthropic's API",
        ("copilot-cli",),
        "1.0.103",
        "P1-0's observed calls (D-6)",
        RESPONSES + ANTHROPIC,
        ("http",),
        "Bearer sk-…, x-api-key sk-ant-api03-…",
        EVERY,
        MASKED,
        "observed",
        (f"{AGENTS}::test_copilot_cli",),
        ("openai-responses", "anthropic-key"),
        "passed in P1-0's agent run; no launch key there (carrier-unverified)",
        "copilot",
    ),
    Row(
        P_COPILOT,
        "Copilot CLI over Responses on a WebSocket (COPILOT_PROVIDER_TRANSPORT=websockets)",
        ("copilot-cli",),
        "none",
        "P1-0 D-7",
        ("openai.responses.1",),
        ("ws",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("codex-ws",),
        NOT_RUN,
        "copilot",
    ),
    Row(
        P_COPILOT,
        "VS Code custom endpoints, JetBrains and Visual Studio",
        ("copilot-cli",),
        "none",
        "none",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-chat",),
        NOT_RUN,
    ),
    Row(
        P_COPILOT,
        "Copilot signed in with GitHub",
        ("copilot-cli",),
        "none",
        "docs/agents/copilot",
        (),
        (),
        "none",
        "refused: latet copilot does not start signed in with GitHub",
        "none",
        "outside",
        ("tests/unit/test_copilot_wrapper.py::test_copilot_cli_signed_in_with_github_does_not_start",),
        (),
        "refused before the agent starts",
    ),
    Row(
        P_JUNIE,
        "Junie CLI on its three APIs",
        ("Junie CLI",),
        "none",
        "its documented settings",
        CHAT + RESPONSES + ANTHROPIC,
        ("http",),
        "Bearer sk-…, x-api-key sk-ant-api03-…",
        EVERY,
        MASKED,
        "untested",
        (f"{AGENTS}::test_junie_cli",),
        ("openai-chat", "openai-responses", "anthropic-key"),
        SETTING,
    ),
    Row(
        P_JUNIE,
        "JetBrains AI Assistant and Zed",
        ("JetBrains AI Assistant", "Zed"),
        "none",
        "none",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-chat",),
        NOT_RUN,
    ),
    Row(
        P_CLINE,
        "Cline, Kilo Code and Continue CLIs",
        ("Cline CLI", "Kilo Code CLI", "Continue CLI"),
        "none",
        "their documented settings",
        CHAT + ANTHROPIC,
        ("http",),
        "Bearer sk-…, x-api-key sk-ant-api03-…",
        EVERY,
        MASKED,
        "untested",
        (f"{AGENTS}::test_cline_cli", f"{AGENTS}::test_kilo_cli", f"{AGENTS}::test_continue_cli"),
        ("openai-chat", "anthropic-key"),
        SETTING,
    ),
    Row(
        P_CLINE,
        "Cline, Kilo Code and Continue editor extensions",
        ("Cline", "Kilo Code", "Continue"),
        "none",
        "none",
        CHAT + ANTHROPIC,
        ("http",),
        "as the CLIs",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-chat", "anthropic-key"),
        NOT_RUN,
    ),
    Row(
        P_QWEN,
        "Qwen Code on OpenAI's and Anthropic's APIs",
        ("qwen-code",),
        "0.22.0",
        "P1-0's observed calls (observed-calls.json)",
        CHAT + ANTHROPIC,
        ("http",),
        "Bearer sk-…, x-api-key sk-ant-api03-…",
        EVERY + "; its User-Agent grants no verify-only authority",
        MASKED,
        "observed",
        (f"{AGENTS}::test_qwen_code",),
        ("openai-chat", "anthropic-key"),
        "passed in P1-0's agent run",
    ),
    Row(
        P_CRUSH,
        "Crush and Prime Agent",
        ("Crush", "Prime Agent"),
        "none",
        "their documented settings",
        CHAT + ANTHROPIC,
        ("http",),
        "Bearer sk-…, Bearer sk-ant-oat01-…",
        EVERY,
        MASKED,
        "untested",
        (f"{AGENTS}::test_crush", f"{AGENTS}::test_prime_agent", f"{AGENTS}::test_prime_agent_claude_plan"),
        ("openai-chat", "anthropic-key", "claude-code"),
        SETTING,
    ),
    Row(
        P_ANDROID,
        "Android Studio's third-party model route",
        ("Android Studio",),
        "none",
        "its documented settings",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (),
        ("openai-chat",),
        NOT_RUN,
    ),
    Row(
        P_SDKS,
        "OpenAI and Anthropic Python SDKs, LangChain and LangGraph",
        ("sdk:openai", "sdk:anthropic"),
        "uv.lock of the head",
        "observed-calls.json agents (sdk:openai, sdk:anthropic)",
        CHAT + RESPONSES + ANTHROPIC,
        ("http",),
        "Bearer sk-…, x-api-key sk-ant-api03-…",
        EVERY,
        MASKED,
        "observed",
        (
            "tests/e2e/test_sdks_e2e.py::test_the_openai_sdk_chat_gets_its_answer_back_and_the_provider_reads_no_secret",
            "tests/e2e/test_sdks_e2e.py::test_the_anthropic_sdk_gets_its_answer_back_and_the_provider_reads_no_secret",
            "tests/e2e/test_sdks_e2e.py::test_langchain_on_the_openai_sdk_gets_its_answer_back_and_the_provider_reads_no_secret",
        ),
        ("openai-chat", "openai-responses", "anthropic-key"),
        "passed in P1-0's e2e run",
    ),
    Row(
        P_SDKS,
        "AutoGen and CrewAI",
        ("AutoGen, CrewAI",),
        "none",
        "their documented settings",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        (
            "tests/agents/test_frameworks.py::test_autogen_gets_its_answer_back_and_the_provider_reads_no_secret",
            "tests/agents/test_frameworks.py::test_crewai_gets_its_answer_back_and_the_provider_reads_no_secret",
        ),
        ("openai-chat",),
        SETTING,
    ),
    Row(
        P_SDKS,
        "Aider and LiteLLM",
        ("Aider",),
        "none",
        "its documented settings",
        CHAT + ANTHROPIC,
        ("http",),
        "Bearer sk-…",
        EVERY,
        MASKED,
        "untested",
        ("tests/agents/test_aider.py::test_aider_gets_its_answer_back_and_the_provider_reads_no_secret",),
        ("openai-chat", "anthropic-key"),
        SETTING,
    ),
    Row(
        P_SDKS,
        "Programs started by latet run",
        ("latet run",),
        "none",
        "the launcher's own tests",
        CHAT + ANTHROPIC,
        ("http",),
        "as the program's",
        EVERY,
        MASKED,
        "fixture",
        ("tests/unit/test_launch_key_sinks.py::test_latet_run_starts_keyless_on_either_machine",),
        ("openai-chat", "anthropic-key"),
        "no launch key: its sessions are claimed (R-119)",
    ),
    Row(
        P_COMPANY,
        "A company gateway destination (Chat Completions)",
        ("gateway fixture",),
        "none",
        "the gateway's own client",
        CHAT,
        ("http",),
        "Bearer sk-…",
        EVERY + "; the destination's own credential headers by approval (D4)",
        MASKED,
        "fixture",
        ("tests/unit/test_request_profile_drift.py::test_each_familys_drift_has_its_documented_outcome_on_a_computer",),
        ("company",),
        "fixtures: NEGATIVE and DRIFT company on a computer and a server",
    ),
    Row(
        P_COMPANY,
        "Azure OpenAI and Foundry destinations",
        ("gateway fixture",),
        "none",
        "the gateway's own client",
        ("azure-openai.chat.1", "azure-openai.responses.1", "foundry.chat.1"),
        ("http",),
        "api-key …",
        EVERY + "; api-version and deployment exact",
        MASKED,
        "fixture",
        (
            "tests/unit/test_request_profile_fixes.py::test_explicit_destination_profile_owns_api_key_and_exact_query_roles",
        ),
        ("openai-chat",),
        "fixtures of the profile tests",
    ),
    Row(
        P_COMPANY,
        "Bedrock and Vertex fixed routes",
        ("gateway fixture",),
        "none",
        "the gateway's own client",
        BEDROCK + VERTEX,
        ("http",),
        "Bearer ABSK…, SigV4 by Latet, Bearer ya29…",
        EVERY + "; regions and locations enumerated",
        CHECKED,
        "fixture",
        (
            "tests/unit/test_cloud.py::test_bedrock_turns_go_to_the_region_s_runtime",
            "tests/e2e/test_cloud_e2e.py::test_vertex_is_checked_and_passes_unchanged",
        ),
        ("bedrock", "vertex"),
        "fixtures: NEGATIVE and DRIFT bedrock and vertex (vertex on a computer alone)",
    ),
    Row(
        P_COMPANY,
        "OpenRouter's route",
        ("gateway fixture",),
        "none",
        "the gateway's own client",
        CHAT,
        ("http",),
        "Bearer sk-or-v1-…",
        EVERY + "; Anthropic's, OpenAI's and Google's model ids by grammar, any other vendor's listed",
        MASKED,
        "fixture",
        (
            "tests/unit/test_request_profile_findings.py::test_an_openrouter_chat_with_a_vendor_model_is_masked_and_sent",
        ),
        ("openrouter",),
        "fixtures: NEGATIVE and DRIFT openrouter on a computer and a server",
    ),
    Row(
        P_TUTORIAL,
        "Latet's tutorial",
        ("tutorial",),
        "none",
        "the pretend model's fixed routes",
        (),
        ("http",),
        "the tutorial's demo key",
        "every chapter's request through the fixture profiles",
        MASKED,
        "fixture",
        (
            "tests/e2e/test_tutorial_chapters.py::test_a_chapter_holds_from_its_first_step_to_its_last",
            "tests/e2e/test_tutorial_safety.py::test_no_chapter_reaches_the_network",
        ),
        (),
        "the tutorial's e2e suites",
    ),
)

# Surfaces that are a desktop app or an editor's extension: never green because a CLI shares their settings.
_DESKTOP = re.compile(r"Desktop|extension|VS Code|JetBrains|Zed|Android Studio|Visual Studio")


def _plan_rows() -> list[str]:
    text = PLAN.read_text("utf-8")
    section = text.split("## Complete routed-agent qualification ledger to instantiate", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    return [row.split("|")[1].strip() for row in rows[1:] if not row.startswith("| ---")]


@pytest.mark.skipif(not PLAN.exists(), reason="the plan is a private note, not exported")
def test_every_row_of_the_plans_ledger_is_instantiated():
    plan = _plan_rows()
    assert len(plan) == 15
    assert {row.plan for row in LEDGER} == set(plan)


def test_each_row_is_complete_and_its_status_known():
    surfaces = [row.surface for row in LEDGER]
    assert len(surfaces) == len(set(surfaces))
    for row in LEDGER:
        assert row.status in STATUSES, row.surface
        assert row.clients and row.version and row.pin and row.credential and row.coverage and row.result, row.surface
        if row.status in ("observed", "fixture"):
            assert row.evidence and row.transports and row.masks != "none", row.surface


def test_every_evidence_node_exists():
    """By the file's syntax tree: no collection, nothing run."""
    for row in LEDGER:
        for node in row.evidence:
            path, _, name = node.partition("::")
            tree = ast.parse((ROOT / path).read_text("utf-8"))
            names = {item.name for item in tree.body if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)}
            assert name in names, node


def test_only_an_agent_that_ran_is_observed():
    observed = json.loads(OBSERVED.read_text("utf-8"))
    ran = {
        name.strip(): agent["version"]
        for key, agent in observed["agents"].items()
        if agent.get("run")
        for name in key.split(",")
    }
    for row in LEDGER:
        if row.status == "observed":
            for client in row.clients:
                assert client in ran, (row.surface, client)
                assert row.version == ran[client], row.surface


def test_a_skipped_executable_or_a_desktop_surface_is_untested_never_green():
    observed = json.loads(OBSERVED.read_text("utf-8"))
    skipped = {entry["agent"] for entry in observed["skipped"]}
    for row in LEDGER:
        if skipped & set(row.clients):
            assert row.status in ("untested", "outside"), row.surface
        if _DESKTOP.search(row.surface):
            assert row.status in ("untested", "outside"), row.surface


def test_every_profile_named_is_a_built_in_one():
    for row in LEDGER:
        assert set(row.profiles) <= PROFILE_IDS, (row.surface, set(row.profiles) - PROFILE_IDS)


def test_a_launched_surface_names_its_pinned_carrier():
    """G2 (re-review N1): every launcher's carrier is pinned in latet.launch_carriers, and the ledger reads it: a row a
    launcher starts names that carrier, whose verified versions its own version is one of."""
    pinned = {carrier.agent: carrier for carrier in CARRIERS}
    named = {row.carrier for row in LEDGER if row.carrier is not None}
    assert named == set(pinned)
    for row in LEDGER:
        if row.carrier is None or row.version == "none":
            continue
        carrier = pinned[row.carrier]
        assert any(version in row.version for version in carrier.verified), (row.surface, carrier.verified)


def test_each_family_has_its_negative_and_drift_fixtures():
    named = {family for row in LEDGER for family in row.families}
    assert named <= set(NEGATIVE) and named <= set(DRIFT)
    for family, rows in NEGATIVE.items():
        kinds = {fixture.kind for fixture in rows}
        assert "main" in kinds and "sibling" in kinds, family
    ids = [fixture.case.id for rows in (*NEGATIVE.values(), *DRIFT.values()) for fixture in rows]
    assert len(ids) == len(set(ids))


def _report_rows() -> dict[str, str]:
    text = REPORT.read_text("utf-8")
    table = text.split("<!-- ledger -->", 2)[1]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| ---")]
    return {cells[1].strip(): cells[3].strip() for cells in (row.split("|") for row in rows[1:])}


@pytest.mark.skipif(not REPORT.exists(), reason="the report is a private note, not exported")
def test_the_reports_ledger_is_this_ledger():
    """The report's table (between its two ``<!-- ledger -->`` marks) names each surface with its status, as here."""
    assert _report_rows() == {row.surface: row.status for row in LEDGER}


@pytest.fixture
async def cell(tmp_path):
    upstream = RecordingWSUpstream().start()
    try:
        async with computer_cell(tmp_path, upstream) as made:
            await apply_resources(made)
            yield made
    finally:
        upstream.stop()


@pytest.mark.parametrize("family", sorted(NEGATIVE))
async def test_each_familys_negative_fixtures_on_a_computer(cell, family):
    """Its main call, its ancillary calls and one unsupported sibling, through a computer's actual ingress: what each
    must give, the canary absent from everything that left."""
    found = {}
    for fixture in NEGATIVE[family]:
        if "computer" in fixture.case.machines:
            observation = await run_case(cell, fixture.case)
            if wrong := problems(cell, observation, fixture.expect):
                found[fixture.case.id] = wrong
    assert not found, found
