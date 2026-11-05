"""What Latet tells an agent, `latet doctor` and the settings' descriptions offer only what exists in 0.11 (P2-7, the
docs agent's findings). No exception can name a route, an image or a document (P2-6 F-4 (a)): a refused route is said
to be unsupported, never to wait for an exception. Every command a finding names exists. The text-in-images settings
say what 0.11 does: a request that carries an image no one reads is refused, never sent as it is."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands
from latet.cli.main import app
from latet.config import Config, ProjectEntry
from latet.doctor import upgrade
from latet.gateway.coverage import EnvelopeRefused


def test_a_refused_route_is_unsupported_and_offers_no_exception():
    said = EnvelopeRefused("envelope-route", template="/{destination}/{unsupported-route}").agent_message()
    assert "/{destination}/{unsupported-route}" in said
    assert "exception" not in said
    assert "not support" in said and "agent" in said


def test_an_expired_exception_is_still_said_to_be_renewable():
    """Renewal is a new approval, which exists: that refusal keeps saying so."""
    said = EnvelopeRefused("envelope-exception-expired", expired=("x0123456789", "2026-11-01")).agent_message()
    assert "x0123456789" in said and "2026-11-01" in said and "renew" in said


@pytest.fixture
def history(computer, monkeypatch):
    monkeypatch.setattr(upgrade, "_reads_images", lambda config: False)
    monkeypatch.setattr(
        upgrade, "start_entries", lambda: [("~/.config/systemd/user/latet.service", "latet serve --passthrough")]
    )
    monkeypatch.setattr(upgrade, "_rows", lambda data: [({"path": "/anthropic/v1/files", "flags": []}, 300)])
    return Config(projects=[ProjectEntry(path=str(computer / "shop"), enabled=False)])


def test_the_uncovered_calls_finding_offers_no_exception(history):
    [finding] = [f for f in upgrade.findings(history) if f.category == "ancillary"]
    assert "exception" not in finding.detail
    assert "not support" in finding.detail


def test_every_command_a_finding_names_exists(history):
    said = " ".join(f.said() for f in upgrade.findings(history))
    commands = set(re.findall(r"`(latet [a-z][a-z -]*?)`", said))
    assert commands, "the findings name commands"
    assert "latet project list" not in said
    for command in commands:
        words = [word for word in command.split()[1:] if not word.startswith("-")]
        result = CliRunner().invoke(app, [*words, "--help"])
        assert result.exit_code == 0, f"`{command}` does not exist: {result.output[:200]}"


@pytest.mark.parametrize("field", ["ocr", "images"])
def test_the_text_in_images_settings_say_what_0_11_does(field):
    described = Config.model_fields[field].description or ""
    assert "sent as they are" not in described and "flagged" not in described
    assert "refused" in described


def test_global_install_says_an_unread_image_is_refused():
    from pathlib import Path

    import latet.cli.admin as admin

    source = Path(admin.__file__).read_text(encoding="utf-8")
    assert "images are sent as they are" not in source
