"""Changes to the settings that lower protection wait for you: an agent's command, a hand-edited
config.yaml, a daemon started over one. What adds protection applies at once. `global-install` is one
too: the daemon takes the settings it saved before an agent's destination is added to them."""

import base64
import json

import pytest
import yaml
from starlette.testclient import TestClient
from typer.testing import CliRunner

from latet.config import Config, IdentityConfig, Profile, load_config, save_config
from latet.gateway.app import create_app
from latet.runtime import REVIEWED_FILE, make_runtime

from ..conftest import ROOT_KEY

pytestmark = pytest.mark.e2e

API = "/_latet/api/v1"
UI = {"x-latet-ui": "1"}
COMPANY = "https://llm.example.com"  # the address a company's Claude Code settings name


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_ROOT_KEY", base64.b64encode(ROOT_KEY).decode())
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("HERMES_SESSION_ID", "")  # the commands below run in an agent
    save_config(Config(identity=IdentityConfig(enabled=False)), tmp_path / "config.yaml")
    return tmp_path


@pytest.fixture
def daemon(files, monkeypatch):
    """A daemon, with the CLI's own calls going to it."""
    from latet.cli import admin

    rt = make_runtime(data=files / "data", config_file=files / "config.yaml")
    with TestClient(create_app(rt.gateway), base_url="http://127.0.0.1:5283") as client:

        def call(method, path, body=None, timeout=60):
            headers = {"x-latet-token": rt.gateway.bridge_token, "x-latet-command": "1"}
            headers["x-latet-agent"] = "hermes"
            r = client.request(method, path, json=None if method == "GET" else (body or {}), headers=headers)
            answer = r.json()
            return answer | {"status": r.status_code} if r.status_code >= 400 else answer

        monkeypatch.setattr(admin, "_admin", call)
        code = client.post("/_latet/admin/login-code", headers={"x-latet-token": rt.gateway.bridge_token})
        client.get(f"/_latet/ui/login?code={code.json()['code']}", follow_redirects=False)
        yield rt, client
    rt.close()


@pytest.fixture
def claude(files, daemon, monkeypatch):
    """Claude Code's folder, its settings naming a company's address, for `latet global-install` to route with the
    daemon running: the destination it registers reaches that daemon too. None of your files is read or written,
    no agent runs, no port is probed."""
    from latet import wrappers
    from latet.integrations import agents, routes
    from latet.integrations.base import AgentInfo
    from latet.service import daemon as process
    from latet.service import ports

    rt, client = daemon
    folder = files / "claude"
    folder.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    monkeypatch.setenv("CODEX_HOME", str(files / "codex"))
    for var in routes.ROUTE_VARS:  # where the agents go is read from the shell too: none of this one's
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(routes, "MANAGED_SETTINGS", (files / "managed-settings.json",))
    monkeypatch.setattr(agents, "ALL", {"claude": agents.ClaudeCode()})
    monkeypatch.setattr(agents.ClaudeCode, "detect", lambda self: AgentInfo("claude", "Claude Code", True, "2.1"))
    monkeypatch.setattr(agents.ClaudeCode, "_claude", lambda self, *args: {"outcome": "ok"})  # no `claude plugin`

    def to_the_daemon(path, body, config=None, method="POST"):  # an agent's address registered with the daemon
        r = client.request(method, path, json=body, headers={"x-latet-token": rt.gateway.bridge_token})
        if r.status_code >= 400:
            raise wrappers.DaemonError(str(r.json().get("error")), r.status_code)
        return r.json()

    running = process.Status(True, port=5283)
    monkeypatch.setattr(wrappers, "_daemon_call", to_the_daemon)
    monkeypatch.setattr(process, "status", lambda port=None: running)
    monkeypatch.setattr(process, "start", lambda: running)
    monkeypatch.setattr(process, "stop", lambda: pytest.fail("the daemon must not be stopped"))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "latet"))
    (folder / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": COMPANY}}), "utf-8")
    return folder


def _cli(*args, input=None):
    import latet.cli.admin
    import latet.cli.llm  # noqa: F401 - register the commands
    from latet.cli.main import app

    return CliRunner().invoke(app, list(args), input=input)


def test_what_adds_protection_applies_at_once(daemon):
    rt, _ = daemon
    result = _cli("config", "set", "profile", "max")
    assert result.exit_code == 0, result.output
    assert rt.config.profile == "max" and load_config().profile == "max"
    assert _cli("config", "add", "terms", "{value: Project Falcon, type: PROJECT}").exit_code == 0
    assert [t.value for t in rt.config.terms] == ["Project Falcon"]
    assert "Project Falcon" in _cli("config", "get", "terms").output


def test_a_term_of_signs_alone_is_refused_and_said_why(daemon):
    rt, _ = daemon
    result = _cli("config", "add", "terms", "{value: '--'}")
    assert result.exit_code == 2 and "terms.0.value: a term needs a letter or a digit" in result.output
    assert rt.config.terms == [] and load_config().terms == []


def test_what_lowers_it_waits_for_you_in_the_dashboard(daemon, files):
    rt, client = daemon
    result = _cli("config", "add", "allow_values", "ACME")
    assert result.exit_code == 3, result.output
    assert "values never to mask: 1 added" in result.output
    assert "in the dashboard (`latet dashboard`, Approvals)" in result.output  # how to open it, and where
    assert rt.config.allow_values == []  # not applied
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["agent"] == "hermes" and change["source"] == "command" and change["keys"] == ["allow_values"]
    # You approve it: it applies, and becomes what later changes are measured against.
    r = client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert r.json() == {"ok": True, "state": "approved"}
    assert rt.config.allow_values == ["ACME"]
    assert json.loads((files / "data" / REVIEWED_FILE).read_text("utf-8"))["allow_values"] == ["ACME"]


def test_under_max_an_agent_turning_strict_data_mode_off_waits_for_you(daemon):
    rt, client = daemon
    assert _cli("config", "set", "profile", "max").exit_code == 0  # strict data mode comes with it, at once
    assert rt.config.data is None and rt.config.data_mode() == "strict"
    result = _cli("config", "set", "data", "normal")
    assert result.exit_code == 3, result.output
    assert "strict data mode: turned off" in result.output
    assert rt.config.data_mode() == "strict"  # not applied
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["keys"] == ["data"]


def test_the_reviewer_s_guidance_from_an_agent_waits_and_you_read_it(daemon, files):
    rt, client = daemon
    result = _cli("llm", "guidance", "-", input="Never report ACME.\r\n")
    assert result.exit_code == 3, result.output
    assert "its guidance changed" in result.output and rt.config.llm_review.guidance is None
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["guidance"] == [{"where": None, "before": None, "after": "Never report ACME."}]
    client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert rt.config.llm_review.guidance == "Never report ACME."
    assert _cli("llm", "guidance").output.strip() == "Never report ACME."
    # A project's own comes after it.
    assert _cli("llm", "guidance", "PRJ- codes are projects.", "--project", str(files)).exit_code == 3
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert change["guidance"] == [{"where": files.name, "before": None, "after": "PRJ- codes are projects."}]
    client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    prompt = _cli("llm", "prompt", "--project", str(files)).output
    assert prompt.rstrip().endswith("Never report ACME.\n\nPRJ- codes are projects.")
    assert _cli("llm", "prompt").output.rstrip().endswith("Never report ACME.")
    too_long = _cli("llm", "guidance", "x" * 2001)
    assert too_long.exit_code == 2 and "at most 2000" in too_long.output
    assert _cli("llm", "guidance", "--clear").exit_code == 3


def test_what_you_reject_is_undone_in_the_file(daemon, files):
    rt, client = daemon
    assert _cli("project", "off", str(files)).exit_code == 3
    assert load_config().projects  # the command saved it...
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert any("Latet turned off" in r for r in change["loosened"])
    client.post(f"{API}/changes/{change['id']}", json={"action": "reject"}, headers=UI)
    assert load_config().projects == [] and rt.config.projects == []  # ...and it is undone
    gone = client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert gone.status_code == 409


def test_a_hand_edited_file_waits_too_and_a_stale_change_is_dropped(daemon, files):
    rt, client = daemon
    data = yaml.safe_load((files / "config.yaml").read_text("utf-8")) or {}
    (files / "config.yaml").write_text(yaml.safe_dump(data | {"images": "allow", "ner": "off"}), "utf-8")
    assert _cli("reload").exit_code == 3
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert rt.config.ner == "auto"
    # The settings change meanwhile (you, in the dashboard): approving it would undo that.
    rt.apply_config(rt.config.model_copy(update={"profile": Profile.MAX}), save=False)
    r = client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert r.status_code == 409 and rt.config.ner == "auto"


def test_latet_reload_says_each_allow_pattern_the_daemon_left_out_and_applies_the_rest(daemon, files):
    """A pattern RE2 cannot compile is left out where config.yaml is loaded (more is masked), and the reload's answer
    names its place, which `latet reload` says, never the pattern."""
    rt, _ = daemon
    (files / "config.yaml").write_text(
        "identity: {enabled: false}\nterms: [{value: Mercury Labs}]\nallow_patterns: ['(emily\\.carter']\n", "utf-8"
    )
    result = _cli("reload")
    assert result.exit_code == 0, result.output
    assert "config.yaml: allow_patterns entry 1 is not a valid RE2 pattern and is left out" in result.output
    assert "Rules reloaded." in result.output and "emily" not in result.output
    assert [t.value for t in rt.config.terms] == ["Mercury Labs"] and rt.config.allow_patterns == []


def test_latet_reload_says_why_the_daemon_refused_it_in_words_that_hold_no_value(daemon, files):
    """A config.yaml that is not valid settings is refused by the daemon, in words that name places: they are said, and
    never that the daemon is not running."""
    (files / "config.yaml").write_text(
        "retention_days: emily.carter@acme.com\npolicies: {Falcon Holdings: hide}\n", "utf-8"
    )
    result = _cli("reload")
    assert result.exit_code == 1 and "not running" not in result.output
    said = result.output.replace("\n", " ")
    assert "The daemon did not reload: cannot read the configuration: " in said and "is not valid settings: " in said
    assert "retention_days: " in said and "policies.*: " in said
    assert "emily" not in said and "Falcon" not in said


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ({"error": "unauthorized", "status": 401}, "another data directory's"),  # its token is not this one's
        ({"error": "HTTP 500", "status": 500}, "it answered 500"),
        ({"error": "emily.carter@acme.com is not here", "status": 404}, "it answered 404"),  # words of who knows what
    ],
    ids=["another-daemon", "failed", "not-latet"],
)
def test_a_refused_reload_is_said_by_its_status_where_its_words_could_hold_anything(files, monkeypatch, answer, said):
    from latet.cli import admin

    monkeypatch.setattr(admin, "_admin", lambda *args, **kwargs: dict(answer))
    for args in (["reload"], ["config", "add", "terms", "{value: Mercury Labs}"]):
        result = _cli(*args)
        assert result.exit_code == 1, (args, result.output)
        assert "The daemon did not reload" in result.output and said in result.output.replace("\n", " ")
        assert "not running" not in result.output and "emily" not in result.output


def test_a_daemon_started_over_a_lowered_file_keeps_what_you_approved(files):
    rt = make_runtime(data=files / "data", config_file=files / "config.yaml")
    rt.close()
    data = yaml.safe_load((files / "config.yaml").read_text("utf-8")) or {}
    (files / "config.yaml").write_text(yaml.safe_dump(data | {"profile": "secrets"}), "utf-8")
    rt = make_runtime(data=files / "data", config_file=files / "config.yaml")
    try:
        assert rt.config.profile == "balanced"
        [change] = rt.mediator.approvals.pending_changes()
        assert change["source"] == "start" and "profile: from balanced to secrets" in change["loosened"]
    finally:
        rt.close()


def test_commands_that_take_protection_away_are_yours(files):
    for args in (["global-uninstall"], ["agents", "revert"], ["service", "uninstall"]):
        result = _cli(*args)
        assert result.exit_code == 2 and "run it yourself" in result.output, args


def test_a_single_value_prints_as_it_is(files):
    assert _cli("config", "get", "profile").output.strip() == "balanced"
    assert _cli("config", "get", "live_review.enabled").output.strip() == "false"


def test_a_colleague_s_rules_add_at_once_and_the_rest_waits(daemon, files):
    """Without --all what adds protection applies at once. With --all the import is one change, saved and reloaded
    once (ER.8): it waits for your approval whole, what adds protection with what lowers it, and nothing of it is said
    added meanwhile; rejected, config.yaml is as it was."""
    rt, client = daemon
    rules = files / "rules.yaml"
    rules.write_text("latet_rules: 1\nterms: [{value: Project Falcon, type: PROJECT}]\nallow_values: [ACME]\n", "utf-8")
    result = _cli("rules", "import", str(rules), "--all")
    assert result.exit_code == 3, result.output
    assert "Added:" not in result.output and "Would add:" in result.output
    assert rt.config.terms == [] and rt.config.allow_values == []  # nothing of it applies while it waits
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert set(change["keys"]) == {"terms", "allow_values"}  # the whole import waits
    client.post(f"{API}/changes/{change['id']}", json={"action": "reject"}, headers=UI)
    assert load_config().terms == [] and load_config().allow_values == []
    result = _cli("rules", "import", str(rules))  # without --all: what adds protection, at once
    assert result.exit_code == 0 and "Rules reloaded." in result.output, result.output
    assert [t.value for t in rt.config.terms] == ["Project Falcon"] and rt.config.allow_values == []


def test_global_install_keeps_its_choices_when_it_adds_a_destination(daemon, claude):
    """`global-install` saves the profile and the countries, then routes Claude Code, whose settings name a company's
    address: the running daemon adds that destination. It used to add it to the settings it had from before and save
    those over config.yaml, and the choices were gone. It now takes what was saved first."""
    rt, _ = daemon
    result = _cli(
        "global-install", "--yes", "--no-autostart", "--agents", "claude", "--profile", "max", "--id-regions", "it"
    )
    assert result.exit_code == 0, result.output
    for config in (rt.config, load_config()):  # the settings in use, and the file
        assert config.profile == "max" and config.id_regions == ["IT"]
        assert [(d.name, d.url, d.agent) for d in config.upstreams] == [("llm-example-com", COMPANY, "claude")]
    env = json.loads((claude / "settings.json").read_text("utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5283/llm-example-com"  # Claude Code goes through it


def test_global_install_again_asks_the_daemon_twice_and_keeps_one_destination(daemon, claude):
    """A second `global-install` asks the daemon for the address before it undoes the first and again after:
    the address is known by then, so it is the same destination, and the agent stays routed."""
    rt, _ = daemon
    for _ in range(2):
        result = _cli("global-install", "--yes", "--no-autostart", "--agents", "claude")
        assert result.exit_code == 0, result.output
        assert "left as it is" not in result.output
    for config in (rt.config, load_config()):
        assert [(d.name, d.url, d.agent) for d in config.upstreams] == [("llm-example-com", COMPANY, "claude")]
    env = json.loads((claude / "settings.json").read_text("utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:5283/llm-example-com"


def test_global_install_lowering_protection_says_so_once_and_what_waits_holds_the_destination(daemon, claude):
    """The profile it saves lowers protection, so it waits for you; the destination is added meanwhile, to the
    settings in use and to what waits. It is said once, at the end (the reload that takes the settings first does
    not say it), and approving keeps the destination."""
    rt, client = daemon
    result = _cli("global-install", "--yes", "--no-autostart", "--agents", "claude", "--profile", "secrets")
    assert result.exit_code == 0, result.output
    assert result.output.count("This change lowers protection:") == 1
    [change] = client.get(f"{API}/changes").json()["changes"]
    assert "profile: from balanced to secrets" in change["loosened"]
    assert rt.config.profile == "balanced" and [d.name for d in rt.config.upstreams] == ["llm-example-com"]
    assert load_config().profile == "secrets" and [d.name for d in load_config().upstreams] == ["llm-example-com"]
    r = client.post(f"{API}/changes/{change['id']}", json={"action": "approve"}, headers=UI)
    assert r.json() == {"ok": True, "state": "approved"}
    for config in (rt.config, load_config()):
        assert config.profile == "secrets" and [d.name for d in config.upstreams] == ["llm-example-com"]
