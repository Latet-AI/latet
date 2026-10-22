"""Move the two undo-history roots aside without replacing an earlier quarantine."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


def quarantine_record(folder: Path, name: str) -> None:
    """Rename one fixed leaf using the platform's exclusive operation and one anchored parent fd.

    Missing native/filesystem capability refuses; ordinary rename is never a fallback. An error closing the fd
    leaves the outcome unconfirmed, even if the rename happened. No record or backup content is opened or listed.
    """
    if name not in ("integrations.json", "backups"):
        raise ValueError("not an integration record root")
    if sys.platform == "linux":
        symbol, flag = "renameat2", 1  # Linux RENAME_NOREPLACE
    elif sys.platform == "darwin":
        symbol, flag = "renameatx_np", 4  # macOS RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, "non-overwriting quarantine unavailable")
    directory, nofollow, cloexec = (getattr(os, key, 0) for key in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"))
    if not all((directory, nofollow, cloexec)):
        raise OSError(errno.ENOTSUP, "non-overwriting quarantine unavailable")
    try:
        rename = getattr(ctypes.CDLL(None, use_errno=True), symbol)
    except (AttributeError, OSError):
        raise OSError(errno.ENOTSUP, "non-overwriting quarantine unavailable") from None
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    try:
        fd = os.open(folder, os.O_RDONLY | directory | nofollow | cloexec)
    except OSError:
        # A missing/unreadable parent does not verify absence of either selected leaf.
        raise OSError(errno.EIO, "quarantine parent unavailable") from None
    try:
        ctypes.set_errno(0)
        if rename(fd, os.fsencode(name), fd, os.fsencode(name + ".untrusted"), flag) != 0:
            raise OSError(ctypes.get_errno() or errno.EIO, "non-overwriting quarantine failed")
    finally:
        try:
            os.close(fd)
        except OSError:
            raise OSError(errno.EIO, "quarantine outcome unconfirmed") from None
