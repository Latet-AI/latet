"""Bounded regular-file bytes; callers own text formats and filesystem deadlines."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Literal

ReadFailure = Literal["missing", "nonregular", "too_large", "unavailable"]

# Windows: a file opened on itself, never on what a link in its place names (CreateFileW's flags). Opened so, a symbolic
# link or a junction swapped in after the look is the link itself on the handle, whose reparse tag says so: refused
# there. A reparse point that is no link (a cloud placeholder, a compressed or deduplicated file) is read as itself.
_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete: no lock on a file another program writes
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000  # a folder opens too (and is refused as one), as os.stat opens it
_NAME_SURROGATE = 0x20000000  # a reparse tag's bit that marks a link: a symbolic link, a junction


class _LinkInPlace(OSError):
    """A link, or another file, where the file opened without following one was looked at: never read."""


def _attribute_tag(handle: int) -> tuple[int, int]:
    """The attributes and reparse tag of the file a Windows handle holds (``FileAttributeTagInfo``)."""
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "Windows only")
    import ctypes
    from ctypes import wintypes

    class _Info(ctypes.Structure):
        _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    query.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    query.restype = wintypes.BOOL
    info = _Info()
    if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # FileAttributeTagInfo
        raise ctypes.WinError(ctypes.get_last_error())
    return info.FileAttributes, info.ReparseTag


def _same_file(first: int, second: int) -> bool:
    """Whether two descriptors hold the very same file (its volume and its index)."""
    one, other = os.fstat(first), os.fstat(second)
    return (one.st_dev, one.st_ino) == (other.st_dev, other.st_ino)


def _open_no_follow_windows(path: str | os.PathLike[str]) -> int:
    """A descriptor of ``path`` itself, never of what a link there names: Windows has no ``O_NOFOLLOW``, and opens the
    reparse point itself with ``FILE_FLAG_OPEN_REPARSE_POINT``. A link there (its tag a name surrogate) raises
    ``_LinkInPlace``; another reparse point is opened again as the file it is, and must be the same file. Raises
    ``OSError`` as ``os.open`` does."""
    if sys.platform != "win32":
        raise OSError(errno.ENOSYS, "Windows only")
    import _winapi
    import msvcrt

    def opened(flags: int) -> int:
        handle = _winapi.CreateFile(os.fspath(path), _GENERIC_READ, _SHARE_ALL, 0, _OPEN_EXISTING, flags, 0)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except BaseException:
            _winapi.CloseHandle(handle)
            raise

    first = opened(_OPEN_REPARSE_POINT | _BACKUP_SEMANTICS)
    try:
        attributes, tag = _attribute_tag(msvcrt.get_osfhandle(first))
        if not attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return first
        if tag & _NAME_SURROGATE:
            raise _LinkInPlace(errno.ELOOP, "a link in its place")
        second = opened(_BACKUP_SEMANTICS)  # the file it is, read through its own reparse point
    except BaseException:
        os.close(first)
        raise
    try:
        if not _same_file(first, second):
            raise _LinkInPlace(errno.ELOOP, "another file in its place")
    except BaseException:
        os.close(second)
        raise
    finally:
        os.close(first)
    return second


# The platform's atomic way to open a file without following a link in its place: ``O_NOFOLLOW`` where the system has
# it (looked for at each read), Windows' own above; None where neither: a read that must not follow is then refused
# before anything is opened.
_windows_no_follow: Callable[[str | os.PathLike[str]], int] | None = (
    _open_no_follow_windows if os.name == "nt" else None
)


def _no_follow_opener() -> Callable[[str | os.PathLike[str]], int] | None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | nofollow
        return lambda path: os.open(path, flags)
    return _windows_no_follow


class FileReadError(RuntimeError):
    """A fixed failure category, retaining neither a path nor an underlying exception."""

    def __init__(self, reason: ReadFailure, limit: int) -> None:
        self.reason: ReadFailure = reason
        words = {
            "missing": "cannot be read",
            "nonregular": "cannot be read: it is not a regular file",
            "too_large": f"holds more than {limit} bytes",
            "unavailable": "cannot be read",
        }
        super().__init__(words[reason])


def _windows_device(path: str | os.PathLike[str]) -> bool:
    if os.name != "nt":
        return False
    text = os.fspath(path).replace("/", "\\")
    if text.upper().startswith(("\\\\.\\", "\\??\\", "\\\\?\\GLOBALROOT\\", "\\\\?\\PIPE\\")):
        return True
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(
        f"{prefix}{digit}"
        for prefix in ("COM", "LPT")
        for digit in "123456789\N{SUPERSCRIPT ONE}\N{SUPERSCRIPT TWO}\N{SUPERSCRIPT THREE}"
    )
    return any(
        part.rstrip(" .").split(".", 1)[0].split(":", 1)[0].upper() in reserved for part in PureWindowsPath(text).parts
    )


def read_regular_bytes(path: str | os.PathLike[str], *, limit: int, follow: bool) -> bytes:
    """``read_regular_snapshot``'s bytes alone."""
    return read_regular_snapshot(path, limit=limit, follow=follow)[0]


def read_regular_snapshot(path: str | os.PathLike[str], *, limit: int, follow: bool) -> tuple[bytes, os.stat_result]:
    """Read at most ``limit`` bytes, refusing special/oversized files before and after open.

    Links to regular files are allowed only with ``follow=True``. Nonblocking opens prevent
    a POSIX FIFO path swap from waiting for a writer. Without ``follow`` the file is opened
    atomically on itself (``O_NOFOLLOW``; on Windows its reparse point, refused once open),
    and refused before any open where the platform has no such way. Arbitrary filesystem
    latency still needs a caller-owned lifecycle deadline; no worker or unrestricted fallback
    is added. With the bytes, the file's status as it was opened (``os.fstat``).
    """
    if limit <= 0:
        raise ValueError("the byte ceiling must be positive")
    problem: ReadFailure | None = None
    descriptor = None
    opened: os.stat_result | None = None
    content = bytearray()
    try:
        if _windows_device(path):
            problem = "nonregular"
        else:
            before = os.stat(path, follow_symlinks=follow)
            opener = None if follow else _no_follow_opener()
            if not stat.S_ISREG(before.st_mode):
                problem = "nonregular"
            elif before.st_size > limit:
                problem = "too_large"
            elif not follow and opener is None:
                problem = "unavailable"
            else:
                if opener is None:
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
                else:
                    descriptor = opener(path)
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    problem = "nonregular"
                elif opened.st_size > limit:
                    problem = "too_large"
                else:
                    while len(content) <= limit:
                        chunk = os.read(descriptor, limit + 1 - len(content))
                        if not chunk:
                            break
                        content.extend(chunk)
                    if len(content) > limit:
                        problem = "too_large"
    except FileNotFoundError:
        problem = "missing" if descriptor is None else "unavailable"
    except OSError as error:  # ELOOP without following: a link (``O_NOFOLLOW``, or ``_LinkInPlace`` on Windows)
        problem = "nonregular" if not follow and error.errno == errno.ELOOP else "unavailable"
    except ValueError:
        problem = "unavailable"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                problem = "unavailable"
    if problem or opened is None:
        raise FileReadError(problem or "unavailable", limit)
    return bytes(content), opened


def own_file(path: Path, most: int, *, follow: bool = False) -> bytes | None:
    """The bytes of ``path`` when it is a regular file of ``most`` bytes at most (``read_regular_bytes``); None when it
    is there and is not: a folder, a device, a pipe, a larger file, or, unless ``follow``, a link (a symbolic one or a
    junction), none of them read. ``follow``: through links, as a conversation's project file is read
    (``projects.read_file``). Raises ``FileNotFoundError`` when it is not there, and ``OSError`` when it cannot be read
    now (a refused permission, a sharing violation, a link loop, a platform that cannot open it without following a
    link): words of its own, never the path's."""
    try:
        return read_regular_bytes(path, limit=most, follow=follow)
    except FileReadError as failure:
        if failure.reason == "missing":
            raise FileNotFoundError(errno.ENOENT, "it is not there") from None
        if failure.reason == "unavailable":
            raise OSError(errno.EIO, "it cannot be read now") from None
        return None
