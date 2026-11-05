"""What 0.11 stops that 0.10 sent (P2-7, R-58): `latet doctor` and the daemon's first 0.11 start list it by category and
count, never a value: projects with Latet off (D5), text recognition not installed (M3), `--passthrough` in a service
unit or a launcher (D5), and the calls (method and route template) seen in the local history that no built-in profile
covers (D1). `latet doctor` also names each launcher whose sessions were launched by Latet without their key, with the
agent and its version (R-120)."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from latet.config import Config, ProjectEntry, data_dir, save_config
from latet.doctor import checks, upgrade
from latet.storage.exchanges import ExchangeStore

from ..conftest import ROOT_KEY


def _history(rows: list[dict]) -> None:
    """A local history holding ``rows`` as an older Latet wrote them: their summaries as they were (a 0.10 row's raw
    path, its agent's version), each with its body's size."""
    data_dir().mkdir(parents=True, exist_ok=True)
    ExchangeStore(data_dir() / "exchanges.db", ROOT_KEY).close()
    db = sqlite3.connect(data_dir() / "exchanges.db")
    try:
        for n, row in enumerate(rows):
            summary = {"id": f"ex_{n}", "ts": 1_800_000_000.0 + n, "session": "s", "flags": []} | row
            db.execute(
                "INSERT INTO exchanges (id, ts, session, agent, flags, summary, body_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    summary["id"],
                    summary["ts"],
                    "s",
                    summary.get("agent", "other"),
                    " " + " ".join(summary["flags"]) + " ",
                    json.dumps(summary),
                    row.get("body_bytes", 0),
                ),
            )
        db.commit()
    finally:
        db.close()


def _text(findings) -> str:
    return "\n".join(line for finding in findings for line in (finding.title, finding.detail, *finding.lines))


@pytest.fixture
def quiet(computer, monkeypatch):
    """Text recognition installed, no start entry: each test adds the one thing it looks at."""
    monkeypatch.setattr(upgrade, "_reads_images", lambda config: True)
    monkeypatch.setattr(upgrade, "start_entries", lambda: [])
    return computer


def test_projects_with_latet_off_are_counted_never_named_by_their_folder(quiet):
    config = Config(
        projects=[
            ProjectEntry(path=str(quiet / "secret-client-acme"), enabled=False),
            ProjectEntry(path=str(quiet / "billing"), enabled=False),
            ProjectEntry(path=str(quiet / "shop")),
        ]
    )
    [finding] = upgrade.findings(config)
    assert finding.category == "projects-off" and finding.count == 2
    assert "refused" in finding.detail and "latet project show" in finding.detail
    assert "acme" not in _text([finding]) and str(quiet) not in _text([finding])


@pytest.mark.parametrize(("images", "found"), [("allow", True), ("mask", True), ("block", False)])
def test_text_recognition_not_installed_stops_requests_that_carry_an_image(quiet, monkeypatch, images, found):
    monkeypatch.setattr(upgrade, "_reads_images", lambda config: False)
    got = upgrade.findings(Config(images=images))
    assert [f.category for f in got] == (["text-recognition"] if found else [])
    if found:
        assert "latet ocr install" in got[0].detail and "image" in got[0].detail


def test_text_recognition_off_counts_as_not_installed(computer, monkeypatch):
    monkeypatch.setattr(upgrade, "start_entries", lambda: [])
    from latet.vision import models

    monkeypatch.setattr(models, "runtime_available", lambda: True)
    monkeypatch.setattr(models, "installed", lambda folder: True)
    assert upgrade._reads_images(Config()) is True
    assert upgrade._reads_images(Config(ocr="off")) is False
    monkeypatch.setattr(models, "installed", lambda folder: False)
    assert upgrade._reads_images(Config()) is False


def test_passthrough_in_a_start_entry_is_named_by_where_it_is(quiet, monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "start_entries",
        lambda: [
            ("~/.config/systemd/user/latet.service", "ExecStart=/opt/latet serve --passthrough --port 5283"),
            ("~/.config/systemd/user/other.service", "ExecStart=/usr/bin/other --passthrough"),
            ("HKCU Run: Latet", '"C:\\latet\\pythonw.exe" -m latet serve --log-level info'),
        ],
    )
    [finding] = upgrade.findings(Config())
    assert finding.category == "passthrough" and finding.count == 1
    assert finding.lines == ("~/.config/systemd/user/latet.service",)
    assert "--port" not in _text([finding]) and "exits" in finding.detail


def test_the_start_entries_are_read_from_the_systemd_user_units(computer):
    units = computer / "home" / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "latet.service").write_text("[Service]\nExecStart=/x/latet serve --passthrough\n", encoding="utf-8")
    (units / "notes.txt").write_text("latet serve --passthrough", encoding="utf-8")
    found = upgrade.start_entries("linux")
    # By its kind and its file's name alone: never the folder, which names the user (review Minor 2).
    assert [name for name, _ in found] == ["systemd user unit latet.service"]
    assert all(str(computer) not in name for name, _ in found)


def test_calls_no_built_in_profile_covers_are_listed_by_method_and_route_template(quiet):
    _history(
        [
            {"path": "/anthropic/v1/files", "flags": ["unknown-route"], "body_bytes": 4096},
            {"path": "/anthropic/v1/files", "flags": ["unknown-route"], "body_bytes": 512},
            {"path": "/openai/v1/files/file-Zq81secret/content", "flags": ["unknown-route"]},
            {"path": "/openai/v1/batches", "flags": ["unknown-route"], "body_bytes": 90},
            # Covered in 0.11 by D1's built-in profiles, or the main calls: not listed.
            {"path": "/anthropic/v1/models", "flags": ["unknown-route"]},
            {"path": "/openai/v1/models/gpt-5.1", "flags": ["unknown-route"]},
            {"path": "/anthropic/v1/messages", "protocol": "anthropic", "body_bytes": 800},
            {"path": "/bedrock/eu-west-1/model/anthropic.claude-sonnet-4-5/invoke", "body_bytes": 800},
            # 0.11's own rows: a route template or an area, already decided under 0.11.
            {"path": "/{destination}/v1/messages", "body_bytes": 800},
            {"path": "/gateway", "flags": ["blocked"], "block_reason": "envelope-route"},
        ]
    )
    [finding] = upgrade.findings(Config())
    assert finding.category == "ancillary" and finding.count == 3
    assert "naming its route" not in finding.detail  # its refusal names a placeholder, never the route (Minor 1)
    assert finding.lines == (
        "POST /{destination}/v1/files (2 seen)",
        "POST /{destination}/v1/batches (1 seen)",
        "GET /{destination}/v1/files/{…}/content (1 seen)",
    )
    assert "Zq81secret" not in _text([finding]) and "openai" not in "".join(finding.lines)


def test_launchers_without_their_key_are_named_with_the_agent_and_its_version(quiet):
    _history(
        [
            {"launch": "launched-key-missing", "agent": "claude-code", "agent_version": "2.1.312"},
            {"launch": "launched-key-missing", "agent": "claude-code", "agent_version": "2.1.312"},
            {"launch": "launched-key-missing", "agent": "codex", "agent_version": "0.114.0"},
            {"launch": "launched-key-missing", "agent": "codex", "agent_version": "a b c"},
            {"launch": "launched", "agent": "codex", "agent_version": "0.162.0"},
            {"launch": "claimed", "agent": "opencode", "agent_version": "1.2.3"},
        ]
    )
    [launch] = [f for f in upgrade.launches() if f.category == "launched-key-missing"]
    assert launch.category == "launched-key-missing" and launch.count == 4
    assert launch.lines == ("Claude Code 2.1.312 (2 requests)", "Codex 0.114.0 (1 request)", "Codex (1 request)")


def test_doctor_lists_what_0_11_stops_and_says_so_when_nothing_is_seen(quiet):
    save_config(Config(projects=[ProjectEntry(path=str(quiet / "a"), enabled=False)]))
    found = checks.check_upgrade()
    assert [c.status for c in found] == [checks.WARN]
    assert "Latet off" in found[0].title and "1 project" in found[0].detail
    save_config(Config())
    [ok] = checks.check_upgrade()
    assert ok.status == checks.OK and "nothing" in ok.detail


def test_doctor_runs_the_upgrade_checks_on_a_computer():
    assert "check_upgrade" in checks.COMPUTER_GROUPS and callable(checks.check_upgrade)


def test_the_first_0_11_start_says_it_once_in_the_log(quiet, caplog):
    config = Config(projects=[ProjectEntry(path=str(quiet / "client"), enabled=False)])
    with caplog.at_level(logging.WARNING, logger="latet.doctor.upgrade"):
        assert upgrade.first_start(config, data_dir()) is True
        assert upgrade.first_start(config, data_dir()) is False
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2 and "what 0.11 stops" in said[0] and "1 project" in said[1]
    assert str(quiet) not in " ".join(said)
    assert (data_dir() / upgrade.MARKER).exists()


def test_the_first_start_with_nothing_to_stop_says_nothing_and_remembers_it(quiet, caplog):
    with caplog.at_level(logging.INFO, logger="latet.doctor.upgrade"):
        assert upgrade.first_start(Config(), data_dir()) is True
    assert caplog.records == [] and (data_dir() / upgrade.MARKER).exists()


def test_the_daemon_and_latet_serve_run_the_first_start_check(tmp_path, monkeypatch):
    """`latet serve` and the daemon ask ``first_start`` once their runtime is made, beside the server (a thread: the
    start waits for nothing), with the settings in use and the data folder."""
    import socket
    import threading
    from types import SimpleNamespace

    import uvicorn

    from latet.cli import main as cli_main
    from latet.server.logs import saved
    from latet.service import ports

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free = sock.getsockname()[1]
    sock.close()
    asked, done = [], threading.Event()

    def first(config, data):
        asked.append((type(config).__name__, data))
        done.set()
        return True

    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LATET_HOST", raising=False)
    monkeypatch.setattr(upgrade, "first_start", first)
    monkeypatch.setattr(cli_main, "uvicorn_server", lambda *a, **k: SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: pytest.fail("a server of its own was run"))
    monkeypatch.setattr(ports, "probe", lambda port: ports.PortState(port, True, "free"))
    with saved():
        cli_main.run_server(port=free)
    assert done.wait(10) and asked == [("Config", tmp_path)]


def test_launchers_whose_carrier_is_not_verified_are_named_by_agent_and_version(quiet):
    """Root's ruling on P2-7's concern 4: the static carrier table (which launcher passes no key, R-119) read with the
    history's rows of that agent and version decided as claimed. The sessions stay claimed; nothing else is named."""
    _history(
        [
            {"launch": "claimed", "agent": "codex", "agent_version": "0.114.0", "upstream": "custom"},
            {"launch": "claimed", "agent": "codex", "agent_version": "0.114.0", "upstream": "custom"},
            {"launch": "claimed", "agent": "codex", "agent_version": "0.162.0", "upstream": "chatgpt"},
            {"launch": "claimed", "agent": "gemini-cli", "agent_version": "0.18.0", "upstream": "gemini"},
            {"launch": "claimed", "agent": "opencode", "agent_version": "1.2.3", "upstream": "anthropic"},
            # A verified carrier (Codex on a provider of its own, Gemini CLI from 0.19.0, OpenCode from 1.19.5, P2-8a's
            # pins in latet.launch_carriers), a launch with its key, an agent the table says nothing of: not named.
            {"launch": "claimed", "agent": "codex", "agent_version": "0.162.0", "upstream": "custom"},
            {"launch": "claimed", "agent": "gemini-cli", "agent_version": "0.21.0", "upstream": "gemini"},
            {"launch": "claimed", "agent": "opencode", "agent_version": "1.19.5", "upstream": "anthropic"},
            {"launch": "launched", "agent": "codex", "agent_version": "0.114.0", "upstream": "custom"},
            {"launch": "claimed", "agent": "claude-code", "agent_version": "2.1.312", "upstream": "anthropic"},
            {"launch": "claimed", "agent": "other", "agent_version": None, "upstream": "openai"},
        ]
    )
    found = {f.category: f for f in upgrade.launches()}
    carriers = found["carrier-unverified"]
    assert carriers.count == 5
    assert carriers.lines == (
        "Codex 0.114.0: Codex older than 0.115 has no way to carry a launch key (2 requests)",
        "Codex 0.162.0: Codex's built-in providers (its OpenAI provider, ChatGPT sign-in included) have no verified "
        "way to carry a launch key (1 request)",
        "Gemini CLI 0.18.0: Gemini CLI older than 0.19 has no way to carry a launch key (1 request)",
        "OpenCode 1.2.3: OpenCode older than 1.19.5 has no verified way to carry a launch key (1 request)",
    )
    assert "claimed" in carriers.detail
    # Agents set up by hand look the same in the history: never said to be all launched by Latet (Minor 9).
    assert "by hand" in carriers.detail
    [check] = [c for c in checks.check_upgrade() if "carrier" in c.title]
    assert check.status == checks.WARN and "Codex 0.114.0" in check.detail


def test_a_launchd_agent_is_named_by_its_file_alone(computer):
    agents = computer / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "dev.latet.daemon.plist").write_text("<string>latet</string><string>serve</string>", encoding="utf-8")
    found = upgrade.start_entries("darwin")
    assert [name for name, _ in found] == ["launchd agent dev.latet.daemon.plist"]
