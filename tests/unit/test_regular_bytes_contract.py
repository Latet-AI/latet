"""Neutral EOP-31/ER.1 contract, separate from existing-consumer security RED.

The proposed helper interface is not implemented at this test-only checkpoint.
A missing interface is a setup failure, never evidence that a security guard failed.
Native probes are opt-in and also require an independently supervised isolated process.
"""

import importlib
import os
import signal
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import pytest


def _api():
    # Keep baseline consumer-test collection independent of the not-yet-added module.
    module = importlib.import_module("latet.fileio")
    return module.read_regular_bytes, module.FileReadError


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [("missing", "missing"), ("directory", "nonregular"), ("large", "too_large"), ("unreadable", "unavailable")],
)
def test_typed_failures_distinguish_absence_nonregular_size_and_uncertainty(tmp_path, monkeypatch, fixture, reason):
    read, failure = _api()
    path = tmp_path / "eop31-private-path"
    if fixture == "directory":
        path.mkdir()
    elif fixture == "large":
        path.write_bytes(b"12345")
    elif fixture == "unreadable":
        path.write_bytes(b"1234")

        def refused(*args, **kwargs):
            raise PermissionError("eop31-private-error", str(path))

        monkeypatch.setattr(os, "open", refused)
    with pytest.raises(failure) as caught:
        read(path, limit=4, follow=True)
    assert caught.value.reason == reason
    shown = "".join(traceback.format_exception(caught.value))
    assert str(path) not in shown and "eop31-private-error" not in shown
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_primitive_returns_exact_arbitrary_bytes_without_parsing_secret_formats(tmp_path):
    read, _ = _api()
    path = tmp_path / "bytes"
    raw = b"\x00\xff\xef\xbb\xbf\r\n \t"
    path.write_bytes(raw)
    assert read(path, limit=10, follow=True) == raw


@pytest.mark.parametrize("limit", [0, -1])
def test_primitive_requires_a_positive_explicit_byte_ceiling(tmp_path, limit):
    read, _ = _api()
    path = tmp_path / "bytes"
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="positive"):
        read(path, limit=limit, follow=True)


def test_explicit_no_follow_refuses_a_link_while_follow_reads_its_regular_target(tmp_path):
    read, failure = _api()
    target, path = tmp_path / "target", tmp_path / "link"
    target.write_bytes(b"exact bytes")
    try:
        path.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("creating Windows symbolic links requires unavailable privilege")
        raise
    assert read(path, limit=11, follow=True) == b"exact bytes"
    with pytest.raises(failure) as caught:
        read(path, limit=11, follow=False)
    assert caught.value.reason == "nonregular"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="atomic O_NOFOLLOW is unavailable on this platform")
def test_no_follow_mode_rejects_a_symlink_swapped_after_admission(tmp_path, monkeypatch):
    read, failure = _api()
    target, path = tmp_path / "target", tmp_path / "file"
    target.write_bytes(b"must not be read")
    path.write_bytes(b"original")
    real_open = os.open
    swapped = False

    def swap(found, flags, *args, **kwargs):
        nonlocal swapped
        if os.fspath(found) == os.fspath(path) and not swapped:
            path.unlink()
            path.symlink_to(target)
            swapped = True
        return real_open(found, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(failure) as caught:
        read(path, limit=64, follow=False)
    assert swapped
    assert caught.value.reason == "nonregular"


class NativeReadWatchdog(BaseException):
    """A reader's expected Exception recovery must never consume the watchdog."""


@contextmanager
def _native_watchdog():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("SIGALRM watchdog needs the main thread")
    alarm = getattr(signal, "SIGALRM", None)
    timer = getattr(signal, "ITIMER_REAL", None)
    get_timer = getattr(signal, "getitimer", None)
    set_timer = getattr(signal, "setitimer", None)
    if alarm is None or timer is None or get_timer is None or set_timer is None:
        pytest.skip("POSIX interval timer watchdog is unavailable")
    old_handler = signal.getsignal(alarm)
    old_delay, old_interval = get_timer(timer)
    if 0 < old_delay <= 5:
        pytest.skip("preserve an already armed tighter watchdog")
    started = time.monotonic()

    def expired(*args):
        raise NativeReadWatchdog("native secret read exceeded its five-second watchdog")

    signal.signal(alarm, expired)
    try:
        set_timer(timer, 5)
        yield
    finally:
        set_timer(timer, 0)
        signal.signal(alarm, old_handler)
        if old_delay:
            set_timer(timer, max(0.000001, old_delay - (time.monotonic() - started)), old_interval)


NATIVE_PROBES = os.name == "posix" and os.environ.get("EOP31_PLATFORM_PROBES") == "1"


@pytest.mark.skipif(not NATIVE_PROBES, reason="requires granted POSIX slot and independent 15-second process watchdog")
def test_real_fifo_swap_cannot_block_or_be_accepted_as_an_empty_regular_file(tmp_path, monkeypatch):
    # Run this exact primitive test, never only a higher-level timed/supervised consumer.
    read, failure = _api()
    path = tmp_path / "secret"
    path.write_bytes(b"valid regular bytes")
    real_stat = os.stat
    make_fifo = getattr(os, "mkfifo", None)
    if make_fifo is None:
        pytest.skip("POSIX FIFO creation is unavailable")
    swapped = False

    def swapped_after_stat(found, *args, **kwargs):
        nonlocal swapped
        info = real_stat(found, *args, **kwargs)
        if os.fspath(found) == os.fspath(path) and not swapped:
            path.unlink()
            make_fifo(path)
            swapped = True
        return info

    monkeypatch.setattr(os, "stat", swapped_after_stat)
    try:
        with _native_watchdog(), pytest.raises(failure) as caught:
            read(path, limit=64, follow=True)
    except NativeReadWatchdog:
        pytest.fail("O_NONBLOCK guard missing: direct primitive FIFO open blocked")
    assert swapped, "the actual neutral primitive pre-open stat seam was not exercised"
    assert caught.value.reason == "nonregular"


@pytest.mark.skipif(not NATIVE_PROBES, reason="requires granted POSIX slot and independent 15-second process watchdog")
@pytest.mark.parametrize("form", ["fifo", "device", "directory"])
def test_real_known_special_files_are_refused_under_an_independent_watchdog(tmp_path, form):
    read, failure = _api()
    if form == "fifo":
        make_fifo = getattr(os, "mkfifo", None)
        if make_fifo is None:
            pytest.skip("POSIX FIFO creation is unavailable")
        path = tmp_path / "fifo"
        make_fifo(path)
    else:
        path = Path("/dev/zero") if form == "device" else tmp_path
    if form == "device" and not path.exists():
        pytest.skip("this POSIX installation has no /dev/zero")
    try:
        with _native_watchdog(), pytest.raises(failure) as caught:
            read(path, limit=64, follow=True)
    except NativeReadWatchdog:
        pytest.fail("known special file blocked native secret read")
    assert caught.value.reason == "nonregular"
