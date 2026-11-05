"""Bounded manifest publication, with no fuzz processes or database allocation."""

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_task8_benchmark_measurements import helper


def clock_for(driver, monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return clock


@pytest.mark.parametrize("denials", [1, 3])
def test_manifest_retries_transient_replace_without_exposing_partial_state(tmp_path, monkeypatch, denials):
    driver = helper("task8-fuzz-batch.py")
    clock = clock_for(driver, monkeypatch)
    path = tmp_path / "manifest.json"
    previous = {"phase": "running"}
    pending = {"phase": "finished", "failed": "interrupted", "counts": {"errors": 1}}
    path.write_text(json.dumps(previous))
    replace = Path.replace
    attempts = []

    def replacing(temporary, target):
        attempts.append(clock.now)
        assert target == path
        assert json.loads(path.read_text()) == previous
        assert json.loads(temporary.read_text()) == pending
        if len(attempts) <= denials:
            raise PermissionError("synthetic sharing denial")
        return replace(temporary, target)

    monkeypatch.setattr(Path, "replace", replacing)
    driver.save_manifest(path, pending)
    assert json.loads(path.read_text()) == pending
    assert not path.with_suffix(".tmp").exists()
    assert len(attempts) == denials + 1
    assert len(clock.sleeps) == denials
    assert all(0 < duration <= 0.02 for duration in clock.sleeps)


def test_manifest_persistent_denial_expires_and_preserves_old_and_failed_states(tmp_path, monkeypatch):
    driver = helper("task8-fuzz-batch.py")
    clock = clock_for(driver, monkeypatch)
    path = tmp_path / "manifest.json"
    path.write_text('{"phase":"running"}')
    pending = {"phase": "finished", "failed": "past 3600 s, stopped"}
    error = PermissionError("persistent sharing denial")
    attempts = []

    def replacing(temporary, target):
        attempts.append(clock.now)
        raise error

    monkeypatch.setattr(Path, "replace", replacing)
    with pytest.raises(PermissionError, match="persistent sharing denial") as caught:
        driver.save_manifest(path, pending)
    assert caught.value is error
    assert 0.99 <= clock.now <= 1.0
    assert len(attempts) > 1
    assert all(0 < duration <= 0.02 for duration in clock.sleeps)
    assert json.loads(path.read_text()) == {"phase": "running"}
    assert json.loads(path.with_suffix(".tmp").read_text()) == pending
    assert pending["failed"] == "past 3600 s, stopped"


def test_manifest_nonpermission_error_propagates_without_retry(tmp_path, monkeypatch):
    driver = helper("task8-fuzz-batch.py")
    clock = clock_for(driver, monkeypatch)
    path = tmp_path / "manifest.json"
    path.write_text('{"phase":"running"}')
    error = OSError(28, "synthetic disk full")
    calls = []

    def replacing(temporary, target):
        calls.append((temporary, target))
        raise error

    monkeypatch.setattr(Path, "replace", replacing)
    with pytest.raises(OSError, match="synthetic disk full") as caught:
        driver.save_manifest(path, {"phase": "failed"})
    assert caught.value is error
    assert len(calls) == 1
    assert clock.sleeps == []
    assert json.loads(path.read_text()) == {"phase": "running"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share-delete denial")
def test_manifest_recovers_after_real_windows_reader_releases_delete_denial(tmp_path, monkeypatch):
    driver = helper("task8-fuzz-batch.py")
    path = tmp_path / "manifest.json"
    path.write_text('{"phase":"running"}')
    temporary = path.with_suffix(".tmp")
    temporary.write_text('{"phase":"finished"}')
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    sleeps = []

    def release(seconds):
        nonlocal handle
        sleeps.append(seconds)
        assert kernel.CloseHandle(handle)
        handle = None

    try:
        with pytest.raises(PermissionError):
            temporary.replace(path)
        monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=release))
        driver.save_manifest(path, {"phase": "finished", "failed": "interrupted"})
        assert len(sleeps) == 1
        assert 0 < sleeps[0] <= 0.02
        assert json.loads(path.read_text()) == {"phase": "finished", "failed": "interrupted"}
        assert not temporary.exists()
    finally:
        if handle is not None:
            assert kernel.CloseHandle(handle)
