"""Gemini CLI through Latet: `latet gemini` (and `latet run gemini`) starts it with the two base URLs when it uses an API
key or Vertex, and refuses to start it signed in with Google, which Latet cannot go in front of; `latet env` and `latet
run` give every program the base URLs; doctor and the Protected agents page know it."""

import json

import pytest
from typer.testing import CliRunner

from latet import wrappers
from latet.cli.main import app
from latet.doctor import checks
from latet.integrations import agents
from latet.integrations.agents import GeminiCli

from .test_wrapper_routes import launched  # noqa: F401 - the fixture

GOOGLE_VARS = (
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_DEFAULT_AUTH_TYPE",
    "GEMINI_CLI_HOME",
    "GEMINI_CLI_AUTH_OVERRIDE",
)


@pytest.fixture
def gemini_home(tmp_path, monkeypatch):
    """Gemini CLI's folder, here; and none of this shell's Google variables."""
    for name in GOOGLE_VARS:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "gemini-home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(tmp_path / "system" / "settings.json"))
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", str(tmp_path / "system" / "system-defaults.json"))
    monkeypatch.chdir(tmp_path)  # a folder with no .gemini of its own
    return home


def _choose(home, selected: str | None, *, legacy: bool = False) -> None:
    settings: dict = {"general": {"vimMode": False}}
    if selected is not None and legacy:
        settings["selectedAuthType"] = selected
    elif selected is not None:
        settings["security"] = {"auth": {"selectedType": selected}}
    (home / ".gemini" / "settings.json").write_text(json.dumps(settings), "utf-8")


def test_latet_env_gives_the_gemini_and_vertex_base_urls():
    result = CliRunner().invoke(app, ["env", "--shell", "bash"])
    assert result.exit_code == 0, result.output
    base = wrappers.base_url()
    assert f"export GOOGLE_GEMINI_BASE_URL='{base}/gemini'" in result.output
    assert f"export GOOGLE_VERTEX_BASE_URL='{base}/vertex'" in result.output


def test_generic_env_on_an_api_keys_space_names_its_routes():
    values = wrappers.generic_env(base="https://latet.acme.com/u/abc")
    assert values["GOOGLE_GEMINI_BASE_URL"] == "https://latet.acme.com/u/abc/gemini"
    assert values["GOOGLE_VERTEX_BASE_URL"] == "https://latet.acme.com/u/abc/vertex"


@pytest.mark.parametrize("selected", ["gemini-api-key", "vertex-ai", "gateway"])
def test_gemini_cli_starts_with_both_base_urls(launched, gemini_home, selected):  # noqa: F811
    runs, _, _ = launched
    _choose(gemini_home, selected)
    result = CliRunner().invoke(app, ["gemini", "-p", "hello"])
    assert result.exit_code == 0, result.output
    [run] = runs
    assert run["argv"][0].endswith("gemini") and run["argv"][1:] == ["-p", "hello"]
    base = wrappers.base_url()
    assert run["env"]["GOOGLE_GEMINI_BASE_URL"] == f"{base}/gemini"
    assert run["env"]["GOOGLE_VERTEX_BASE_URL"] == f"{base}/vertex"
    assert "GEMINI_DEFAULT_AUTH_TYPE" not in run["env"]


def test_latet_run_gemini_is_latet_gemini(launched, gemini_home):  # noqa: F811
    runs, _, _ = launched
    _choose(gemini_home, "gemini-api-key", legacy=True)
    result = CliRunner().invoke(app, ["run", "gemini", "--version"])
    assert result.exit_code == 0, result.output
    assert "latet run gemini is latet gemini" in result.output
    assert runs[0]["env"]["GOOGLE_GEMINI_BASE_URL"].endswith("/gemini")


@pytest.mark.parametrize("selected", ["oauth-personal", "compute-default-credentials", "cloud-shell"])
def test_gemini_cli_signed_in_with_google_does_not_start(launched, gemini_home, selected):  # noqa: F811
    runs, _, _ = launched
    _choose(gemini_home, selected)
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 2 and runs == []
    assert "signed in with Google" in result.output and "gemini-api-key" in result.output


def test_a_google_sign_in_asked_by_the_shell_does_not_start(launched, gemini_home, monkeypatch):  # noqa: F811
    runs, _, _ = launched
    monkeypatch.setenv("GOOGLE_GENAI_USE_GCA", "true")
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 2 and runs == []


@pytest.mark.parametrize(("vertex", "default"), [(None, "gemini-api-key"), ("true", "vertex-ai")])
def test_no_auth_type_chosen_starts_with_one_offered_and_says_so(launched, gemini_home, monkeypatch, vertex, default):  # noqa: F811
    runs, _, _ = launched
    _choose(gemini_home, None)
    if vertex:
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", vertex)
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 0, result.output
    assert runs[0]["env"]["GEMINI_DEFAULT_AUTH_TYPE"] == default
    assert "security.auth.selectedType" in result.output


def test_settings_that_cannot_be_read_count_as_no_choice(gemini_home):
    (gemini_home / ".gemini" / "settings.json").write_text("{not json", "utf-8")
    assert wrappers.gemini_auth() is None
    (gemini_home / ".gemini" / "settings.json").write_text('{"security": {"auth": {"selectedType": 3}}}', "utf-8")
    assert wrappers.gemini_auth() is None


def test_settings_with_comments_are_read(gemini_home):
    text = '{\n  // chosen once\n  "security": {"auth": {"selectedType": "vertex-ai"}}\n}\n'
    (gemini_home / ".gemini" / "settings.json").write_text(text, "utf-8")
    assert wrappers.gemini_auth() == "vertex-ai"


def test_the_route_says_each_case(gemini_home):
    _choose(gemini_home, "gemini-api-key")
    assert GeminiCli().route().problem is None and "latet gemini" in GeminiCli().route().summary
    _choose(gemini_home, "oauth-personal")
    assert "signed in with Google" in (GeminiCli().route().problem or "")
    _choose(gemini_home, None)
    route = GeminiCli().route()
    assert route.problem is None and any("selectedType" in note for note in route.notes)


def test_gemini_cli_is_not_one_of_the_agents_latet_edits():
    assert "gemini" not in agents.ALL and isinstance(agents.WRAPPED["gemini"], GeminiCli)
    assert GeminiCli().status() == "wrapper"


def test_the_doctor_lists_gemini_cli_when_it_is_installed(gemini_home, monkeypatch):
    _choose(gemini_home, "gemini-api-key")
    monkeypatch.setattr(agents, "ALL", {})
    monkeypatch.setattr(
        "latet.integrations.agents.shutil.which", lambda name: "C:/bin/gemini.cmd" if name == "gemini" else None
    )
    monkeypatch.setattr("latet.integrations.agents._version", lambda exe: "0.64.0")
    found = {c.title: c for c in checks.check_agents()}
    assert found["Gemini CLI"].status == checks.OK and "latet gemini" in found["Gemini CLI"].detail
    _choose(gemini_home, "oauth-personal")
    found = {c.title: c for c in checks.check_agents()}
    assert found["Gemini CLI route"].status == checks.WARN


def test_the_doctor_says_nothing_of_gemini_cli_when_it_is_not_installed(gemini_home, monkeypatch):
    monkeypatch.setattr(agents, "ALL", {})
    monkeypatch.setattr("latet.integrations.agents.shutil.which", lambda name: None)
    assert not [c for c in checks.check_agents() if c.title.startswith("Gemini CLI")]


def _write(path, selected: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"security": {"auth": {"selectedType": selected}}}), "utf-8")


@pytest.mark.parametrize("layer", ["workspace", "system"])
def test_a_google_sign_in_chosen_above_your_settings_does_not_start(
    launched,  # noqa: F811
    gemini_home,
    tmp_path,
    monkeypatch,
    layer,
):
    runs, _, _ = launched
    _choose(gemini_home, "gemini-api-key")
    if layer == "workspace":  # a project's own, which Gemini CLI reads above yours
        project = tmp_path / "project"
        _write(project / ".gemini" / "settings.json", "oauth-personal")
        monkeypatch.chdir(project)
    else:  # the machine's, which Gemini CLI reads above every other
        _write(tmp_path / "system" / "settings.json", "oauth-personal")
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 2 and runs == []
    assert "signed in with Google" in result.output


def test_the_auth_override_of_the_shell_counts_as_your_choice(launched, gemini_home, monkeypatch):  # noqa: F811
    runs, _, _ = launched
    monkeypatch.setenv("GEMINI_CLI_AUTH_OVERRIDE", "oauth-personal")
    _choose(gemini_home, "gemini-api-key")
    assert CliRunner().invoke(app, ["gemini"]).exit_code == 2 and runs == []
    monkeypatch.setenv("GEMINI_CLI_AUTH_OVERRIDE", "gemini-api-key")
    _choose(gemini_home, None)
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 0 and "GEMINI_DEFAULT_AUTH_TYPE" not in runs[0]["env"]


def test_the_machines_defaults_count_when_nothing_else_chooses(launched, gemini_home, tmp_path):  # noqa: F811
    runs, _, _ = launched
    _choose(gemini_home, None)
    _write(tmp_path / "system" / "system-defaults.json", "vertex-ai")
    result = CliRunner().invoke(app, ["gemini"])
    assert result.exit_code == 0 and "GEMINI_DEFAULT_AUTH_TYPE" not in runs[0]["env"]
    assert wrappers.gemini_choice()[0] == "vertex-ai"
