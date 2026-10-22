"""A file read without following a link, on Windows too: its reparse point opened, and a link refused once open.

Windows has no ``O_NOFOLLOW``. ``fileio`` opens the file itself there (``FILE_FLAG_OPEN_REPARSE_POINT``), so that a link
put in its place after the look is the link on the handle, whose reparse tag (a name surrogate) the reader refuses; a
reparse point that is no link (a cloud placeholder, a deduplicated file) is read as itself (R24 M3). The list of the
repositories a computer remembers is read so (``LocalRepositories``): without it, that list could not be read on Windows
at all, and no repository would be remembered there.
"""

import os
import stat

import pytest

from latet import fileio
from latet.fileio import FileReadError, own_file, read_regular_bytes

windows = pytest.mark.skipif(os.name != "nt", reason="Windows' own way of opening a file without following a link")


SYMLINK, MOUNT_POINT, CLOUD, DEDUP = 0xA000000C, 0xA0000003, 0x9000001A, 0x80000013


@windows
@pytest.mark.parametrize("tag", [SYMLINK, MOUNT_POINT], ids=["symbolic-link", "junction"])
def test_a_link_seen_on_the_open_file_is_refused_whatever_the_look_said(tmp_path, monkeypatch, tag):
    """Once open, the handle's own reparse tag says whether a link took its place since the look: a name surrogate
    (a symbolic link, a junction), never read through. As a link the look did not see (one swapped in after it) would
    show itself."""
    path = tmp_path / "regular"
    path.write_bytes(b"must not be read through a link")
    monkeypatch.setattr(fileio, "_attribute_tag", lambda handle: (stat.FILE_ATTRIBUTE_REPARSE_POINT, tag))
    with pytest.raises(FileReadError) as caught:
        read_regular_bytes(path, limit=64, follow=False)
    assert caught.value.reason == "nonregular"
    assert read_regular_bytes(path, limit=64, follow=True) == b"must not be read through a link"  # following: read


@windows
@pytest.mark.parametrize("tag", [CLOUD, DEDUP], ids=["cloud-placeholder", "deduplicated"])
def test_a_reparse_point_that_is_no_link_is_read_as_the_file_it_is(tmp_path, monkeypatch, tag):
    """A cloud placeholder, a compressed or deduplicated file is a reparse point and no link: read as itself (R24 M3),
    never refused, so that a list kept in such a folder is not taken for a damaged one and written over."""
    path = tmp_path / "regular"
    path.write_bytes(b"read as itself")
    monkeypatch.setattr(fileio, "_attribute_tag", lambda handle: (stat.FILE_ATTRIBUTE_REPARSE_POINT, tag))
    assert read_regular_bytes(path, limit=64, follow=False) == b"read as itself"


@windows
def test_a_reparse_point_that_changed_between_its_two_opens_is_refused(tmp_path, monkeypatch):
    """Opened again to be read as itself, it must be the very file the first open saw: one that took its place between
    the two is refused."""
    path = tmp_path / "regular"
    path.write_bytes(b"must not be read")
    monkeypatch.setattr(fileio, "_attribute_tag", lambda handle: (stat.FILE_ATTRIBUTE_REPARSE_POINT, CLOUD))
    monkeypatch.setattr(fileio, "_same_file", lambda first, second: False)
    with pytest.raises(FileReadError) as caught:
        read_regular_bytes(path, limit=64, follow=False)
    assert caught.value.reason == "nonregular"


@windows
def test_windows_opens_the_file_itself_without_following(tmp_path, monkeypatch):
    import _winapi

    path = tmp_path / "regular"
    path.write_bytes(b"owned regular bytes")
    opened, real = [], _winapi.CreateFile

    def recording(name, access, share, security, disposition, flags, template):
        opened.append(flags)
        return real(name, access, share, security, disposition, flags, template)

    monkeypatch.setattr(_winapi, "CreateFile", recording)
    assert read_regular_bytes(path, limit=64, follow=False) == b"owned regular bytes"
    assert own_file(path, 64) == b"owned regular bytes"
    assert len(opened) == 2 and all(flags & fileio._OPEN_REPARSE_POINT for flags in opened)


@windows
def test_a_junction_put_in_the_files_place_after_its_look_is_never_read_through(tmp_path, monkeypatch):
    import _winapi

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "inside").write_bytes(b"must not be read")
    path = tmp_path / "list"
    _winapi.CreateJunction(str(elsewhere), str(path))
    real = os.stat

    def a_file_when_looked_at(found, *args, **kwargs):
        info = real(found, *args, **kwargs)
        if os.fspath(found) == os.fspath(path):
            return os.stat_result((stat.S_IFREG | 0o644, *tuple(info)[1:6], 10, *tuple(info)[7:10]))
        return info

    try:
        assert own_file(path, 64) is None  # the look sees the junction: never opened
        monkeypatch.setattr(os, "stat", a_file_when_looked_at)
        with pytest.raises(FileReadError) as caught:
            read_regular_bytes(path, limit=64, follow=False)
        assert caught.value.reason == "nonregular"
    finally:
        os.rmdir(path)  # the test's own junction alone, never what it names
