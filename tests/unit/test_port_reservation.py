"""The elevated netsh and Latet's record of the ports it reserved: nothing here shows a UAC dialog or runs netsh."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from latet.service import ports, reservation
from latet.service.ports import ExcludedRange

SYSTEM = "C:\\Windows\\System32"
NETSH = f"{SYSTEM}\\netsh.exe"
CMD = f"{SYSTEM}\\cmd.exe"


@pytest.fixture(autouse=True)
def _data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATET_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ports, "system_folder", lambda: SYSTEM, raising=False)


class Elevation:
    """Stands in for the one function that calls Windows: records the program and the parameters it is handed, as
    one line each."""

    def __init__(self, result: int | None = 0, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.result = result
        self.raises = raises

    def __call__(self, *handed: object) -> int | None:
        self.calls.append(" ".join(str(part) for part in handed[:-1]))  # the last one is the timeout
        if self.raises is not None:
            raise self.raises
        return self.result


def _table(monkeypatch: pytest.MonkeyPatch, *ranges: ExcludedRange) -> None:
    """Windows' table, as both readers give it: the cached one and the one read fresh."""

    def fake() -> tuple[ExcludedRange, ...]:
        return tuple(ranges)

    fake.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(ports, "excluded_ranges", fake)
    monkeypatch.setattr(ports, "read_excluded_ranges", fake, raising=False)


def _tables(monkeypatch: pytest.MonkeyPatch, *states: tuple[ExcludedRange, ...]) -> None:
    """Windows' table changing: each fresh read gives the next state (the last one stays); the cached reader gives the
    state read last."""
    reads = iter(states)
    last = [states[0]]

    def fake() -> tuple[ExcludedRange, ...]:
        return last[0]

    def fresh() -> tuple[ExcludedRange, ...]:
        last[0] = next(reads, last[0])
        return last[0]

    fake.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(ports, "excluded_ranges", fake)
    monkeypatch.setattr(ports, "read_excluded_ranges", fresh, raising=False)


def _unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """netsh that fails: the cached reader gives an empty table, as it does then; the fresh one raises."""

    def empty() -> tuple[ExcludedRange, ...]:
        return ()

    def fails() -> tuple[ExcludedRange, ...]:
        raise getattr(ports, "TableUnreadable", OSError)("netsh exited with code 1")

    empty.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(ports, "excluded_ranges", empty)
    monkeypatch.setattr(ports, "read_excluded_ranges", fails, raising=False)


def _readable_then_not(monkeypatch: pytest.MonkeyPatch, first: tuple[ExcludedRange, ...]) -> None:
    """Windows' table read once before the elevated step, then no longer readable."""
    reads = iter([first])

    def fresh() -> tuple[ExcludedRange, ...]:
        found = next(reads, None)
        if found is None:
            raise ports.TableUnreadable("netsh exited with code 1")
        return found

    _table(monkeypatch, *first)
    monkeypatch.setattr(ports, "read_excluded_ranges", fresh)


def _record(tmp_path: Path, ports_: object) -> None:
    (tmp_path / reservation.RECORD).write_text(json.dumps({"ports": ports_}), encoding="utf-8")


def test_the_elevated_command_is_fixed_netsh_lines_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(monkeypatch, (), (ExcludedRange(5341, 5341, True),))
    reservation.reserve(5341, release=[5283])
    assert elevation.calls == [
        f'{CMD} /d /c "{NETSH} int ipv4 add excludedportrange protocol=tcp startport=5341 numberofports=1 '
        f"store=persistent & {NETSH} int ipv4 delete excludedportrange protocol=tcp startport=5283 "
        f'numberofports=1 store=persistent"'
    ]


def test_one_line_runs_netsh_itself_so_that_windows_names_it_in_the_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """With cmd.exe, the UAC dialog would name "Windows Command Processor" for the usual single reservation."""
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(monkeypatch, (), (ExcludedRange(5283, 5283, True),))
    reservation.reserve(5283)
    assert elevation.calls == [
        f"{NETSH} int ipv4 add excludedportrange protocol=tcp startport=5283 numberofports=1 store=persistent"
    ]


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_a_port_outside_1_to_65535_is_refused_before_anything_runs(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    with pytest.raises(ValueError, match="not a port"):
        reservation.add_line(port)
    with pytest.raises(ValueError, match="not a port"):
        reservation.delete_line(port)
    with pytest.raises(ValueError, match="not a port"):
        reservation.reserve(port)
    with pytest.raises(ValueError, match="not a port"):
        reservation.reserve(5341, release=[port])
    assert elevation.calls == []


def test_reserved_only_when_the_table_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(0))
    _table(monkeypatch)
    assert reservation.reserve(5341) is False
    assert reservation.recorded() == []
    _tables(monkeypatch, (), (ExcludedRange(5341, 5341, True),))
    assert reservation.reserve(5341) is True
    assert reservation.recorded() == [5341]


def test_a_released_port_leaves_the_record_unless_a_range_still_holds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record(tmp_path, [5283, 6000])
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(0))
    _tables(
        monkeypatch,
        (ExcludedRange(5283, 5283, True), ExcludedRange(6000, 6000, True)),
        (ExcludedRange(5341, 5341, True), ExcludedRange(6000, 6000, True)),
    )
    assert reservation.reserve(5341, release=[5283, 6000]) is True
    assert reservation.recorded() == [6000, 5341]


@pytest.mark.parametrize(
    "theirs",
    [ExcludedRange(5341, 5400, True), ExcludedRange(5341, 5341, True)],
    ids=["a wider range", "the one port"],
)
def test_a_port_someone_else_reserved_is_neither_reserved_again_nor_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, theirs: ExcludedRange
) -> None:
    """Read before the elevated step: an exclusion Latet did not make is never recorded as Latet's, so that `latet
    reserve-port --remove` never takes it away."""
    _record(tmp_path, [5283])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _table(monkeypatch, theirs, ExcludedRange(5283, 5283, True))
    already = getattr(reservation, "ReservedAlready", None)
    assert already is not None, "reservation.ReservedAlready does not exist"
    with pytest.raises(already):
        reservation.reserve(5341, release=[5283])
    assert elevation.calls == []
    assert reservation.recorded() == [5283]


def test_a_port_latet_reserved_already_is_not_added_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A move back to a port on Latet's record: no add for it, and the elevated step only for what it gives up."""
    _record(tmp_path, [5341, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(
        monkeypatch,
        (ExcludedRange(5341, 5341, True), ExcludedRange(6000, 6000, True)),
        (ExcludedRange(5341, 5341, True),),
    )
    assert reservation.reserve(5341, release=[6000]) is True
    assert elevation.calls == [
        f"{NETSH} int ipv4 delete excludedportrange protocol=tcp startport=6000 numberofports=1 store=persistent"
    ]
    assert reservation.recorded() == [5341]
    elevation.calls.clear()
    assert reservation.reserve(5341) is True
    assert elevation.calls == []


def test_a_table_that_cannot_be_read_before_a_reservation_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record(tmp_path, [5283])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _unreadable(monkeypatch)
    with pytest.raises(ports.TableUnreadable) as raised:
        reservation.reserve(5341, release=[5283])
    assert not isinstance(raised.value, reservation.OutcomeUnknown)  # nothing ran: the outcome is known
    assert elevation.calls == []
    assert reservation.recorded() == [5283]


def test_a_declined_dialog_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _record(tmp_path, [5283])
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(raises=reservation.ElevationDeclined()))
    _table(monkeypatch, ExcludedRange(5283, 5283, True))
    with pytest.raises(reservation.ElevationDeclined):
        reservation.reserve(5341, release=[5283])
    assert reservation.recorded() == [5283]


def test_a_netsh_that_does_not_end_in_time_is_judged_by_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait that runs out (no exit code) is for netsh once the dialog was answered: the table read afterwards
    says whether the port is reserved."""
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(None))
    _table(monkeypatch)
    assert reservation.reserve(5341) is False
    _tables(monkeypatch, (), (ExcludedRange(5341, 5341, True),))
    assert reservation.reserve(5341) is True


def test_remove_takes_away_only_latets_own_single_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _record(tmp_path, [5283, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(
        monkeypatch,
        (ExcludedRange(5283, 5283, True), ExcludedRange(6000, 6009, True)),
        (ExcludedRange(6000, 6009, True),),
    )
    assert reservation.remove([5283, 6000]) == [5283]
    assert len(elevation.calls) == 1
    assert "startport=5283 " in elevation.calls[0]
    assert "startport=6000" not in elevation.calls[0]
    assert reservation.recorded() == [6000]


def test_a_recorded_port_no_range_holds_leaves_the_record_without_elevation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Taken away by hand, or held by a range of Windows' own alone: gone, off the record, and no UAC dialog for it."""
    _record(tmp_path, [5283, 5341])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _table(monkeypatch, ExcludedRange(5300, 5399, False))
    assert reservation.remove([5283, 5341]) == [5283, 5341]
    assert elevation.calls == []
    assert reservation.recorded() == []


def test_a_port_gone_already_leaves_the_record_beside_one_taken_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record(tmp_path, [5283, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(monkeypatch, (ExcludedRange(6000, 6000, True),), ())
    assert reservation.remove([5283, 6000]) == [5283, 6000]
    assert len(elevation.calls) == 1
    assert "startport=6000 " in elevation.calls[0]
    assert "startport=5283" not in elevation.calls[0]
    assert reservation.recorded() == []


def test_a_port_gone_already_leaves_the_record_when_the_dialog_is_declined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record(tmp_path, [5283, 6000])
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(raises=reservation.ElevationDeclined()))
    _table(monkeypatch, ExcludedRange(6000, 6000, True))
    with pytest.raises(reservation.ElevationDeclined):
        reservation.remove([5283, 6000])
    assert reservation.recorded() == [6000]


def test_a_table_that_cannot_be_read_changes_neither_the_record_nor_the_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """netsh failing is no table with nothing in it: the ports Latet reserved stay on the record, and no UAC dialog."""
    _record(tmp_path, [5283, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _unreadable(monkeypatch)
    with pytest.raises(OSError, match="netsh exited with code 1"):
        reservation.remove([5283, 6000])
    assert elevation.calls == []
    assert reservation.recorded() == [5283, 6000]


def test_a_table_that_cannot_be_read_after_a_reservation_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whether the elevated step worked is not known: no port added to the record, none released from it."""
    _record(tmp_path, [5283, 6000])
    elevation = Elevation(0)
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _readable_then_not(monkeypatch, (ExcludedRange(5283, 5283, True), ExcludedRange(6000, 6000, True)))
    with pytest.raises(reservation.OutcomeUnknown, match="netsh exited with code 1"):
        reservation.reserve(5341, release=[5283])
    assert len(elevation.calls) == 1
    assert reservation.recorded() == [5283, 6000]


def test_a_record_that_cannot_be_written_after_the_delete_still_says_the_ports_are_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The elevated delete took the ports away; a record file an antivirus holds is no "nothing taken away"."""
    _record(tmp_path, [5283, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(monkeypatch, (ExcludedRange(5283, 5283, True), ExcludedRange(6000, 6000, True)), ())
    written: list[list[int]] = []

    def locked(found: list[int]) -> None:
        written.append(found)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(reservation, "_write_record", locked)
    not_updated = getattr(reservation, "RecordNotUpdated", None)
    assert not_updated is not None, "reservation.RecordNotUpdated does not exist"
    with pytest.raises(not_updated) as raised:
        reservation.remove([5283, 6000])
    assert raised.value.gone == [5283, 6000]
    assert "Permission denied" in str(raised.value)
    assert len(elevation.calls) == 1
    assert written == [[]]


def test_a_table_that_cannot_be_read_after_the_elevated_delete_is_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read before, the table showed 5283 gone (off the record) and 6000 Latet's (deleted); read after, it cannot be
    read: whether 6000 is gone is not known, so it stays on the record, and the error says it came after the step."""
    _record(tmp_path, [5283, 6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    reads = iter([(ExcludedRange(6000, 6000, True),)])

    def fresh() -> tuple[ExcludedRange, ...]:
        found = next(reads, None)
        if found is None:
            raise ports.TableUnreadable("netsh exited with code 1")
        return found

    _table(monkeypatch, ExcludedRange(6000, 6000, True))
    monkeypatch.setattr(ports, "read_excluded_ranges", fresh)
    unknown = getattr(reservation, "OutcomeUnknown", None)
    assert unknown is not None, "reservation.OutcomeUnknown does not exist"
    with pytest.raises(unknown, match="netsh exited with code 1"):
        reservation.remove([5283, 6000])
    assert len(elevation.calls) == 1
    assert "startport=6000 " in elevation.calls[0]
    assert reservation.recorded() == [6000]
    assert issubclass(unknown, ports.TableUnreadable)  # a table that could not be read, as any other


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal off Windows")
def test_off_windows_nothing_is_reserved_or_taken_off_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With Windows' real readers, which have no table to read off Windows."""
    _record(tmp_path, [5283])
    with pytest.raises(ports.TableUnreadable):
        reservation.remove([5283])
    with pytest.raises(ports.TableUnreadable):
        reservation.reserve(5341, release=[5283])
    with pytest.raises(OSError, match="port reservation is for Windows"):
        reservation.run_elevated([reservation.add_line(5341)])
    assert reservation.recorded() == [5283]


def test_remove_with_nothing_of_latets_runs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _record(tmp_path, [6000])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _table(monkeypatch, ExcludedRange(6000, 6009, True))
    assert reservation.remove([6000]) == []
    assert elevation.calls == []


def test_the_manual_command_is_the_same_netsh_line() -> None:
    assert reservation.manual_command(5283) == (
        "netsh int ipv4 add excludedportrange protocol=tcp startport=5283 numberofports=1 store=persistent"
    )


UNREADABLE = ['{"ports": [5283', '{"ports": ["x"]}', "[]", '{"ports": [true]}', '{"ports": 5}', "\xff\xfe"]


@pytest.mark.parametrize("content", UNREADABLE)
def test_an_unreadable_record_is_told_from_a_missing_one(tmp_path: Path, content: str) -> None:
    """``recorded`` lists, and never raises; ``read_record`` says that the record cannot be read."""
    (tmp_path / reservation.RECORD).write_bytes(content.encode("latin-1"))
    assert reservation.recorded() == []
    unreadable = getattr(reservation, "RecordUnreadable", None)
    assert unreadable is not None, "reservation.RecordUnreadable does not exist"
    with pytest.raises(unreadable, match="cannot be read"):
        reservation.read_record()


def test_a_missing_record_is_an_empty_one() -> None:
    assert reservation.recorded() == []
    assert reservation.read_record() == []


def test_a_deeply_nested_record_is_an_unreadable_one(tmp_path: Path) -> None:
    (tmp_path / reservation.RECORD).write_text('{"ports": ' + "[" * 100000 + "]" * 100000 + "}", encoding="utf-8")
    assert reservation.recorded() == []
    with pytest.raises(reservation.RecordUnreadable):
        reservation.read_record()


def test_a_reservation_beside_an_unreadable_record_leaves_that_record_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It reserves, and says it could not record it: the file it cannot read is never written over."""
    (tmp_path / reservation.RECORD).write_text('{"ports": [5283', encoding="utf-8")
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _tables(monkeypatch, (), (ExcludedRange(5341, 5341, True),))
    unreadable = getattr(reservation, "RecordUnreadable", None)
    assert unreadable is not None, "reservation.RecordUnreadable does not exist"
    with pytest.raises(unreadable):
        reservation.reserve(5341)
    assert len(elevation.calls) == 1
    assert (tmp_path / reservation.RECORD).read_text(encoding="utf-8") == '{"ports": [5283'


def test_remove_with_an_unreadable_record_changes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / reservation.RECORD).write_text('{"ports": [5283', encoding="utf-8")
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _table(monkeypatch, ExcludedRange(5283, 5283, True))
    unreadable = getattr(reservation, "RecordUnreadable", None)
    assert unreadable is not None, "reservation.RecordUnreadable does not exist"
    with pytest.raises(unreadable):
        reservation.remove([5283])
    assert elevation.calls == []
    assert (tmp_path / reservation.RECORD).read_text(encoding="utf-8") == '{"ports": [5283'


def test_remove_leaves_an_administrators_own_single_port_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _record(tmp_path, [5283])
    elevation = Elevation()
    monkeypatch.setattr(reservation, "_shell_execute", elevation)
    _table(monkeypatch, ExcludedRange(5283, 5283, True), ExcludedRange(7000, 7000, True))
    assert reservation.remove([7000]) == []
    assert elevation.calls == []
    assert reservation.recorded() == [5283]


def test_a_range_of_windows_own_is_no_reservation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(reservation, "_shell_execute", Elevation(0))
    _table(monkeypatch, ExcludedRange(5300, 5399, False))
    assert reservation.reserve(5341) is False
    assert reservation.recorded() == []


def test_a_declined_dialog_is_told_from_another_failure() -> None:
    assert isinstance(reservation._failure(1223), reservation.ElevationDeclined)
    other = reservation._failure(5)
    assert isinstance(other, OSError)
    assert not isinstance(other, reservation.ElevationDeclined)


@pytest.mark.skipif(sys.platform != "win32", reason="asks Windows for its system folder")
def test_the_system_folder_comes_from_windows_not_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the autouse fake of the system folder
    monkeypatch.setenv("SYSTEMROOT", "C:\\Elsewhere")
    monkeypatch.setenv("windir", "C:\\Elsewhere")
    folder = ports.system_folder()
    assert "elsewhere" not in folder.lower()
    assert folder.lower().endswith("\\system32")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own structure for ShellExecuteExW")
def test_the_dialog_is_asked_for_in_front_of_the_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console's window is handed to Windows, so that the UAC dialog comes to the front; nothing is started."""
    monkeypatch.setattr(reservation, "_console_window", lambda: 0x1234, raising=False)
    shaped = getattr(reservation, "_execute_info", None)
    assert shaped is not None, "reservation._execute_info does not exist"
    info = shaped(NETSH, "int ipv4 show dynamicport tcp")
    assert info.hwnd == 0x1234
    assert (info.lpVerb, info.lpFile, info.lpParameters, info.nShow) == (
        "runas",
        NETSH,
        "int ipv4 show dynamicport tcp",
        0,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="asks Windows for the console's window")
def test_the_console_window_is_the_one_windows_gives() -> None:
    assert sys.platform == "win32"  # for the type checker, as the mark skips it elsewhere
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetConsoleWindow.restype = wintypes.HWND
    assert reservation._console_window() == kernel.GetConsoleWindow()
