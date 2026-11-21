"""``latet global-uninstall`` and the ports Latet reserved (notes/windows-port-reservation-design.md, "Everything else
that changes")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # noqa: F401 - registers global-uninstall
from latet.cli.main import app
from latet.integrations import base
from latet.service import autostart, daemon, reservation


class Reserved:
    def __init__(self) -> None:
        self.record: list[int] = []
        self.removed: list[list[int]] = []
        self.console = True


@pytest.fixture
def reserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Reserved:
    from latet.cli import reserve

    found = Reserved()
    for name, sub in (("LATET_DATA_DIR", "data"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex"),
                      ("XDG_CONFIG_HOME", "xdg")):  # fmt: skip
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("LATET_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: None)
    monkeypatch.setattr(base, "records_preflight", lambda *, write=False: base.load_records())
    monkeypatch.setattr("latet.integrations.agents.ALL", {})  # no agent to revert: this is about the ports
    monkeypatch.setattr(autostart, "uninstall", lambda: "not installed")
    monkeypatch.setattr(daemon, "stop", lambda timeout=10.0: True)
    monkeypatch.setattr(reserve, "_console", lambda: found.console)
    monkeypatch.setattr(reservation, "recorded", lambda: list(found.record))
    monkeypatch.setattr(reservation, "read_record", lambda: list(found.record), raising=False)

    def remove(ports: Any) -> list[int]:
        found.removed.append(list(ports))
        return list(ports)

    monkeypatch.setattr(reservation, "remove", remove)
    return found


def uninstall(input: str | None = None) -> Any:
    return CliRunner().invoke(app, ["global-uninstall"], input=input)


def test_global_uninstall_offers_to_take_latets_reservations_away(reserved: Reserved) -> None:
    reserved.record = [5283]
    result = uninstall("y\n")
    assert result.exit_code == 0, result.output
    assert reserved.removed == [[5283]]
    assert "Port 5283 is no longer reserved." in result.output

    reserved.removed.clear()
    result = uninstall("n\n")
    assert result.exit_code == 0, result.output
    assert reserved.removed == []
    assert "latet reserve-port --remove" in result.output


def test_global_uninstall_asks_nothing_without_a_record(reserved: Reserved) -> None:
    result = uninstall()  # any question would read EOF and abort
    assert result.exit_code == 0, result.output
    assert reserved.removed == []
    assert "reserve-port" not in result.output


def test_global_uninstall_with_no_console_prints_the_command(reserved: Reserved) -> None:
    reserved.record = [5283]
    reserved.console = False
    result = uninstall()
    assert result.exit_code == 0, result.output
    assert reserved.removed == []
    assert "latet reserve-port --remove" in result.output


def test_global_uninstall_says_a_removal_that_failed_as_reserve_port_does(
    reserved: Reserved, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserved.record = [5283]

    def declined(ports: Any) -> list[int]:
        raise reservation.ElevationDeclined()

    monkeypatch.setattr(reservation, "remove", declined)
    result = uninstall("y\n")
    assert result.exit_code == 1
    assert "Nothing taken away: the administrator's confirmation was declined." in result.output
    assert "daemon: stopped" in result.output  # the uninstall itself was done before the question


def test_global_uninstall_never_says_to_start_latet_again(reserved: Reserved, monkeypatch: pytest.MonkeyPatch) -> None:
    """A port still reserved after the elevated delete: the line to run by hand, with the daemon the uninstall stopped
    left stopped."""
    reserved.record = [5283]
    monkeypatch.setattr(reservation, "remove", lambda ports: [])
    result = uninstall("y\n")
    assert result.exit_code == 1
    assert "Port 5283 is still reserved. To take it away by hand, in a PowerShell run as administrator:" in (
        result.output
    )
    assert f"netsh {reservation.delete_line(5283)}" in result.output
    assert "latet start" not in result.output


def test_global_uninstall_says_a_record_that_cannot_be_read(
    reserved: Reserved, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = getattr(reservation, "RecordUnreadable", None)
    assert unreadable is not None, "reservation.RecordUnreadable does not exist"

    def fails() -> list[int]:
        raise unreadable("Latet's record of the ports it reserved, port-reservations.json, cannot be read")

    monkeypatch.setattr(reservation, "read_record", fails)
    result = uninstall()  # no question: any would read EOF and abort
    assert result.exit_code == 0, result.output
    assert "port-reservations.json, cannot be read" in result.output
    assert "`netsh int ipv4 show excludedportrange protocol=tcp`" in result.output
    assert reserved.removed == []
