"""The helper that reads the latet.yaml of the repositories a computer remembers, for its daemon (``repository_io``).

One process for one check, started by the daemon as ``python -I -B <this file>``: its memory capped at 1 GiB first (a
Windows job, ``RLIMIT_AS`` elsewhere), and on Windows no process of its own; then it says it is ready, reads the list of
folders the daemon gives it on its standard input, and answers each folder's file on its standard output in its order,
then an end, and exits. Whatever a share that no longer answers makes a read wait, the daemon waits for this process no
longer than its deadlines, and ends it.

Every message is a frame: four bytes of length (big-endian), then that many bytes of a JSON object, at most
``FRAME_MOST``. The daemon gives no command but the one check, and takes nothing but these frames: no folder in a
command line, an environment variable or a log line. Imported by the daemon too, for the frames' shape; it imports
nothing of Latet until it runs as the helper.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from collections.abc import Callable
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from latet.project_file import Remembered

PROTOCOL = 1
FRAME_MOST = 2 * 1024 * 1024  # one frame, its length checked before anything is read of it
FOLDERS_MOST = 2_000  # the repositories a computer remembers at most (``state.local.REPOSITORIES_KEPT``)
FOLDER_LONGEST = 4_096  # a working directory as a client reports one
MEMORY_MOST = 1024**3  # the helper's own memory, at most
WHY_LONGEST = 64
LIMIT_FAILED = 3  # its exit code when its memory could not be capped: it read nothing
_HEADER = struct.Struct(">I")
_KINDS = ("settings", "missing", "nonregular", "invalid", "unavailable")
_job: Any = None  # the helper's Windows job, held for as long as it runs


class ProtocolError(Exception):
    """A frame that is not one: too long, cut short, not a JSON object, not of the check's shape. Fixed words."""


def encode(frame: dict[str, Any]) -> bytes:
    data = json.dumps(frame, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if not 0 < len(data) <= FRAME_MOST:
        raise ProtocolError("a frame too long")
    return _HEADER.pack(len(data)) + data


def _exactly(stream: IO[bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < size:
        chunk = stream.read(size - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(stream: IO[bytes]) -> dict[str, Any] | None:
    """The next frame of ``stream``; None at its end, between two frames. Its length is checked before its bytes are
    read: never more than ``FRAME_MOST`` of them."""
    return read_sized(stream)[0]


def read_sized(stream: IO[bytes]) -> tuple[dict[str, Any] | None, int]:
    """``read_frame``'s frame, and how many bytes it took."""
    header = _exactly(stream, _HEADER.size)
    if not header:
        return None, 0
    if len(header) != _HEADER.size:
        raise ProtocolError("a frame cut short")
    (size,) = _HEADER.unpack(header)
    if not 0 < size <= FRAME_MOST:
        raise ProtocolError("a frame too long")
    data = _exactly(stream, size)
    if len(data) != size:
        raise ProtocolError("a frame cut short")
    try:
        frame = json.loads(data)
    except (ValueError, RecursionError):
        raise ProtocolError("a frame that is not JSON") from None
    if not isinstance(frame, dict):
        raise ProtocolError("a frame that is not an object")
    return frame, _HEADER.size + size


def a_folder(folder: Any) -> bool:
    """Whether ``folder`` is one a check may name: a text of ``FOLDER_LONGEST`` characters at most, absolute, no NUL."""
    return (
        isinstance(folder, str) and 0 < len(folder) <= FOLDER_LONGEST and "\x00" not in folder and os.path.isabs(folder)
    )


def manifest(folders: list[str]) -> bytes:
    """The check's one command, as a frame: refused (``ProtocolError``) when a folder is not one a check may name or
    the list is longer than a computer remembers, before any helper is started."""
    if len(folders) > FOLDERS_MOST or not all(a_folder(folder) for folder in folders):
        raise ProtocolError("not a list of folders")
    return encode({"op": "scan", "folders": folders})


def _folders(frame: dict[str, Any] | None) -> list[str]:
    if frame is None or set(frame) != {"op", "folders"} or frame["op"] != "scan":
        raise ProtocolError("not the check")
    folders = frame["folders"]
    if not isinstance(folders, list) or len(folders) > FOLDERS_MOST or not all(a_folder(f) for f in folders):
        raise ProtocolError("not a list of folders")
    return folders


def item(index: int, read: Remembered) -> dict[str, Any]:
    """One folder's answer: its place, what its file came to, what it sets (JSON), why there is nothing to weigh."""
    frame: dict[str, Any] = {"i": index, "kind": read.kind}
    if read.why:
        frame["why"] = read.why[:WHY_LONGEST]
    if read.kind == "settings" and read.settings is not None:
        try:
            frame["settings"] = read.settings.model_dump(mode="json", exclude_defaults=True)
            encode(frame)
        except Exception:  # what JSON cannot hold, or a frame too long: not weighed, and never as nothing
            return {"i": index, "kind": "unavailable", "why": "not sendable"}
    return frame


def serve(reader: IO[bytes], writer: IO[bytes], read_one: Callable[[str], Remembered], *, capped: bool = False) -> int:
    """The helper's work, once its memory is capped: ready (and whether it is capped, ``capped``), the check read,
    each folder answered in order, the end."""
    writer.write(encode({"ready": PROTOCOL, "cap": capped}))
    writer.flush()
    try:
        folders = _folders(read_frame(reader))
    except ProtocolError:
        return 2
    for index, folder in enumerate(folders):
        writer.write(encode(item(index, read_one(folder))))
        writer.flush()
    writer.write(encode({"end": len(folders)}))
    writer.flush()
    return 0


def _windows_job() -> bool:
    """This process put in a job of its own: its memory capped at ``MEMORY_MOST``, and no other process in it (it
    starts none)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _Io(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong) for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
        )

    class _Extended(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _Io),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = _Extended()
    # JOB_OBJECT_LIMIT_ACTIVE_PROCESS, _PROCESS_MEMORY, _DIE_ON_UNHANDLED_EXCEPTION
    limits.BasicLimitInformation.LimitFlags = 0x8 | 0x100 | 0x400
    limits.BasicLimitInformation.ActiveProcessLimit = 1
    limits.ProcessMemoryLimit = MEMORY_MOST
    extended = 9  # JobObjectExtendedLimitInformation
    if not kernel32.SetInformationJobObject(job, extended, ctypes.byref(limits), ctypes.sizeof(limits)):
        return False
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        return False
    global _job
    _job = job  # held while the process runs: its end closes the job
    return True


def limit_memory() -> bool | None:
    """This process's memory capped at ``MEMORY_MOST`` (a system's own lower cap kept): True when it is, False when it
    could not be (the helper then reads nothing: no helper runs uncapped where a cap is promised). Windows: a job of
    its own; Linux and the other POSIX systems: its address space (``RLIMIT_AS``). macOS does not enforce
    ``RLIMIT_AS``: None, no cap claimed there, the helper bounded by the daemon's deadlines alone (R23)."""
    if os.name == "nt":
        return _windows_job()
    if sys.platform == "darwin":
        return None
    try:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        most = MEMORY_MOST if hard == resource.RLIM_INFINITY else min(MEMORY_MOST, hard)
        resource.setrlimit(resource.RLIMIT_AS, (most, most))
    except (ImportError, OSError, ValueError):
        return False
    return True


def run(reader: IO[bytes], writer: IO[bytes], reading: Callable[[], Callable[[str], Remembered] | None]) -> int:
    """The helper's whole run: its memory capped first, before anything is read (``LIMIT_FAILED`` when a cap that is
    promised could not be set); then ``reading()`` gives what reads a folder (None: it may not run, exit 2), and the
    check is served, saying whether it is capped."""
    capped = limit_memory()
    if capped is False:
        return LIMIT_FAILED
    read_one = reading()
    if read_one is None:
        return 2
    return serve(reader, writer, read_one, capped=capped is True)


def _reading() -> Callable[[str], Remembered] | None:
    """What reads a folder's latet.yaml: this helper's own Latet, from where it is installed (never the current folder
    or a variable); None when another Latet comes first on the path, checked before any of its modules runs."""
    package = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(package)
    if root not in sys.path:
        sys.path.append(root)
    import importlib.util

    found = importlib.util.find_spec("latet")  # where it would come from, nothing of it run yet
    if found is None or found.origin is None or os.path.dirname(os.path.abspath(found.origin)) != package:
        return None
    from latet.fileio import own_file
    from latet.project_file import _project_settings, read_remembered

    return lambda folder: read_remembered(folder, own_file, _project_settings)


def main() -> int:
    return run(sys.stdin.buffer, sys.stdout.buffer, _reading)


if __name__ == "__main__":
    sys.exit(main())
