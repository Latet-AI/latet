"""Shared machinery for agent integrations: backed-up, revertible edits of the agents' config files."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from latet.config import data_dir, write_atomic
from latet.storage import folders

RECORD_BYTES = 16 * 1024 * 1024
RECORD_DEPTH = 32
RECORD_NODES = 100_000


class RecordsUnavailable(RuntimeError):
    """Undo history cannot safely be used; never an empty installation or a fallback credential source. ``reason``:
    the fixed reason; ``said``: words that name the folder or file that failed, why and the fix (a folder, an account
    and its rights, a mode: never a secret), None when there are none."""

    def __init__(self, message: str, reason: str = "", said: str | None = None) -> None:
        super().__init__(message)
        self.reason, self.said = reason, said


def _unavailable(reason: str, said: str | None = None) -> RecordsUnavailable:
    if said is not None:
        return RecordsUnavailable(
            f"Integration records unavailable ({reason}): {said}; with a new data folder, `latet agents apply` "
            "applies your agents again.",
            reason,
            said,
        )
    return RecordsUnavailable(
        f"Integration records unavailable ({reason}): use a new private data folder (LATET_DATA_DIR), "
        "then explicitly reapply.",
        reason,
    )


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise _unavailable("metadata unavailable", _fixed(f"{path} cannot be inspected", None)) from None


def _metadata_or(path: Path, fix: str | None) -> os.stat_result | None:
    """``_metadata``, its refusal with the caller's own fix (``fix``) when it has one."""
    if fix is None:
        return _metadata(path)
    try:
        return _metadata(path)
    except RecordsUnavailable as exc:
        raise _unavailable(exc.reason, _fixed(f"{path} cannot be inspected", fix)) from None


def _fixed(reason: str, fix: str | None) -> str:
    """``reason`` with a data folder's fix, or the caller's own (``fix``; "" for none: it says its own)."""
    chosen = _OWN if fix is None else fix
    return f"{reason}: {chosen}" if chosen else reason


_OWN = "use a private data folder of your own (LATET_DATA_DIR)"


def _plain_path(path: Path, *, directory: bool = False, fix: str | None = None) -> os.stat_result:
    found = _metadata_or(path, fix)
    if found is None:
        raise _unavailable("metadata unavailable", f"{path} went away while Latet inspected it")
    linked = stat.S_ISLNK(found.st_mode) or bool(getattr(found, "st_file_attributes", 0) & 0x400)
    if linked or (directory and not stat.S_ISDIR(found.st_mode)):
        what = "a link or a junction" if linked else "not a folder"
        said = _fixed(f"{path} is {what}, which Latet does not go through", fix)
        raise _unavailable("unsafe folder or link", said)
    return found


def _connected() -> bool:
    """Whether this computer is connected to a Latet server: a connection file is there (R1)."""
    from latet.server.connection import connection_present

    return connection_present()


def _normal(path: Path) -> Path:
    """``path`` as a comparison reads it, never as a check inspects it: absolute, its `..` folded and its case as the
    system compares it."""
    return Path(os.path.normcase(os.path.abspath(path)))


def _gone_past(parent: Path, folder: Path, problem: str, connected: bool | None, *, above: bool) -> bool:
    """Whether ``parent``, a component of ``folder`` that another account may change (``problem``), is one Latet goes
    on past (R28): in local mode (no connection file; ``connected`` decides when it is given), a folder strictly above
    Latet's data folder (``above``) whose access list or mode lets another account change it, where `latet doctor`
    warns. Never ``folder`` itself, the data folder, a folder in it or any other folder, nor a folder another account
    owns or whose entries Latet cannot read; on a connected computer nothing: the server's token is at stake there."""
    if problem != folders.CHANGEABLE or parent == folder or not above:
        return False
    return not (_connected() if connected is None else connected)


def _namespace(
    folder: Path,
    *,
    create: bool = False,
    fix: str | None = None,
    connected: bool | None = None,
    data_folder: Path | None = None,
) -> bool:
    """Whether every component of ``folder`` is there (``create``: made, one at a time, 0700), each a plain folder no
    other account may change. Raises RecordsUnavailable naming the component that failed, why, and a data folder's fix
    (``fix``: the caller's own; "" for none). In local mode a folder above the data folder that another account may
    change by its access list or mode is gone past, and said by `latet doctor` (R28, ``_gone_past``); ``connected``:
    True for what guards a connection (the token's folders, the pinned certificate, the helper), whether or not a
    connection file is there yet; None: as this computer is. The data folder and every folder in it are checked with no
    trust in another account (M3): known by their path, compared as the system compares it, or by the data folder's
    identity, so that another spelling of it (a short name, a substituted drive) is never taken for another folder.
    ``data_folder``: the data folder, where the caller knows it better than LATET_DATA_DIR does (the helper reads its
    binding while that names its own empty cwd)."""
    # Never resolve links or normalize away a parent traversal before inspecting the actual chain.
    if not folder.is_absolute():
        folder = Path.cwd() / folder
    if not folder.is_absolute():
        raise _unavailable("unsafe folder")
    data = _normal(data_folder if data_folder is not None else data_dir())
    try:
        held = data.lstat()
        identity = (held.st_dev, held.st_ino) if held.st_ino else None
    except OSError:
        identity = None
    inside = False
    for parent in reversed((folder, *folder.parents)):
        if _metadata_or(parent, fix) is None:
            if not create:
                return False
            try:
                parent.mkdir(mode=0o700)  # One missing component only, beneath the already inspected parent.
            except FileExistsError:
                pass  # A racing creator supplied an object: inspect it before any descent, never adopt a link.
            except OSError as exc:
                said = f"{parent} cannot be made ({exc.strerror or 'refused'})"
                raise _unavailable("folder unavailable", said) from None
        found = _plain_path(parent, directory=True, fix=fix)
        normal = _normal(parent)
        inside = inside or normal == data or data in normal.parents or (found.st_dev, found.st_ino) == identity
        problem = (
            folders.namespace_folder_problem(parent, found, trust=False)
            if inside
            else folders.namespace_folder_problem(parent, found)
        )
        if problem is not None:
            if _gone_past(parent, folder, problem, connected, above=not inside and normal in data.parents):
                continue  # local mode: `latet doctor` warns (``folders.namespace_warning``)
            said = folders.namespace_refusal(parent, found, private=parent == folder, fix=fix, trust=not inside)
            raise _unavailable("unsafe folder namespace", said or _fixed(f"{parent}: {problem}", fix))
    return True


def _trusted_folder(folder: Path, *, connected: bool | None = None, data_folder: Path | None = None) -> None:
    # Inspect metadata only; never repair permissions or enter an unsafe record tree (Latet's own data folder is
    # tightened before, where nobody else could have written into it: ``folders.tighten_folder``).
    if not _namespace(folder, connected=connected, data_folder=data_folder):
        raise _unavailable("metadata unavailable", f"{folder} is not there")
    if (problem := folders.private_folder_problem(folder)) is not None:
        raise _unavailable("unsafe folder", problem)


def _store_exists() -> bool:
    folder = data_dir()
    if not _namespace(folder):
        return False
    folders.tighten_folder(folder)  # an older Latet's 0755: the first time it is needed private (R2)
    _trusted_folder(folder)
    return True


def _checked_bytes(
    path: Path,
    most: int | None = None,
    *,
    connected: bool | None = None,
    data_folder: Path | None = None,
    folder_checked: bool = False,
) -> bytes:
    """``path``'s bytes, never through a link nor from a file swapped while read. ``folder_checked``: the caller
    checked the folder with a rule of its own (the doctor's for an agent's settings), not Latet's for its secrets."""
    if not folder_checked:
        _trusted_folder(path.parent, connected=connected, data_folder=data_folder)
    before = _plain_path(path)
    if not stat.S_ISREG(before.st_mode):
        raise _unavailable("nonregular file", f"{path} is not a plain file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise _unavailable("file changed during inspection", f"{path} changed while Latet inspected it")
            return stream.read() if most is None else stream.read(most)
    except OSError as exc:
        raise _unavailable("file unreadable", f"{path} cannot be read ({exc.strerror or 'refused'})") from None


def checked_backup(path: Path, *, most: int | None = None) -> bytes:
    """Read a record-selected backup only after the store and its actual path have passed the same trust boundary."""
    if not _store_exists():
        raise _unavailable("missing store for backup")
    return _checked_bytes(path, most)


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


@dataclass
class FileEdit:
    path: str
    backup: str | None  # None when Latet created the file
    sha_before: str | None
    sha_after: str | None


@dataclass
class Record:
    integration: str
    applied_at: float
    files: list[FileEdit] = field(default_factory=list)
    data: dict = field(default_factory=dict)  # integration-specific (what was set, for surgical undo)


@dataclass
class AgentInfo:
    id: str
    name: str
    installed: bool
    version: str | None = None
    config: str | None = None
    notes: list[str] = field(default_factory=list)


def manifest_path() -> Path:
    return data_dir() / "integrations.json"


def load_records() -> dict[str, Record]:
    if not _store_exists():
        return {}
    path = manifest_path()
    if _metadata(path) is None:
        return {}
    return _parse_records(_checked_bytes(path, RECORD_BYTES + 1))


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _unavailable("duplicate member")
        result[key] = value
    return result


def _parse_records(raw: bytes) -> dict[str, Record]:
    if len(raw) > RECORD_BYTES:
        raise _unavailable("byte limit")
    # JSON's container depth is checked before its parser or dataclass reconstruction can recurse.
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > RECORD_DEPTH:
                raise _unavailable("nesting limit")
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        pending, nodes = [value], 1
        while pending:
            item = pending.pop()
            children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            for child in children:
                nodes += 1
                if nodes > RECORD_NODES:
                    raise _unavailable("node limit")
                pending.append(child)
        if not isinstance(value, dict):
            raise _unavailable("invalid manifest")
        out = {}
        for key, row in value.items():
            if not isinstance(row, dict) or row.get("integration") != key or not isinstance(key, str):
                raise _unavailable("invalid record")
            stamp = row.get("applied_at")
            if isinstance(stamp, bool) or not isinstance(stamp, int | float) or not math.isfinite(stamp):
                raise _unavailable("invalid timestamp")
            files, data = row.get("files", []), row.get("data", {})
            if not isinstance(files, list) or not isinstance(data, dict):
                raise _unavailable("invalid record shape")
            edits = []
            for edit in files:
                if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
                    raise _unavailable("invalid file edit")
                if any(
                    edit.get(name) is not None and not isinstance(edit[name], str)
                    for name in ("backup", "sha_before", "sha_after")
                ):
                    raise _unavailable("invalid file edit")
                edits.append(FileEdit(**edit))
            record = Record(**{**row, "files": edits, "data": data})
            out[key] = record
        return out
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise _unavailable("malformed manifest") from None


def records_preflight(*, write: bool = False) -> None:
    """Validate before an integration reads history or starts side effects; missing diagnostics stay read-only."""
    load_records()
    if write:
        folder = data_dir()
        _namespace(folder, create=True)
        folders.tighten_folder(folder)
        _trusted_folder(folder)


def save_records(records: dict[str, Record]) -> None:
    """Latet's record of what it applied, private: on a computer connected to a Latet server it holds the address of
    the identity's space (what was written, Codex's edits), a secret like the token."""
    records_preflight(write=True)
    text = json.dumps({k: asdict(v) for k, v in records.items()}, indent=2)
    _parse_records(text.encode("utf-8"))
    _trusted_folder(data_dir())
    write_atomic(manifest_path(), text, private=True)


def read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def backup(path: Path) -> tuple[str | None, str | None]:
    """Copy ``path`` aside before Latet edits it. Returns (backup path, sha256)."""
    records_preflight(write=True)
    data = read_bytes(path)
    if data is None:
        return None, None
    root = data_dir() / "backups"
    root.mkdir(mode=0o700, exist_ok=True)
    _trusted_folder(root)
    folder = root / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(mode=0o700, exist_ok=True)
    _trusted_folder(folder)
    target = folder / f"{path.name}.{hashlib.sha256(str(path).encode()).hexdigest()[:8]}"
    shutil.copy2(path, target)
    return str(target), _sha(data)


def edit_text(path: Path, transform: Callable[[str], str], encoding: str = "utf-8", private: bool = False) -> FileEdit:
    """Edit ``path`` through ``transform`` (missing file → ""), keeping its line endings. ``private``: written whole
    and new, your own alone (0600 on POSIX), whatever mode it had, and never through a link (which the caller refused
    before: the link would be replaced)."""
    backup_path, sha_before = backup(path)
    raw = read_bytes(path)
    text = raw.decode(encoding) if raw is not None else ""
    crlf = "\r\n" in text
    new = transform(text.replace("\r\n", "\n"))
    if crlf:
        new = new.replace("\n", "\r\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = new.encode(encoding)
    if private:
        write_atomic(path, new, private=True)
    else:
        path.write_bytes(data)
    return FileEdit(str(path), backup_path, sha_before, _sha(data))


def restore(edit: FileEdit, surgical: Callable[[str], str] | None = None) -> str:
    """Undo one edit: byte-for-byte when the file is as Latet left it, else surgically."""
    records_preflight(write=True)
    restored = checked_backup(Path(edit.backup)) if edit.backup is not None else None
    path = Path(edit.path)
    current = read_bytes(path)
    if _sha(current) == edit.sha_after:
        if edit.backup is None:
            path.unlink(missing_ok=True)
            return "removed"
        path.write_bytes(restored if restored is not None else b"")
        return "restored"
    if current is None:
        return "missing"
    if surgical is None:
        return "drifted"
    text = current.decode("utf-8")
    crlf = "\r\n" in text
    new = surgical(text.replace("\r\n", "\n"))
    if crlf:
        new = new.replace("\n", "\r\n")
    path.write_bytes(new.encode("utf-8"))
    return "drifted-undone"
