"""The launch-key carriers of Latet's launchers, pinned (R-119; P2-8's qualification): for each agent what carries its
key, the first version given one (an older one is given none, or `latet doctor` names its claimed sessions), the
versions whose source the pin was read from, and where. The launchers, `latet doctor` (``doctor.upgrade._no_carrier``)
and the qualification ledger (``tests/agents/test_request_profile_compatibility.py``) read this one table."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Carrier:
    agent: str  # the history's name of the agent (storage.exchanges' agents; Copilot CLI's requests show as "other")
    command: str
    carrier: str
    floor: tuple[int, int, int] | None  # the first version given a key; None: no version floor
    verified: tuple[str, ...]  # the versions whose source or observed calls the pin was read from
    anchors: tuple[str, ...]


CARRIERS: tuple[Carrier, ...] = (
    Carrier(
        "claude-code",
        "latet claude",
        "ANTHROPIC_CUSTOM_HEADERS in its private --settings file",
        None,
        ("2.1.312",),
        ("P1-0's observed calls: Claude Code 2.1.312 sends ANTHROPIC_CUSTOM_HEADERS",),
    ),
    Carrier(
        "codex",
        "latet codex",
        "env_http_headers on a provider of model_providers",
        (0, 115, 0),
        ("0.162.0",),
        ("Codex c1382380 (0.162.0): a model_providers entry's env_http_headers (P1-0 D-11, F-3)",),
    ),
    Carrier(
        "opencode",
        "latet opencode",
        "options.headers naming {env:...} in OPENCODE_CONFIG_CONTENT",
        (1, 19, 5),
        ("1.19.5",),
        (
            "OpenCode 810c0eba (1.19.5) packages/opencode/src/config/config.ts:227-238, 482-489",
            "OpenCode 810c0eba packages/opencode/src/config/variable.ts:34-38",
            "OpenCode 810c0eba packages/opencode/src/provider/provider.ts:1787-1880",
            "OpenCode 1.0.0 packages/opencode/src/config/config.ts:44-46 (no substitution)",
        ),
    ),
    Carrier(
        "gemini-cli",
        "latet gemini",
        "GEMINI_CLI_CUSTOM_HEADERS",
        (0, 19, 0),
        ("0.19.0", "0.66.0"),
        (
            "Gemini CLI acf5ed595f (#11893): first in 0.19.0",
            "Gemini CLI 97041320 (0.66.0) packages/core/src/core/contentGenerator.ts:240-241, 275-283, 312-412",
            "Gemini CLI 97041320 packages/core/src/utils/customHeaderUtils.ts:10-41",
            "Gemini CLI 578f03324e (#29865, 0.67.0-preview.0): the newer split keeps the same entries",
            "Gemini CLI 97041320 packages/cli/src/config/config.ts:363-371; "
            "packages/cli/src/acp/acpRpcDispatcher.ts:128-157 (ACP mode: no key)",
        ),
    ),
    Carrier(
        "copilot",
        "latet copilot",
        "COPILOT_PROVIDER_HEADERS on Chat Completions",
        None,
        ("1.0.103",),
        ("Copilot CLI 1.0.103 (P1-0 D-6): Chat Completions alone; Responses, Anthropic and WebSocket pass no key",),
    ),
)


def floor(agent: str) -> tuple[int, int, int] | None:
    """The first version of ``agent`` its launcher gives a key; None for no floor, or an agent the table does not
    name."""
    return next((carrier.floor for carrier in CARRIERS if carrier.agent == agent), None)
