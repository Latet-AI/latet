"""Whether a command runs inside an AI agent, or at your own terminal.

Agents mark the shells they run commands in with variables of their own. This is a guard against
mistakes, not a lock: an agent that runs as you can unset them, and read what Latet reads. What
lowers protection is checked by the daemon as well, whoever asks (see `latet.protection`).
"""

from __future__ import annotations

import os
import sys

# The variables each agent sets in the shells it runs commands in (present, even when empty).
MARKERS: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SANDBOX", "CODEX_THREAD_ID"),
    "opencode": ("OPENCODE",),
    "cursor": ("CURSOR_AGENT",),
    "hermes": ("HERMES_SESSION_ID", "HERMES_SESSION_KEY"),
    "goose": ("GOOSE_TERMINAL",),
    "openclaw": ("OPENCLAW_SHELL",),
    "gemini-cli": ("GEMINI_CLI",),
}
# Prefixes of their variables: some are settings you may set yourself, so only the strict check
# (before showing real values) counts them.
PREFIXES = {"CLAUDE_CODE_": "claude-code", "CODEX_": "codex", "OPENCODE_": "opencode", "AIDER_": "aider"}


def agent_name(strict: bool = False) -> str | None:
    """The agent this command runs in, or None. ``strict`` also counts any variable of theirs."""
    env = os.environ
    for name, markers in MARKERS.items():
        if any(marker in env for marker in markers):
            return name
    agent = env.get("AGENT", "").strip()  # a convention some agents follow (AGENT=goose)
    if agent:
        return agent.lower()[:40]
    if strict:
        for prefix, name in PREFIXES.items():
            if any(key.startswith(prefix) for key in env):
                return name
    return None


def at_your_terminal(strict: bool = False) -> bool:
    """You at a terminal of your own: input and output are a terminal, and no agent is around."""
    return sys.stdin.isatty() and sys.stdout.isatty() and agent_name(strict) is None
