"""GitHub Copilot CLI through Latet: `latet copilot` (and `latet run copilot`) starts it with the address of a provider of
your own (COPILOT_PROVIDER_*) pointed at Latet, for that session alone, and refuses to start it with none: signed in with
GitHub, it sends to GitHub's Copilot service, which Latet cannot go in front of."""

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app

from .test_wrapper_routes import launched  # noqa: F401 - the fixture

COPILOT_VARS = (
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_MODEL",
    "COPILOT_OFFLINE",
)


@pytest.fixture
def copilot(launched, monkeypatch):  # noqa: F811
    """The launched agent, with none of this shell's Copilot variables."""
    for name in COPILOT_VARS:
        monkeypatch.delenv(name, raising=False)
    return launched


def _start(monkeypatch, variables: dict[str, str], *args: str):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    return CliRunner().invoke(app, ["copilot", *args])


def test_copilot_cli_signed_in_with_github_does_not_start(copilot, monkeypatch):
    runs, added, _ = copilot
    result = _start(monkeypatch, {})
    assert result.exit_code == 2 and runs == [] and added == []
    assert "signed in with GitHub" in result.output and "COPILOT_PROVIDER_TYPE" in result.output


@pytest.mark.parametrize(
    ("variables", "route"),
    [
        ({"COPILOT_PROVIDER_TYPE": "openai"}, "/openai/v1"),
        ({"COPILOT_PROVIDER_TYPE": "anthropic"}, "/anthropic"),
        ({"COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_BASE_URL": "https://api.openai.com/v1"}, "/openai/v1"),
        (
            {"COPILOT_PROVIDER_TYPE": "anthropic", "COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com"},
            "/anthropic",
        ),
        (
            {"COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_BASE_URL": "https://openrouter.ai/api/v1/"},
            "/openrouter/api/v1",
        ),
        ({"COPILOT_PROVIDER_BASE_URL": "https://openrouter.ai/api/v1"}, "/openrouter/api/v1"),
    ],
)
def test_a_provider_of_your_own_goes_through_latet(copilot, monkeypatch, variables, route):
    runs, added, _ = copilot
    result = _start(monkeypatch, {**variables, "COPILOT_PROVIDER_API_KEY": "sk-test-not-a-key"}, "-p", "hello")
    assert result.exit_code == 0, result.output
    [run] = runs
    assert run["argv"][0].endswith("copilot") and run["argv"][1:] == ["-p", "hello"]
    assert run["env"]["COPILOT_PROVIDER_BASE_URL"] == wrappers.base_url() + route
    assert added == []  # Latet's own routes: no destination added


def test_another_providers_address_becomes_a_destination(copilot, monkeypatch):
    runs, added, _ = copilot
    variables = {"COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_BASE_URL": "https://llm.acme.com/v1"}
    result = _start(monkeypatch, variables)
    assert result.exit_code == 0, result.output
    assert runs[0]["env"]["COPILOT_PROVIDER_BASE_URL"] == f"{wrappers.base_url()}/acme"
    assert added == [("https://llm.acme.com/v1", None)]


def test_an_address_of_latet_is_left_as_it_is(copilot, monkeypatch):
    runs, added, _ = copilot
    variables = {"COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_BASE_URL": f"{wrappers.base_url()}/openai/v1"}
    result = _start(monkeypatch, variables)
    assert result.exit_code == 0, result.output
    assert "COPILOT_PROVIDER_BASE_URL" not in runs[0]["env"] and added == []


def test_an_address_on_this_computer_is_left_as_it_is_and_said(copilot, monkeypatch):
    runs, added, _ = copilot
    variables = {"COPILOT_PROVIDER_TYPE": "openai", "COPILOT_PROVIDER_BASE_URL": "http://localhost:11434/v1"}
    result = _start(monkeypatch, variables)
    assert result.exit_code == 0, result.output
    assert "COPILOT_PROVIDER_BASE_URL" not in runs[0]["env"] and added == []
    assert "on this computer" in result.output


@pytest.mark.parametrize(
    ("variables", "said"),
    [
        ({"COPILOT_PROVIDER_BASE_URL": "http://llm.acme.com/v1"}, "llm.acme.com"),  # in clear, on the internet
        ({"COPILOT_PROVIDER_TYPE": "azure"}, "COPILOT_PROVIDER_BASE_URL"),  # no address Latet knows for it
    ],
)
def test_a_provider_latet_cannot_go_in_front_of_does_not_start(copilot, monkeypatch, variables, said):
    runs, added, _ = copilot
    result = _start(monkeypatch, variables)
    assert result.exit_code == 2 and runs == [] and added == []
    assert said in result.output


def test_latet_run_copilot_is_latet_copilot(copilot, monkeypatch):
    runs, _, _ = copilot
    monkeypatch.setenv("COPILOT_PROVIDER_TYPE", "openai")
    result = CliRunner().invoke(app, ["run", "copilot", "--version"])
    assert result.exit_code == 0, result.output
    assert "latet run copilot is latet copilot" in result.output
    assert runs[0]["env"]["COPILOT_PROVIDER_BASE_URL"] == f"{wrappers.base_url()}/openai/v1"
