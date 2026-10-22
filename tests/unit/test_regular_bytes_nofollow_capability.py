"""Pending safe RED: an unavailable atomic nofollow mechanism must refuse before open."""

import os

import pytest

from latet.fileio import FileReadError, read_regular_bytes


def test_no_follow_without_an_atomic_platform_mechanism_refuses_before_open(tmp_path, monkeypatch):
    from latet import fileio

    path = tmp_path / "regular"
    path.write_bytes(b"owned regular bytes")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    # Windows' own atomic way (its reparse point opened, ``fileio._open_no_follow_windows``) taken away too: a platform
    # with neither.
    monkeypatch.setattr(fileio, "_windows_no_follow", None)

    def forbidden(*args, **kwargs):
        raise AssertionError("follow=False attempted open without atomic nofollow support")

    monkeypatch.setattr(os, "open", forbidden)
    with pytest.raises(FileReadError) as caught:
        read_regular_bytes(path, limit=64, follow=False)
    assert caught.value.reason == "unavailable"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert str(path) not in str(caught.value)
    assert path.read_bytes() == b"owned regular bytes"
