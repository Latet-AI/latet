"""Start at login on macOS: a launchd agent that starts the daemon again when it fails, as systemd's Restart=on-failure
does, never when it exits by itself (latet stop, a restore under way, a Latet already running there), and that
`latet service install` hands launchd at once. No test asks the real launchctl."""

import os
import plistlib
import sys
from pathlib import Path

import pytest

from latet.service import autostart

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the launchd agent is made on macOS")

COMMAND = ["/opt/latet/bin/latet-daemon"]

# The agent an earlier Latet made: KeepAlive true starts the daemon again however it exits, every 10 seconds.
EARLIER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    '<plist version="1.0"><dict><key>Label</key><string>dev.latet.daemon</string>'
    f"<key>ProgramArguments</key><array><string>{COMMAND[0]}</string></array>"
    "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n"
)


@pytest.fixture
def launchctl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_command", lambda: COMMAND)
    asked: list[list[str]] = []

    def run(args, **kwargs):
        assert args[0] == "launchctl"
        asked.append(args[1:])

    monkeypatch.setattr(autostart.subprocess, "run", run)
    return asked


def _agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "dev.latet.daemon.plist"


def test_launchd_starts_the_daemon_again_only_when_it_failed(launchctl):
    assert autostart.install() == str(_agent())
    agent = plistlib.loads(_agent().read_bytes())
    assert agent["Label"] == "dev.latet.daemon" and agent["ProgramArguments"] == COMMAND
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert launchctl == [["bootstrap", f"gui/{os.getuid()}", str(_agent())]]


def test_a_service_install_replaces_the_agent_launchd_holds(launchctl):
    """launchd keeps the agent it loaded until it is booted out: bootstrap alone left the earlier one in effect until
    the next login."""
    _agent().parent.mkdir(parents=True)
    _agent().write_text(EARLIER)
    autostart.install()
    domain = f"gui/{os.getuid()}"
    assert launchctl == [["bootout", domain, str(_agent())], ["bootstrap", domain, str(_agent())]]
    assert plistlib.loads(_agent().read_bytes())["KeepAlive"] == {"SuccessfulExit": False}


def test_a_service_install_that_changes_nothing_leaves_the_daemon_running(launchctl):
    """Booting the agent out stops the daemon launchd started: only an agent that changes is."""
    autostart.install()
    launchctl.clear()
    autostart.install()
    assert launchctl == [["bootstrap", f"gui/{os.getuid()}", str(_agent())]]
