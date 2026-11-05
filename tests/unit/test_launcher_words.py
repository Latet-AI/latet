"""What a launcher tells the person when the agent it starts has no launch key (root's ruling I-5 on P2-6a's review): a
plain sentence that says why and what it means, never a code; and a user's own header values left as they were (M4,
the review's Minor 7)."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app
from latet.launch_session import claude_headers, keyless_words

from .test_wrapper_routes import REAL_RUN, launched  # noqa: F401 - fixture

_CODE = re.compile(
    r"\b(?:carrier-unverified|launch-[a-z]+|builtin-provider|older-codex|older-opencode|unknown-opencode"
    r"|older-gemini|gemini-acp)\b"
)
_AGENTS = ("claude", "codex", "opencode", "gemini", "copilot", "run")
_REASONS = (
    "carrier-unverified",
    "builtin-provider",
    "older-codex",
    "older-opencode",
    "unknown-opencode",
    "older-gemini",
    "gemini-acp",
    "launch-capacity",
    "launch-unavailable",
    "launch-invalid",
    "launch-ended",
    "launch-stale",
    "launch-mismatch",
    "launch-busy",
    "unauthorized",
)


@pytest.mark.parametrize("agent", _AGENTS)
@pytest.mark.parametrize("reason", _REASONS)
def test_every_keyless_start_is_said_in_a_sentence_never_a_code(agent, reason):
    said = keyless_words(agent, reason)
    assert said is not None
    words, warn = said
    assert warn and words.startswith("Latet") and words.endswith(".") and not _CODE.search(words), words
    assert "loosenings do not apply" in words, words


def test_the_sentences_say_why_for_the_common_cases():
    assert "built-in" in keyless_words("codex", "builtin-provider")[0]
    assert "0.115" in keyless_words("codex", "older-codex")[0]
    assert "too many" in keyless_words("copilot", "launch-capacity")[0]
    assert "Chat Completions" in keyless_words("copilot", "carrier-unverified")[0]
    assert "latet run" in keyless_words("run", "carrier-unverified")[0]


def test_a_folder_that_is_no_project_is_said_quietly_and_a_local_route_not_at_all():
    words, warn = keyless_words("codex", "launch-unregistered")
    assert not warn and "not one of your projects" in words and not _CODE.search(words)
    assert keyless_words("claude", "local-route") is None  # Latet is not in front of that route at all (Minor 8)


def test_latet_run_says_why_it_has_no_key(launched, monkeypatch):  # noqa: F811
    monkeypatch.setattr(wrappers, "run", REAL_RUN)
    monkeypatch.setattr(wrappers.subprocess, "call", lambda argv, env: 0)
    result = CliRunner().invoke(app, ["run", "ordinary-tool"])
    assert result.exit_code == 0
    assert "latet run" in result.output and not _CODE.search(result.output), result.output


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        ("X-Company: a\x0bb", "X-Company: a\x0bb"),  # a vertical tab inside a value is the user's
        ("X-One: 1\r\nX-Two: 2", "X-One: 1\r\nX-Two: 2"),  # CRLF stays CRLF
        ("X-Note: a\N{LINE SEPARATOR}b\nX-Other: c", "X-Note: a\N{LINE SEPARATOR}b\nX-Other: c"),
        ("X-One: 1\nX-Latet-Launch-Key: old\n", "X-One: 1\n"),
    ],
)
def test_claude_headers_splits_on_newlines_alone(existing, kept):
    assert claude_headers(existing, None) == kept
    key = "latet_lk_" + "A" * 43
    with_key = claude_headers(existing, key)
    assert with_key.startswith(kept.rstrip("\n")) and with_key.endswith("\nx-latet-launch-key: " + key)
    assert with_key.count("x-latet-launch-key") == 1


def test_an_absent_header_variable_stays_absent(launched, monkeypatch):  # noqa: F811
    for name in ("COPILOT_PROVIDER_HEADERS", "GEMINI_CLI_CUSTOM_HEADERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "anthropic")  # an unverified carrier: no key
    runs, _, _ = launched
    result = CliRunner().invoke(app, ["copilot", "-p", "hello"])
    assert result.exit_code == 0, result.output
    assert len(runs) == 1 and "COPILOT_PROVIDER_HEADERS" not in runs[0]["env"]


def test_claude_code_on_a_local_route_says_nothing_of_a_launch_key(launched, monkeypatch):  # noqa: F811
    """Latet is not in front of a route on this computer (the review's Minor 8): there is no key to carry."""
    runs, _, _ = launched
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4000")
    result = CliRunner().invoke(app, ["claude"])
    assert result.exit_code == 0 and len(runs) == 1
    assert "launch key" not in result.output and not _CODE.search(result.output), result.output


def test_codex_on_its_built_in_provider_says_why_it_has_no_key(launched):  # noqa: F811
    runs, _, _ = launched
    result = CliRunner().invoke(app, ["codex", "exec", "hi"])
    assert result.exit_code == 0 and len(runs) == 1, result.output
    assert "built-in providers" in result.output and not _CODE.search(result.output), result.output
