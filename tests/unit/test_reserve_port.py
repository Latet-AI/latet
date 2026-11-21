"""`latet reserve-port`: Latet's port reserved on Windows. Nothing here shows a UAC dialog, runs netsh or changes the
machine: Windows' findings, the free port, the elevated step, the daemon and the agents applied again are faked where
the command meets them, and each call is written down in the order it came."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import latet.cli.admin  # registers the commands
import latet.config
from latet.cli.main import app
from latet.config import load_config
from latet.integrations import base
from latet.service import daemon, ports, reservation
from latet.service.ports import PortState

CONFIG = "port: 5283\n"
REFRESHED = latet.cli.admin.refreshed


class Machine:
    """What the command meets: the finding for each port, the free port Latet would move to, the elevated step, the
    daemon (running on 5283 or not, stopping or not), the agents applied again, the record of what Latet reserved."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.calls: list[tuple[Any, ...]] = []
        self.findings: dict[int, str] = {5283: "at-risk"}
        self.free = 5341
        self.running = False
        self.stops = True
        self.reserves = True
        self.raises: BaseException | None = None
        self.after: str | None = None  # the finding of the reserved port once the elevated step ran
        self.no_free = False
        self.record: list[int] = []

    def changes(self) -> list[tuple[Any, ...]]:
        """The calls that change something (the search for a free port changes nothing)."""
        return [call for call in self.calls if call[0] != "free_port"]

    def finding(self, port: int) -> str:
        return self.findings.get(port, "safe")

    def free_port(self, start: int, below: int = 49152, ours: Sequence[int] = ()) -> PortState:
        self.calls.append(("free_port", start, list(ours)))
        if self.no_free:
            raise ports.NoFreePortError(f"no usable port from {start} up to {below - 1}")
        return PortState(self.free, True, "free")

    def reserve(self, port: int, release: Sequence[int] = ()) -> bool:
        self.calls.append(("reserve", port, list(release)))
        if self.after is not None:
            self.findings[port] = self.after
        if self.raises is not None:
            raise self.raises
        return self.reserves

    def remove(self, found: Sequence[int]) -> list[int]:
        self.calls.append(("remove", list(found)))
        return list(found)

    def status(self, port: int | None = None) -> daemon.Status:
        return daemon.Status(self.running and port in (None, 5283), port=port or 5283)

    def stop(self, timeout: float = 10.0) -> bool:
        self.calls.append(("stop",))
        self.running = not self.stops
        return self.stops

    def start(self, wait: float = 15.0, patience: float = 90.0, slow: Any = None) -> daemon.Status:
        port = load_config().port  # the port the daemon would start on: config.yaml's as it is now
        self.calls.append(("start", port))
        return daemon.Status(True, port=port)

    def refreshed(self, said: str, skip: set[str] | None = None) -> bool:
        self.calls.append(("refreshed", said))
        return True


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    from latet.cli import reserve

    found = Machine(tmp_path / "config.yaml")
    found.config.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LATET_CONFIG", str(found.config))
    monkeypatch.delenv("LATET_PORT", raising=False)
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: None)
    monkeypatch.setattr(reserve, "_windows", lambda: True)
    monkeypatch.setattr(reserve, "_console", lambda: True)
    monkeypatch.setattr("latet.wrappers.connected", lambda: None)
    monkeypatch.setattr(ports, "reservation", found.finding)
    monkeypatch.setattr(ports, "free_port", found.free_port)
    monkeypatch.setattr(reservation, "reserve", found.reserve)
    monkeypatch.setattr(reservation, "remove", found.remove)
    monkeypatch.setattr(reservation, "recorded", lambda: list(found.record))
    monkeypatch.setattr(reservation, "read_record", lambda: list(found.record), raising=False)
    monkeypatch.setattr(daemon, "status", found.status)
    monkeypatch.setattr(daemon, "stop", found.stop)
    monkeypatch.setattr(daemon, "start", found.start)
    monkeypatch.setattr("latet.cli.admin.refreshed", found.refreshed)
    # The records of the agents Latet applied, read as global-install reads them; the data folder's own checks are
    # left out (the test's folder is no data folder of Latet's).
    monkeypatch.setattr(base, "records_preflight", lambda *, write=False: base.load_records())
    saving = latet.config.save_config

    def save(config: latet.config.Config, path: Path | None = None) -> Path:
        found.calls.append(("save", config.port))
        return saving(config, path)

    monkeypatch.setattr(latet.config, "save_config", save)
    return found


def run(*args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(app, ["reserve-port", *args], input=input)


def test_refused_when_an_agent_runs_it(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: "Claude Code")
    result = run("--yes")
    assert result.exit_code == 2
    assert "`latet reserve-port` changes a setting of Windows: run it yourself" in result.output
    assert machine.calls == []


@pytest.mark.parametrize("case", ["off Windows", "safe", "reserved"])
def test_nothing_to_do_off_windows_and_where_the_port_is_safe_or_reserved(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from latet.cli import reserve

    if case == "off Windows":
        monkeypatch.setattr(reserve, "_windows", lambda: False)
    else:
        machine.findings[5283] = case
    machine.running = True
    machine.record = [5341]
    result = run("--yes")
    assert result.exit_code == 0, result.output
    said = "reserved already" if case == "reserved" else "nothing to reserve"
    assert said in result.output
    offered = run("--offer")
    assert offered.exit_code == 0
    assert offered.output == ""
    if case == "off Windows":  # neither reserve nor remove is ever called there: Windows' tables are not there
        assert run("--remove").exit_code == 0
    assert machine.calls == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_at_risk_asks_and_reserves_the_port_where_it_is(machine: Machine) -> None:
    result = run(input="y\n")
    assert result.exit_code == 0, result.output
    assert "port 5283, Latet's, is among the ports it may take" in result.output
    assert "Reserve port 5283 for Latet? Windows asks for an administrator's confirmation once. [Y/n]" in result.output
    assert machine.calls == [("reserve", 5283, [])]
    assert "Port 5283 is reserved for Latet" in result.output
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_at_risk_restarts_the_daemon_that_runs_on_the_port(machine: Machine) -> None:
    machine.running = True
    result = run(input="y\n")
    assert result.exit_code == 0, result.output
    assert "Latet restarts for a moment: the agents retry what they send meanwhile." in result.output
    assert machine.calls == [("stop",), ("reserve", 5283, []), ("start", 5283)]


def test_the_ports_latet_reserved_before_are_given_up_but_never_the_one_in_config(machine: Machine) -> None:
    machine.record = [6000, 5283, 7000]
    result = run("--yes")
    assert result.exit_code == 0, result.output
    assert machine.calls == [("reserve", 5283, [6000, 7000])]


def test_held_moves_latet_after_the_reservation_and_repoints_the_agents(machine: Machine) -> None:
    machine.findings[5283] = "held"
    machine.running = True
    machine.record = [6000]
    result = run(input="y\n")
    assert result.exit_code == 0, result.output
    assert "Windows holds port 5283 since it started: Latet moves to port 5341, free, and reserves it." in result.output
    assert "Move Latet to port 5341 and reserve it? Windows asks for an administrator's confirmation once." in (
        result.output
    )
    assert machine.changes() == [
        ("stop",),
        ("reserve", 5341, [6000]),
        ("save", 5341),
        ("refreshed", "now on port 5341"),
        ("start", 5341),
    ]
    assert load_config().port == 5341
    # Agent sessions open before the move still call the old port.
    assert result.output.rstrip().endswith("Agent sessions open now keep calling port 5283 until you start them again.")


def test_the_move_lands_on_no_port_someone_else_reserved(machine: Machine) -> None:
    """The search for the new port is told which ports Latet reserved: an administered range of anyone else's is
    skipped, so that Latet never records another's exclusion as its own."""
    machine.findings[5283] = "held"
    machine.record = [6000]
    result = run("--yes")
    assert result.exit_code == 0, result.output
    assert ("free_port", 5283, [6000]) in machine.calls


def test_a_port_someone_else_reserved_already_is_said_and_not_recorded(machine: Machine) -> None:
    already = getattr(reservation, "ReservedAlready", None)
    assert already is not None, "reservation.ReservedAlready does not exist"
    machine.raises = already(5283)
    result = run("--yes")
    assert result.exit_code == 0, result.output
    assert (
        "Port 5283 is reserved already, by an exclusion not on Latet's record: Windows cannot take it, and `latet "
        "reserve-port --remove` leaves it alone." in result.output
    )
    assert "Not reserved" not in result.output


def test_held_still_moves_when_the_dialog_is_declined(machine: Machine) -> None:
    machine.findings[5283] = "held"
    machine.raises = reservation.ElevationDeclined()
    result = run("--yes")
    assert result.exit_code == 1
    assert "declined" in result.output
    assert "Latet moves to port 5341 all the same." in result.output
    assert "`latet reserve-port`" in result.output
    assert reservation.manual_command(5341) in result.output  # the port Latet uses now
    assert machine.changes() == [
        ("reserve", 5341, []),
        ("save", 5341),
        ("refreshed", "now on port 5341"),
        ("start", 5341),
    ]
    assert load_config().port == 5341


def test_a_reservation_windows_does_not_show_is_said(machine: Machine) -> None:
    machine.reserves = False
    result = run("--yes")
    assert result.exit_code == 1
    assert "Not reserved" in result.output
    assert reservation.manual_command(5283) in result.output


HAND = (
    "by hand, with Latet stopped: `latet stop`, then this line in a PowerShell run as administrator, then "
    "`latet start`:"
)


def test_no_changes_nothing_and_prints_what_to_run(machine: Machine) -> None:
    machine.running = True
    result = run(input="n\n")
    assert result.exit_code == 0, result.output
    assert "Not reserved. To do it later: `latet reserve-port`, which does all of it." in result.output
    assert f"Or {HAND}" in result.output  # netsh may refuse to exclude a port the daemon listens on
    assert f"  {reservation.manual_command(5283)}" in result.output
    assert machine.changes() == []

    machine.findings[5283] = "held"
    result = run(input="n\n")
    assert result.exit_code == 0, result.output
    assert "`latet reserve-port`" in result.output
    assert "netsh" not in result.output  # a port Latet does not use yet: nothing would move to it
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize("offer", [False, True])
def test_with_no_console_it_asks_nothing(machine: Machine, monkeypatch: pytest.MonkeyPatch, offer: bool) -> None:
    from latet.cli import reserve

    monkeypatch.setattr(reserve, "_console", lambda: False)
    machine.running = True
    result = run(*(["--offer"] if offer else []))
    assert result.exit_code == 0, result.output
    assert "?" not in result.output
    assert "restarts" not in result.output  # nothing is asked, nothing restarts
    assert reserve.offer_lines(5283, None) == [
        "Not reserved. To do it later: `latet reserve-port`, which does all of it.",
        f"Or {HAND}",
        f"  {reservation.manual_command(5283)}",
    ]
    assert reserve.offer_lines(5283, 5341) == [
        "Not moved to port 5341, and not reserved. To do it later: `latet reserve-port`, which does all of it."
    ]
    assert result.output.endswith("\n".join(reserve.offer_lines(5283, None)) + "\n")
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_with_no_console_a_move_is_one_that_would_happen(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    from latet.cli import reserve

    monkeypatch.setattr(reserve, "_console", lambda: False)
    machine.findings[5283] = "held"
    machine.running = True
    result = run()
    assert result.exit_code == 0, result.output
    assert "Windows holds port 5283 since it started: Latet would move to port 5341, free, and reserve it." in (
        result.output
    )
    assert "Latet moves" not in result.output
    assert "restarts" not in result.output
    assert result.output.endswith("\n".join(reserve.offer_lines(5283, 5341)) + "\n")
    assert machine.changes() == []


def test_damaged_agent_records_refuse_a_move_before_anything_changes(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move applies the agents again from their records: records that cannot be used refuse it up front, not after
    the elevated step and config.yaml's save."""

    def damaged() -> dict:
        raise base.RecordsUnavailable("Integration records unavailable (byte limit)", "byte limit")

    monkeypatch.setattr(base, "load_records", damaged)
    machine.findings[5283] = "held"
    machine.running = True
    result = run("--yes")
    assert result.exit_code == 1
    assert "Integration records unavailable (byte limit)" in result.output
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_a_config_that_cannot_be_saved_starts_the_daemon_again(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(config: latet.config.Config, path: Path | None = None) -> Path:
        machine.calls.append(("save", config.port))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(latet.config, "save_config", full)
    machine.findings[5283] = "held"
    machine.running = True
    result = run("--yes")
    assert result.exit_code == 1
    assert "No space left on device" in result.output
    assert "Traceback" not in result.output
    assert machine.changes() == [("stop",), ("reserve", 5341, []), ("save", 5341), ("start", 5283)]
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_the_daemon_starts_again_when_the_wait_for_the_dialog_is_interrupted(machine: Machine) -> None:
    machine.running = True
    machine.raises = KeyboardInterrupt()
    result = run("--yes")
    assert result.exit_code != 0  # click's code for an interruption
    assert machine.changes() == [("stop",), ("reserve", 5283, []), ("start", 5283)]


def test_a_failure_of_the_elevated_step_is_said_and_the_daemon_runs_again(machine: Machine) -> None:
    machine.running = True
    machine.raises = OSError("ShellExecuteExW failed (5)")
    result = run("--yes")
    assert result.exit_code == 1
    assert "Not reserved: ShellExecuteExW failed (5)." in result.output
    assert reservation.manual_command(5283) in result.output
    assert machine.changes() == [("stop",), ("reserve", 5283, []), ("start", 5283)]


def test_a_reservation_made_but_not_recorded_names_what_takes_it_away(machine: Machine) -> None:
    """The exclusion exists but the record could not be written: `latet reserve-port --remove` would not know it, so
    the netsh line that deletes it is said, never the one that adds it."""
    machine.raises = OSError(13, "Permission denied")
    machine.after = "reserved"
    result = run("--yes")
    assert result.exit_code == 1
    assert "Port 5283 is reserved for Latet, but Latet could not record it" in result.output
    assert f"To take it away later {HAND}" in result.output
    assert f"netsh {reservation.delete_line(5283)}" in result.output
    assert reservation.manual_command(5283) not in result.output
    assert "Not reserved" not in result.output


def test_no_free_port_changes_nothing(machine: Machine) -> None:
    machine.findings[5283] = "held"
    machine.running = True
    machine.no_free = True
    result = run("--yes")
    assert result.exit_code == 1
    assert "no port above it up to 49151 is free: nothing changed" in result.output
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_agents_not_applied_again_after_a_move_is_a_failure(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(skip: set[str] | None = None) -> list[str]:
        raise latet.cli.admin.NotAllApplied(["Codex"], [])

    monkeypatch.setattr("latet.cli.admin.refreshed", REFRESHED)
    monkeypatch.setattr("latet.cli.admin.refresh_applied", refused)
    machine.findings[5283] = "held"
    result = run("--yes")
    assert result.exit_code == 1
    assert "Not applied again: Codex. `latet agents apply` applies them." in result.output
    assert machine.changes() == [("reserve", 5341, []), ("save", 5341), ("start", 5341)]
    assert load_config().port == 5341


@pytest.mark.parametrize("finding", ["at-risk", "held"])
def test_the_daemon_that_will_not_stop_stops_everything(machine: Machine, finding: str) -> None:
    machine.findings[5283] = finding
    machine.running = True
    machine.stops = False
    result = run("--yes")
    assert result.exit_code == 1
    assert "did not stop" in result.output
    assert machine.changes() == [("stop",)]
    assert machine.config.read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize("offer", [False, True])
def test_a_port_set_by_latet_port_is_never_moved(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, offer: bool
) -> None:
    monkeypatch.setenv("LATET_PORT", "5283")
    machine.findings[5283] = "held"
    result = run("--yes", *(["--offer"] if offer else []))
    assert result.exit_code == 1
    assert "LATET_PORT" in result.output
    assert "could not be made" not in result.output  # the command's own refusal, not a failure of the offer
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


def test_the_offer_lets_an_interrupted_question_through(machine: Machine) -> None:
    """End of input at the question is an abort, as without --offer: never "could not be made"."""
    result = run("--offer", input="")
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert "could not be made" not in result.output
    assert machine.changes() == []


def test_a_port_set_by_latet_port_is_reserved_and_gives_up_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.yaml's port may be one Latet reserved: a reservation for LATET_PORT's never takes it away."""
    machine.config.write_text("port: 6000\n", encoding="utf-8")
    monkeypatch.setenv("LATET_PORT", "5283")
    machine.record = [6000]
    result = run("--yes")
    assert result.exit_code == 0, result.output
    assert machine.calls == [("reserve", 5283, [])]


def test_remove_takes_away_what_latet_recorded(machine: Machine) -> None:
    machine.record = [5341]
    result = run("--remove")
    assert result.exit_code == 0, result.output
    assert machine.calls == [("remove", [5341])]
    assert "Port 5341 is no longer reserved." in result.output

    machine.record = []
    machine.calls.clear()
    result = run("--remove")
    assert result.exit_code == 0, result.output
    assert "Latet reserved no port" in result.output
    assert machine.calls == []


def test_a_removal_that_did_not_happen_is_said(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    machine.record = [5341]
    machine.findings[5341] = "reserved"
    monkeypatch.setattr(reservation, "remove", lambda found: [])
    result = run("--remove")
    assert result.exit_code == 1
    assert f"Port 5341 is still reserved. To take it away {HAND}" in result.output
    assert f"netsh {reservation.delete_line(5341)}" in result.output

    def declined(found: Sequence[int]) -> list[int]:
        raise reservation.ElevationDeclined()

    monkeypatch.setattr(reservation, "remove", declined)
    result = run("--remove")
    assert result.exit_code == 1
    assert "declined" in result.output
    assert "`latet reserve-port --remove`" in result.output


def test_a_table_that_cannot_be_read_is_said_and_nothing_is_taken_away(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(found: Sequence[int]) -> list[int]:
        machine.calls.append(("remove", list(found)))
        raise ports.TableUnreadable("netsh exited with code 1")

    monkeypatch.setattr(reservation, "remove", unreadable)
    machine.record = [5341]
    result = run("--remove")
    assert result.exit_code == 1
    assert "Nothing taken away: Latet could not read Windows' table of excluded ports (netsh exited with code 1)." in (
        result.output
    )
    assert "no longer reserved" not in result.output
    assert "`latet reserve-port --remove`" in result.output


def _removal(machine: Machine, pruned: list[int], error: BaseException) -> Any:
    """`reservation.remove` that took ``pruned`` off the record (found gone before the elevated step) and then raised
    ``error``."""

    def remove(found: Sequence[int]) -> list[int]:
        machine.calls.append(("remove", list(found)))
        machine.record = [p for p in machine.record if p not in pruned]
        raise error

    return remove


def test_a_table_unreadable_after_the_elevated_delete_says_the_outcome_is_not_known(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = getattr(reservation, "OutcomeUnknown", None)
    assert unknown is not None, "reservation.OutcomeUnknown does not exist"
    machine.record = [5283, 6000]
    monkeypatch.setattr(reservation, "remove", _removal(machine, [5283], unknown("netsh exited with code 1")))
    result = run("--remove")
    assert result.exit_code == 1
    assert "Port 5283 is no longer reserved." in result.output
    assert (
        "Latet could not read Windows' table of excluded ports afterwards (netsh exited with code 1): whether port "
        "6000 was taken away is not known. `latet reserve-port --remove` again tells." in result.output
    )
    assert "Nothing" not in result.output


def test_ports_found_gone_before_a_declined_dialog_are_said(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    machine.record = [5283, 6000, 7000]
    monkeypatch.setattr(reservation, "remove", _removal(machine, [5283], reservation.ElevationDeclined()))
    result = run("--remove")
    assert result.exit_code == 1
    assert "Port 5283 is no longer reserved." in result.output
    assert "Nothing else taken away: the administrator's confirmation was declined." in result.output
    assert "Nothing taken away" not in result.output


def test_a_table_that_cannot_be_read_before_the_elevated_step_is_no_reservation(machine: Machine) -> None:
    """`reservation.reserve` reads the table before anything runs: unreadable then, nothing changed."""
    machine.running = True
    machine.raises = ports.TableUnreadable("netsh exited with code 1")
    result = run("--yes")
    assert result.exit_code == 1
    assert (
        "Not reserved: Latet could not read Windows' table of excluded ports (netsh exited with code 1)."
        in result.output
    )
    assert reservation.manual_command(5283) in result.output
    assert machine.changes() == [("stop",), ("reserve", 5283, []), ("start", 5283)]


def test_a_table_that_cannot_be_read_after_the_elevated_step_is_said(machine: Machine) -> None:
    machine.running = True
    machine.raises = reservation.OutcomeUnknown("netsh exited with code 1")
    result = run("--yes")
    assert result.exit_code == 1
    assert "Latet could not read Windows' table of excluded ports afterwards (netsh exited with code 1)" in (
        result.output
    )
    assert "Not reserved" not in result.output  # not known: the dialog may have done it
    assert reservation.manual_command(5283) not in result.output
    assert machine.changes() == [("stop",), ("reserve", 5283, []), ("start", 5283)]


def _agent_runs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("latet.cli.agent.agent_name", lambda strict=False: "Claude Code")


@pytest.mark.parametrize("finding", ["safe", "reserved"])
def test_an_agent_running_the_installers_offer_where_nothing_is_to_do_says_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, finding: str
) -> None:
    """An agent often runs the installer for you: where the port is safe, its end is no red refusal."""
    _agent_runs_it(monkeypatch)
    machine.findings[5283] = finding
    result = run("--offer")
    assert result.exit_code == 0, result.output
    assert result.output == ""
    assert machine.calls == []


@pytest.mark.parametrize("finding", ["at-risk", "held"])
@pytest.mark.parametrize("yes", [False, True])
def test_an_agent_running_the_offer_prints_what_you_run_yourself(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, finding: str, yes: bool
) -> None:
    from latet.cli import reserve

    _agent_runs_it(monkeypatch)
    machine.findings[5283] = finding
    machine.running = True
    result = run("--offer", *(["--yes"] if yes else []))
    assert result.exit_code == 0, result.output
    assert "run it yourself, in a terminal of your own" not in result.output  # no refusal
    assert "?" not in result.output  # nothing asked
    assert "restarts" not in result.output
    moving_to = 5341 if finding == "held" else None
    assert result.output.endswith("\n".join(reserve.offer_lines(5283, moving_to)) + "\n")
    assert machine.changes() == []
    assert machine.config.read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize("args", [["--remove", "--offer"], ["--yes"], []])
def test_an_agent_is_still_refused_outside_the_offer(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _agent_runs_it(monkeypatch)
    machine.record = [5283]
    result = run(*args)
    assert result.exit_code == 2
    assert "`latet reserve-port` changes a setting of Windows: run it yourself" in result.output
    assert machine.calls == []


@pytest.mark.parametrize(
    ("where", "error", "said"),
    [
        ("latet.config.load_config", "config", "config.yaml is not valid YAML (line 3)"),
        ("latet.service.ports.reservation", "runtime", "netsh is gone"),
    ],
)
def test_the_offer_says_a_failure_in_one_line_never_a_traceback(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, where: str, error: str, said: str
) -> None:
    """At the end of the installer an invalid config.yaml must not make a good install look failed."""

    def broken(*args: object, **kwargs: object) -> Any:
        if error == "config":
            raise latet.config.ConfigError(said + "\n  and more")
        raise RuntimeError(said)

    monkeypatch.setattr(where, broken)
    result = run("--offer")
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    lines = result.output.strip().splitlines()
    assert len(lines) == 1, result.output
    assert said in lines[0]
    assert "`latet reserve-port`" in lines[0]


def test_ports_taken_away_whose_record_cannot_be_updated_are_said_as_gone(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    not_updated = getattr(reservation, "RecordNotUpdated", None)
    assert not_updated is not None, "reservation.RecordNotUpdated does not exist"
    machine.record = [5283, 6000]

    def remove(found: Sequence[int]) -> list[int]:
        raise not_updated([5283, 6000], PermissionError(13, "Permission denied"))

    monkeypatch.setattr(reservation, "remove", remove)
    result = run("--remove")
    assert result.exit_code == 1
    assert "Port 5283 is no longer reserved." in result.output
    assert "Port 6000 is no longer reserved." in result.output
    assert "Nothing" not in result.output
    assert (
        "Latet could not update its record of the ports it reserved ([Errno 13] Permission denied): it still names "
        "ports 5283, 6000. `latet reserve-port --remove` again takes them off it." in result.output
    )


def test_remove_with_a_record_that_cannot_be_read_says_so(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not "Latet reserved no port on this computer": what the computer holds is not known, and netsh tells."""
    unreadable = getattr(reservation, "RecordUnreadable", None)
    assert unreadable is not None, "reservation.RecordUnreadable does not exist"

    def fails() -> list[int]:
        raise unreadable("Latet's record of the ports it reserved, port-reservations.json, cannot be read")

    monkeypatch.setattr(reservation, "read_record", fails)
    result = run("--remove")
    assert result.exit_code == 1
    assert "Latet reserved no port" not in result.output
    assert "port-reservations.json, cannot be read: nothing taken away." in result.output
    assert "`netsh int ipv4 show excludedportrange protocol=tcp`" in result.output
    assert machine.calls == []


def test_a_computer_connected_to_a_server_has_no_port_to_reserve(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("latet.wrappers.connected", lambda: SimpleNamespace(url="https://latet.example.com:8443"))
    result = run("--yes")
    assert result.exit_code == 0, result.output
    assert "connected to https://latet.example.com:8443: your agents go there" in result.output
    offered = run("--offer")
    assert offered.exit_code == 0
    assert offered.output == ""
    assert machine.calls == []
