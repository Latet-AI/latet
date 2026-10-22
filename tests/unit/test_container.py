"""The container image's code paths: its port, key and identity, the host-only commands, and the
instructions and file merges for agents it cannot reach."""

import base64
import json
import tomllib

import pytest
from typer.testing import CliRunner

from latet.config import Config, IdentityConfig, load_config, save_config
from latet.runtime import resolve_identity
from latet.storage.keys import load_root_key


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.setenv("LATET_BRIDGE_TOKEN", "t-container-123")
    from latet.cli import admin

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)
    return tmp_path


def _cli(*args, input=None):
    import latet.cli.admin  # noqa: F401 - registers the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def test_the_port_comes_from_outside(isolated, monkeypatch):
    save_config(Config(port=5283), isolated / "config" / "config.yaml")
    monkeypatch.setenv("LATET_PORT", "6690")
    assert load_config().port == 6690


def test_the_root_key_lives_in_the_volume_or_a_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("LATET_ROOT_KEY", raising=False)
    monkeypatch.setenv("LATET_KEYRING", "off")
    import latet.storage.keys as keys

    monkeypatch.setattr(keys, "_from_keyring", lambda: pytest.fail("the keyring is off"))
    first = load_root_key(tmp_path)
    assert (tmp_path / "root.key").exists() and load_root_key(tmp_path) == first  # kept across restarts
    secret = tmp_path / "secret"
    secret.write_text(base64.b64encode(b"s" * 32).decode())
    monkeypatch.setenv("LATET_ROOT_KEY_FILE", str(secret))
    assert load_root_key(tmp_path / "other") == b"s" * 32


def test_the_container_never_guesses_whose_identity_it_is(monkeypatch):
    monkeypatch.setenv("LATET_CONTAINER", "1")
    assert resolve_identity(Config()) is None  # its own user and host name are not yours
    given = Config(identity=IdentityConfig(usernames=["jdoe"]))
    assert resolve_identity(given) is not None


def test_host_only_commands_refuse_in_the_container(isolated, monkeypatch):
    monkeypatch.setenv("LATET_CONTAINER", "1")
    for args in (["global-install", "--yes"], ["agents", "apply", "codex"], ["service", "install"], ["upgrade"]):
        result = _cli(*args)
        assert result.exit_code == 2 and "not in the container" in result.output, args


def test_listening_beyond_this_machine_needs_the_container(isolated, monkeypatch):
    from latet.cli.main import run_server

    monkeypatch.delenv("LATET_CONTAINER", raising=False)
    with pytest.raises(SystemExit) as stopped:
        run_server(port=6699, host="0.0.0.0")
    assert stopped.value.code == 2


def test_agents_print_never_shows_the_token(isolated):
    text = _cli("agents", "print").output
    assert "claude plugin marketplace add Latet-AI/latet" in text and "identity set" in text
    report = json.loads(_cli("agents", "print", "--json", "--shell", "powershell").output)
    assert "t-container-123" not in text and "t-container-123" not in json.dumps(report)
    assert report["claude"]["env"]["LATET_AUTOSTART"] == "0"
    assert "docker cp" in report["codex"]["merge"] and "$env:USERNAME" in report["identity"]


def test_powershells_merge_copies_through_the_data_volume_the_read_only_container_can_write(isolated):
    """PowerShell has no `<` redirection, so the file is copied in and out with `docker cp`: through the container's
    data volume, since its root is read-only and its /tmp a tmpfs, which `docker cp` cannot write; and the copies are
    removed afterwards."""
    report = json.loads(_cli("agents", "print", "--json", "--shell", "powershell").output)
    for agent in ("claude", "codex", "opencode"):
        merge = report[agent]["merge"]
        assert "/tmp/" not in merge, merge
        assert "latet:/data/merge-in" in merge and "latet:/data/merge-out" in merge
        assert "rm -f /data/merge-in /data/merge-out" in merge


def test_agents_merge_writes_each_file_and_takes_it_out_again(isolated):
    claude = json.loads(_cli("agents", "merge", "claude", input='{"model": "opus"}').output)
    assert claude["model"] == "opus" and claude["env"]["LATET_BRIDGE_TOKEN"] == "t-container-123"
    removed = json.loads(_cli("agents", "merge", "claude", "--remove", input=json.dumps(claude)).output)
    assert removed == {"model": "opus"}
    codex = _cli("agents", "merge", "codex", input='# mine\nmodel = "x"\n').output
    assert codex.startswith("# mine") and tomllib.loads(codex)["openai_base_url"].endswith("/backend-api/codex")
    assert "openai_base_url" not in _cli("agents", "merge", "codex", "--remove", input=codex).output
    opencode = _cli("agents", "merge", "opencode", input="{\n  // mine\n}\n").output
    assert "// mine" in opencode and "/anthropic/v1" in opencode


def test_identity_set_and_show(isolated):
    args = ["identity", "set", "--user-name", "jdoe", "--user-name", "john", "--host-name", "WS-1"]
    assert _cli(*args, "--full-name", "Jane Doe", "--email", "jane@doe.example").exit_code == 0
    identity = load_config().identity
    assert identity.usernames == ["jdoe", "john"] and identity.hostname == "WS-1" and identity.full_name == "Jane Doe"
    assert "jdoe, john" in _cli("identity", "show").output


def test_plugin_versions_agree():
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    plugin = json.loads((root / "src/latet/claude_plugin/.claude-plugin/plugin.json").read_text("utf-8"))["version"]
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text("utf-8"))["plugins"][0]["version"]
    hook = re.search(
        r"PLUGIN_VERSION = '([^']+)'", (root / "src/latet/claude_plugin/hooks/register.ts").read_text("utf-8")
    )
    package = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    assert hook is not None and plugin == market == hook.group(1)
    assert package.startswith(plugin)  # 0.1.0.dev0 until the release says 0.1.0
