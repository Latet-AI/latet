"""The plugin's check at the start of a Claude Code session: are its requests really going through
Latet? Facts from the plugin (variables, and the settings file that set each), a verdict back."""

import pytest

from latet.integrations.routes import check_claude_route

BRIDGE = "http://127.0.0.1:5283"


def _check(env, sources=None, entrypoint="cli", surface="terminal", bridge=BRIDGE):
    facts = {"bridge": bridge, "env": env, "sources": sources or {}, "entrypoint": entrypoint, "surface": surface}
    return check_claude_route(facts)


@pytest.mark.parametrize(
    "env",
    [
        {"ANTHROPIC_BASE_URL": f"{BRIDGE}/anthropic"},
        {"ANTHROPIC_BASE_URL": f"{BRIDGE}/llm-acme-com"},
        {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": f"{BRIDGE}/bedrock/eu-west-1"},
        {"CLAUDE_CODE_USE_VERTEX": "true", "ANTHROPIC_VERTEX_BASE_URL": f"{BRIDGE}/vertex/v1"},
        {"CLAUDE_CODE_USE_FOUNDRY": "1", "ANTHROPIC_FOUNDRY_BASE_URL": f"{BRIDGE}/foundry-acme"},
    ],
)
def test_a_session_through_latet_goes_on(env):
    verdict = _check(env, {"ANTHROPIC_BASE_URL": "flag"})
    assert verdict.ok and verdict.reason is None


def test_a_project_that_sends_claude_code_elsewhere_is_stopped_and_named():
    verdict = _check({"ANTHROPIC_BASE_URL": "https://x.example"}, {"ANTHROPIC_BASE_URL": "project"})
    assert not verdict.ok
    assert "x.example" in verdict.reason and ".claude/settings.json" in verdict.reason


def test_a_local_settings_file_is_named_too():
    verdict = _check({"ANTHROPIC_BASE_URL": "https://x.example"}, {"ANTHROPIC_BASE_URL": "local"})
    assert not verdict.ok and ".claude/settings.local.json" in verdict.reason


def test_a_cloud_reached_directly_is_stopped():
    verdict = _check({"CLAUDE_CODE_USE_BEDROCK": "1"}, {"CLAUDE_CODE_USE_BEDROCK": "user"})
    assert not verdict.ok and "Amazon Bedrock" in verdict.reason and "latet claude" in verdict.reason


def test_no_address_at_all_goes_straight_to_anthropic():
    verdict = _check({})
    assert not verdict.ok and "api.anthropic.com" in verdict.reason


def test_mantle_is_not_read():
    assert not _check({"CLAUDE_CODE_USE_MANTLE": "1"}).ok


def test_an_address_on_this_computer_is_left_alone_and_said_once():
    verdict = _check({"ANTHROPIC_BASE_URL": "http://localhost:4000"})
    assert verdict.ok and "localhost:4000" in verdict.toast


@pytest.mark.parametrize("bridge", ["https://evil.example", "http://10.0.0.5:5283"])
def test_a_bridge_away_from_this_computer_is_refused(bridge):
    assert not _check({"ANTHROPIC_BASE_URL": f"{bridge}/anthropic"}, bridge=bridge).ok


def test_a_bridge_named_by_a_repository_is_refused():
    verdict = _check({"ANTHROPIC_BASE_URL": f"{BRIDGE}/anthropic"}, {"LATET_BRIDGE_URL": "project"})
    assert not verdict.ok and "LATET_BRIDGE_URL" in verdict.reason


def test_claude_desktop_is_told_not_stopped():
    verdict = _check({"ANTHROPIC_BASE_URL": "https://x.example"}, {"ANTHROPIC_BASE_URL": "project"}, "claude-desktop")
    assert verdict.ok and "Claude Desktop" in verdict.toast


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example\\@127.0.0.1:5283/anthropic",  # Node sends to evil.example; urlsplit reads 127.0.0.1
        "http://evil.example\\[/anthropic",  # urlsplit cannot read it at all
        "https://127.attacker.example/anthropic",  # a name, not an address on this computer
        "http://user:pw@127.0.0.1:5283/anthropic",
        "http://127.0.0.1:5283\t.evil.example/anthropic",
    ],
)
def test_an_address_that_only_looks_like_latet_is_stopped(url):
    verdict = _check({"ANTHROPIC_BASE_URL": url}, {"ANTHROPIC_BASE_URL": "user"})
    assert not verdict.ok and verdict.reason


def test_a_repository_never_picks_the_route_even_through_latet():
    verdict = _check({"ANTHROPIC_BASE_URL": f"{BRIDGE}/acme"}, {"ANTHROPIC_BASE_URL": "project"})
    assert not verdict.ok and ".claude/settings.json" in verdict.reason


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "http_proxy", "ALL_PROXY", "ANTHROPIC_FOUNDRY_RESOURCE"])
def test_a_repository_never_sets_a_proxy_or_a_resource(name):
    verdict = _check({"ANTHROPIC_BASE_URL": f"{BRIDGE}/anthropic"}, {name: "local"})
    assert not verdict.ok and name in verdict.reason and ".claude/settings.local.json" in verdict.reason


def test_claude_desktop_named_by_a_repository_is_not_believed():
    sources = {"ANTHROPIC_BASE_URL": "project", "CLAUDE_CODE_ENTRYPOINT": "project"}
    verdict = _check({"ANTHROPIC_BASE_URL": "https://x.example"}, sources, "claude-desktop", surface=None)
    assert not verdict.ok


def test_two_clouds_at_once_are_stopped():
    env = {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_BASE_URL": f"{BRIDGE}/v"}
    verdict = _check(env)
    assert not verdict.ok and "CLAUDE_CODE_USE_BEDROCK" in verdict.reason


@pytest.mark.parametrize(
    "facts",
    [
        {"bridge": 5283},
        {"bridge": "http://127.0.0.1:99999"},
        {"bridge": BRIDGE, "env": ["x"]},
        {"bridge": BRIDGE, "sources": "x"},
        {"bridge": BRIDGE, "env": {"ANTHROPIC_BASE_URL": "http://[::1"}},
    ],
)
def test_facts_that_cannot_be_read_stop_the_session(facts):
    verdict = check_claude_route(facts)
    assert not verdict.ok and verdict.reason


def test_a_password_in_an_address_is_never_said():
    verdict = _check({"ANTHROPIC_BASE_URL": "https://me:hunter2@gw.example"}, {"ANTHROPIC_BASE_URL": "user"})
    assert not verdict.ok and "hunter2" not in verdict.reason and "gw.example" in verdict.reason
