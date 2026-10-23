import json
import shutil
import tomllib
from pathlib import Path

import pytest

from latet import wrappers
from latet.integrations import agents, jsonc
from latet.integrations.agents import ClaudeCode, ClaudeOptions, Codex, OpenCode
from latet.integrations.routes import ROUTE_VARS

CODEX_TOML = """model = "gpt-6-sol" # my model
model_reasoning_effort = "medium"

[windows]
sandbox = "unelevated"

[mcp_servers.node_repl]
command = "node"
"""

OPENCODE_JSONC = """{
  // my OpenCode setup
  "$schema": "https://opencode.ai/config.json",
  "model": "openrouter/qwen/qwen3.8-flash",
  "plugin": ["superpowers@git+https://github.com/obra/superpowers.git"],
}
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", "t-123")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for var in ROUTE_VARS:  # where the agents go is read from the shell too: none of this one's
        monkeypatch.delenv(var, raising=False)
    # A Codex that reads config.toml: the one installed on this computer is never asked.
    monkeypatch.setattr(wrappers, "codex_version", lambda exe="codex": (0, 161, 0))
    return tmp_path


def test_codex_apply_and_byte_identical_revert(isolated):
    path = isolated / "codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(CODEX_TOML.encode())
    codex = Codex()
    codex.apply(chatgpt=True)
    data = tomllib.loads(path.read_text())
    assert data["openai_base_url"] == "http://127.0.0.1:5283/backend-api/codex"
    assert data["windows"]["sandbox"] == "unelevated"
    assert "# my model" in path.read_text()
    assert codex.status() == "applied"
    assert codex.revert() == "restored"
    assert path.read_bytes() == CODEX_TOML.encode()


def test_codex_revert_after_user_edit_removes_only_latet_key(isolated):
    path = isolated / "codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(CODEX_TOML)
    codex = Codex()
    codex.apply(chatgpt=False)
    path.write_text(path.read_text().replace('"medium"', '"high"'))
    assert codex.revert().startswith("drifted")
    data = tomllib.loads(path.read_text())
    assert "openai_base_url" not in data
    assert data["model_reasoning_effort"] == "high"


def test_opencode_apply_keeps_comments_and_reverts(isolated):
    path = isolated / "xdg" / "opencode" / "opencode.jsonc"
    path.parent.mkdir(parents=True)
    path.write_bytes(OPENCODE_JSONC.encode())
    opencode = OpenCode()
    opencode.apply()
    text = path.read_text()
    assert "// my OpenCode setup" in text
    providers = jsonc.loads(text)["provider"]
    assert providers["openrouter"]["options"]["baseURL"] == "http://127.0.0.1:5283/openrouter/api/v1"
    assert providers["anthropic"]["options"]["baseURL"] == "http://127.0.0.1:5283/anthropic/v1"
    assert opencode.status() == "applied"
    opencode.revert()
    assert path.read_bytes() == OPENCODE_JSONC.encode()


def test_opencode_desktop_is_found_where_its_installer_puts_it(tmp_path):
    local, home = tmp_path / "local", tmp_path / "home"
    assert agents._opencode_desktop_paths("win32", str(local), home) == [
        local / "Programs" / "@opencode-aidesktop" / "OpenCode.exe"
    ]
    assert agents._opencode_desktop_paths("darwin", None, home) == [
        Path("/Applications/OpenCode.app"),
        home / "Applications" / "OpenCode.app",
    ]
    assert agents._opencode_desktop_paths("linux", None, home) == []


def test_opencode_desktop_alone_counts_as_opencode_installed(isolated, monkeypatch):
    """OpenCode Desktop reads the same global configuration as the command line: `latet agents status`, `doctor` and
    `global-install` said it was not installed when the `opencode` command was not there."""
    app = isolated / "OpenCode.exe"
    app.write_bytes(b"")
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(agents, "_opencode_desktop", lambda: app)
    info = OpenCode().detect()
    assert info.installed and info.version == "desktop app"
    assert any("OpenCode Desktop" in note and "latet opencode" in note for note in info.notes)
    monkeypatch.setattr(agents, "_opencode_desktop", lambda: None)
    assert not OpenCode().detect().installed


def test_opencodes_chatgpt_sign_in_is_said_to_be_on_the_roadmap_not_in_a_version(isolated, monkeypatch):
    """The note named a version (v1.1) that no longer comes next: Latet's versions are 0.9.x, and 1.0.0 is the
    Enterprise release. The OpenCode guide says the ChatGPT sign-in is on the roadmap."""
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    monkeypatch.setattr(agents, "_opencode_desktop", lambda: None)
    [note] = [note for note in OpenCode().detect().notes if "ChatGPT" in note]
    assert note == "API-key providers only: OpenCode's ChatGPT sign-in plugin cannot be routed (on the roadmap)."


@pytest.mark.e2e
@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code is not installed")
def test_claude_code_plugin_and_env_round_trip(isolated):
    settings = isolated / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps({"outputStyle": "Concise", "permissions": {"defaultMode": "default"}}, indent=2) + "\n"
    settings.write_text(original)
    claude = ClaudeCode()
    claude.apply(ClaudeOptions(disable_classifier_context=True))
    data = json.loads(settings.read_text())
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5283/anthropic"
    assert data["env"]["LATET_BRIDGE_TOKEN"] == "t-123"
    assert data["env"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert data["enabledPlugins"]["latet@latet"] is True
    assert data["outputStyle"] == "Concise"
    assert claude.status() == "applied"
    assert claude.revert() == "restored"
    assert settings.read_text() == original
