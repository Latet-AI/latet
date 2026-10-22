"""``write_atomic``: a file is written whole or not at all, and a private one is for its owner alone. Where the system
refuses chmod (a mount that shows files as another owner's) ``write_atomic(private=True)`` fails, and
``write_private_where_allowed`` writes the file all the same."""

import errno
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from latet.config import write_atomic, write_private_where_allowed

TEXT = "terms: [Mercury Labs]\n"
POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="Windows has no file modes: a file's access follows its folder's"
)


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def umask(request):
    """The umask a test asks for (``indirect`` parametrization), put back after it."""
    before = os.umask(request.param)
    yield request.param
    os.umask(before)


@pytest.mark.parametrize("private", [False, True])
def test_a_file_is_written_whole_and_no_temporary_file_stays(tmp_path, private):
    target = tmp_path / "settings.json"
    target.write_text("old", "utf-8")
    write_atomic(target, TEXT, private=private)
    assert target.read_text("utf-8") == TEXT and list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("private", [False, True])
def test_a_write_that_fails_leaves_the_file_as_it_was_and_no_temporary_file(tmp_path, monkeypatch, private):
    target = tmp_path / "settings.json"
    target.write_text("old", "utf-8")

    def full(source, destination):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError, match="No space left"):
        write_atomic(target, TEXT, private=private)
    assert target.read_text("utf-8") == "old" and list(tmp_path.iterdir()) == [target]


def test_a_private_write_still_fails_where_chmod_is_refused_and_leaves_nothing(tmp_path, chmod_refused):
    """``private=True`` is for what others must not read (a key, a settings file with a token in it): where the system
    cannot make it so the write fails, as every caller of it relies on."""
    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "root.key", TEXT, private=True)
    assert list(tmp_path.iterdir()) == []


def test_a_file_better_kept_than_kept_private_is_written_where_chmod_is_refused_and_it_is_said_once(
    tmp_path, chmod_refused, caplog
):
    target = tmp_path / "config.reviewed.json"
    write_private_where_allowed(target, TEXT)
    assert target.read_text("utf-8") == TEXT and list(tmp_path.iterdir()) == [target]
    [record] = _warnings(caplog)
    assert str(target) in record.getMessage() and record.exc_info is None  # by its file; no traceback
    write_private_where_allowed(target, TEXT.replace("Mercury", "Orion"))  # the next change of the settings
    assert target.read_text("utf-8") == "terms: [Orion Labs]\n" and _warnings(caplog) == [record]
    assert "Mercury" not in caplog.text  # never by what it holds


def test_where_chmod_is_allowed_a_file_better_kept_than_kept_private_is_made_private_and_nothing_is_said(
    tmp_path, monkeypatch, caplog
):
    asked, chmod = [], os.chmod

    def recording(path, mode, *args, **kwargs):
        asked.append((Path(path).name, mode))
        chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recording)
    target = tmp_path / "config.reviewed.json"
    write_private_where_allowed(target, TEXT)
    [(written, mode)] = asked
    assert written.startswith(f".{target.name}") and mode == 0o600  # asked for the file being written
    assert target.read_text("utf-8") == TEXT and list(tmp_path.iterdir()) == [target] and caplog.records == []
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_failure_other_than_chmod_is_still_a_failure_for_a_file_better_kept_than_kept_private(tmp_path, monkeypatch):
    def full(source, destination):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError, match="No space left"):
        write_private_where_allowed(tmp_path / "config.reviewed.json", TEXT)
    assert list(tmp_path.iterdir()) == []


@POSIX_ONLY
@pytest.mark.parametrize("umask", [0o022, 0o000], indirect=True)
def test_a_private_file_is_private_from_the_moment_it_exists(tmp_path, monkeypatch, umask):
    """Created at the umask's mode and made private after, a key would be readable by others for as long as it takes to
    write it: it is created private, and chmod only confirms it."""
    seen, chmod, replace = {}, os.chmod, os.replace

    def looking_before_chmod(path, mode, *args, **kwargs):
        seen["when chmod is asked"] = stat.S_IMODE(os.stat(path).st_mode)
        chmod(path, mode, *args, **kwargs)

    def looking_before_replace(source, destination):
        seen["when replaced"] = stat.S_IMODE(os.stat(source).st_mode)
        replace(source, destination)

    monkeypatch.setattr(os, "chmod", looking_before_chmod)
    monkeypatch.setattr(os, "replace", looking_before_replace)
    target = tmp_path / "root.key"
    write_atomic(target, TEXT, private=True)
    assert seen == {"when chmod is asked": 0o600, "when replaced": 0o600}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600 and list(tmp_path.iterdir()) == [target]


@POSIX_ONLY
@pytest.mark.parametrize("umask", [0o022], indirect=True)
def test_a_write_that_is_not_private_is_as_before_the_umask_decides_and_chmod_is_not_asked(
    tmp_path, monkeypatch, umask
):
    seen, replace = {}, os.replace

    def looking_before_replace(source, destination):
        seen["when replaced"] = stat.S_IMODE(os.stat(source).st_mode)
        replace(source, destination)

    def not_asked(path, mode, *args, **kwargs):
        raise AssertionError(f"chmod {mode:o} {path}")

    monkeypatch.setattr(os, "replace", looking_before_replace)
    monkeypatch.setattr(os, "chmod", not_asked)
    target = tmp_path / "settings.json"
    write_atomic(target, TEXT)
    assert seen == {"when replaced": 0o644} and stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.parametrize("private", [False, True])
def test_a_file_holds_its_text_byte_for_byte(tmp_path, private):
    target = tmp_path / "root.key"
    write_atomic(target, "one\ntwo\n", private=private)
    assert target.read_bytes() == b"one\ntwo\n"  # no CRLF, on Windows either


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' access lists")
def test_on_windows_a_private_file_is_made_new_never_written_into_one_someone_made_at_its_name(tmp_path):
    """A file made at the temporary name first (in a folder others may write, with their access) is not written into:
    the private file is created new (CREATE_NEW), with its folder's access, not that file's."""
    from latet.storage.folders import folder_grants

    target = tmp_path / "server.token"
    planted = tmp_path / f".server.token.{os.getpid()}.tmp"
    planted.write_text("made by someone else", "utf-8")
    subprocess.run(["icacls", str(planted), "/grant", "*S-1-5-32-545:R"], check=True, capture_output=True, timeout=30)
    write_atomic(target, TEXT, private=True)
    assert target.read_text("utf-8") == TEXT and list(tmp_path.iterdir()) == [target]
    assert "S-1-5-32-545" not in {grant.sid for grant in folder_grants(target)[1]}  # Users may not read it


@pytest.mark.parametrize("private", [False, True])
def test_a_temporary_file_left_by_a_run_that_died_does_not_stop_the_write(tmp_path, private):
    (tmp_path / f".root.key.{os.getpid()}.tmp").write_text("what a run that died was writing", "utf-8")
    target = tmp_path / "root.key"
    write_atomic(target, TEXT, private=private)
    assert target.read_text("utf-8") == TEXT and list(tmp_path.iterdir()) == [target]
    if private and os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
