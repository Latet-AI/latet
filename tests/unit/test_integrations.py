import json
import shutil
import tomllib

import pytest

from latet import wrappers
from latet.integrations import jsonc
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
