"""`latet project off` in 0.11 (D5, R-70): a project where Latet is off has its agents' requests refused, never sent
unmasked. Its help says so, and the command warns first, before anything changes, with how an agent there reaches its
provider directly; `latet project show` says it too."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers the commands
from latet.cli.main import app


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config" / "config.yaml"))
    from latet.cli import admin

    monkeypatch.setattr(admin, "_reload_answer", lambda: None)
    root = tmp_path / "shop"
    (root / ".git").mkdir(parents=True)
    return root


def _cli(*args: str):
    return CliRunner().invoke(app, list(args))


def test_the_help_says_the_agents_requests_are_refused():
    shown = " ".join(_cli("project", "off", "--help").output.split())
    assert "refused" in shown and "nothing is masked or blocked" not in shown
    listed = " ".join(_cli("project", "--help").output.split())
    assert "nothing is masked or blocked" not in listed


def test_it_warns_first_with_how_to_reach_the_provider_directly(isolated, monkeypatch):
    from latet.cli import rules

    order: list[str] = []

    real = rules._save_and_reload

    def save(config):
        order.append("saved")
        return real(config)

    monkeypatch.setattr(rules, "_save_and_reload", save)
    import typer

    said = typer.secho

    def secho(message="", *args, **kwargs):
        order.append(str(message))
        return said(message, *args, **kwargs)

    monkeypatch.setattr(typer, "secho", secho)
    result = _cli("project", "off", str(isolated))
    assert result.exit_code == 0, result.output
    warning = next(n for n, line in enumerate(order) if "refused" in line)
    assert warning < order.index("saved"), "the warning comes before anything changes"
    text = " ".join(result.output.split())
    assert "refused" in text and "directly" in text and "without Latet" in text
    assert "nothing is masked or blocked" not in text
    assert "latet project on" in text


def test_show_says_the_requests_there_are_refused(isolated):
    assert _cli("project", "off", str(isolated)).exit_code == 0
    shown = " ".join(_cli("project", "show", str(isolated)).output.split())
    assert "OFF" in shown and "refused" in shown and "nothing is masked or blocked" not in shown
