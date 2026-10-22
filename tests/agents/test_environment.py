"""What the real Claude Code and Codex are given to run with, checked without running them. The tests of the real agents
(``test_real_agents``, opt-in) start third-party programs, which pytest-socket does not fence: it patches the sockets of
the test process alone. So neither agent is left to look for an update or to send analytics, and each connection it makes
but those to 127.0.0.1 goes to a proxy port where nothing listens, where it fails."""

import socket
import tomllib
from urllib.parse import urlsplit

import pytest

from .test_real_agents import _codex_config, _env

PROXIES = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}


def test_every_connection_but_those_to_this_computer_goes_to_a_port_nothing_listens_on(monkeypatch):
    for name, value in (  # this computer's own, in either case: none may reach the agents
        ("http_proxy", "http://proxy.acme.example:3128"),
        ("HTTPS_PROXY", "http://proxy.acme.example:3128"),
        ("all_proxy", "socks5://proxy.acme.example:1080"),
        ("no_proxy", ".acme.example"),
    ):
        monkeypatch.setenv(name, value)
    env = _env()
    assert {name for name in env if name.upper() in PROXIES | {"ALL_PROXY"}} == PROXIES
    assert env["NO_PROXY"] == "127.0.0.1"
    [proxy] = {env["HTTP_PROXY"], env["HTTPS_PROXY"]}
    address = urlsplit(proxy)
    assert address.hostname == "127.0.0.1" and address.port
    # Windows takes about two seconds to refuse a connection to a closed port, whatever the load.
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", address.port), timeout=10):
        pass


def test_a_route_given_to_an_agent_is_kept_whatever_the_proxies_are():
    env = _env(ANTHROPIC_BASE_URL="http://127.0.0.1:5283/acme-claude")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5283/acme-claude" and env["NO_PROXY"] == "127.0.0.1"


def test_claude_code_looks_for_no_update_and_sends_no_telemetry(monkeypatch):
    names = ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY")
    for name in names:
        monkeypatch.delenv(name, raising=False)  # this computer may set them: they would answer for the test
    env = _env()
    for name in names:
        assert env[name] == "1", name


def test_codex_looks_for_no_update_and_sends_no_analytics():
    config = tomllib.loads(_codex_config(5283))
    assert config["check_for_update_on_startup"] is False
    assert config["analytics"] == {"enabled": False}
    assert config["model_provider"] == "acme"  # and it still reaches the company provider, on this computer
    assert config["model_providers"]["acme"]["base_url"] == "http://127.0.0.1:5283/acme-codex"
