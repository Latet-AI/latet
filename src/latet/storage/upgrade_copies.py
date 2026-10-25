"""What `latet upgrade` copies before it installs a release: what the new version may migrate as it opens it.

The vault (``latet.db``), the history (``exchanges.db``) and the settings (``config.yaml``, and the settings you
approved last, ``config.reviewed.json``), as they are, into ``<data>/backups/before-<version>-<UTC time>/``: a folder
that is yours alone, with a manifest (``manifest.json``: the version that was installed, the one being installed, when,
and each file with its size and digest). The databases are copied through SQLite's backup API, a consistent copy also
while the daemon writes, never as files in use. Their values stay sealed with this computer's key, as they are in the
vault: the key is not copied, it stays where it lives (the system's credential store, or ``root.key``). A copy is made
whole or not at all: under a name of its own (``.before-....partial``), renamed once complete; one cut off by a crash
is deleted by the next prune, and never read as a backup by `latet disconnect`. At most three copies are kept
(``KEEP``): the older ones are deleted once the next is whole, and a copy of this kind alone is deleted, a folder whose
name and manifest say it is one. An upgrade with no room for the copy beside those there is refused before anything
changes (``room``).

The copies follow what you remove (``Follow``): Forget takes a value out of every copy's vault, clearing the history
clears every copy's, and the history's retention prunes every copy's, with the live files' own statements
(``db.FORGET``, ``exchanges.CLEARED`` and ``PRUNED``) and nothing else: a copy is never brought to this version's schema
(it exists to go back across a migration), and its digests are made again whenever its file changed. A copy whose
database another process holds is tried again, then left as it is, and what was asked of it is kept beside the copies
(``.asked-*.json``: ids, never a value) and done at the next pass, and before it is put back. A copy that cannot be
opened for it, or whose vault this computer's key does not open, is deleted rather than left holding what was removed
(or, where it cannot be deleted, set aside: its manifest says so, and it is never offered back), and the log says so.
So putting a copy back brings back nothing forgotten. Latet's threads and processes work on the copies one at a time
(``locked``).

Going back to the version a copy was taken under (`latet upgrade --to VERSION`), the copy can be put back
(``put_back``), once the daemon is stopped and the files as they are now are copied first: each of the copy's files,
checked against its digest, staged beside the live one, then swapped in by renames, the live one kept aside until the
last is in; on any failure the files kept aside go back, and what is in place is said exactly.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from latet.storage.folders import make_private_folder

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

FOLDER = "backups"  # in the data folder, beside the backups `latet agents apply` keeps of what it edits
KIND = "latet-upgrade-copy"
FORMAT = 1
KEEP = 3
MANIFEST = "manifest.json"
DATABASES = ("latet.db", "exchanges.db")  # the vault and the history, in the data folder
APPROVED = "config.reviewed.json"  # the settings you approved last, in the data folder
SETTINGS = "config.yaml"  # where `config_path()` says
NAMES = (*DATABASES, APPROVED, SETTINGS)
_SIDES = ("-wal", "-shm", "-journal")  # what SQLite keeps beside a database, which goes with it
# A copy's folder: the version it was taken before, and when (UTC). A version as a release names it.
_NAME = re.compile(r"before-(?P<version>[0-9][0-9A-Za-z.+-]*)-(?P<stamp>[0-9]{8}T[0-9]{6}Z)")
_PARTIAL = re.compile(r"\.before-[0-9][0-9A-Za-z.+-]*-[0-9]{8}T[0-9]{6}Z\.partial")  # a copy being made, or cut off
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]*")
# Room an upgrade leaves beside its copy: for uv's download and install, and the daemon's own writes after it.
MARGIN = 512 * 1024 * 1024
LOCK = ".copies.lock"  # beside the copies: Latet's processes work on them one at a time (``locked``)
_ASKED = re.compile(r"\.asked-[0-9a-f]{32}\.json")  # what could not be done to copies yet (``_ask_later``)
_ASKED_NEW = re.compile(r"\.\.asked-[0-9a-f]{32}\.json\.[0-9a-f]{16}\.new")  # one being written (``_write_whole``)
_LOCK_WAIT = 300.0  # seconds the CLI (`latet upgrade`) waits for another process's work on the copies
# Seconds the daemon's work on the copies (``Follow``: a Forget with the runtime's lock held, the history's prune, the
# start) waits for another's, a `latet upgrade` copying a large history say: then it keeps its work for the next pass.
_FOLLOW_WAIT = 2.0
_BUSY_SECONDS = 1.0  # how long SQLite waits, each try, on a copy's database another process holds
_TRIES = 3
_clock: Callable[[], float] = time.time


class CopyError(Exception):
    """A copy that cannot be made, read or put back, and why (never a value)."""


@dataclass(frozen=True, slots=True)
class Copy:
    folder: Path
    installed: str  # the version installed when it was taken: the one it was taken under
    installing: str  # the version being installed then
    created: str  # when, UTC, as `2026-10-25T09:12:03Z`
    files: dict[str, str]  # name -> its SHA-256

    @property
    def stamp(self) -> str:
        found = _NAME.fullmatch(self.folder.name)
        return found.group("stamp") if found else ""


_HERE = threading.RLock()  # this process's threads, one at a time on the copies
_HELD = threading.local()  # the data folders whose copies this thread holds, and how many times over


@contextlib.contextmanager
def locked(data: Path, wait: float | None = None) -> Iterator[None]:
    """Work on ``data``'s copies one at a time: this process's threads (a lock), and Latet's other processes (a lock on
    ``backups/.copies.lock``, which the system lets go when its process ends, however it ends). A thread that holds it
    takes it again at once. Raises CopyError when another has held it for ``wait`` seconds (``_LOCK_WAIT``, the CLI's;
    the daemon's ``Follow`` waits ``_FOLLOW_WAIT``, then keeps its work for later)."""
    wait = _LOCK_WAIT if wait is None else wait
    if not _HERE.acquire(timeout=wait):
        raise CopyError("the upgrade copies are in use by another of Latet's threads")
    try:
        held: dict[str, int] = _HELD.__dict__.setdefault("folders", {})
        key = os.path.normcase(os.path.abspath(data))
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        root = make_private_folder(data / FOLDER)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(root / LOCK, flags, 0o600)
        try:
            _hold(handle, wait)
            held[key] = 1
            try:
                yield
            finally:
                held.pop(key, None)
                _release(handle)
        finally:
            os.close(handle)
    finally:
        _HERE.release()


def _hold(handle: int, wait: float) -> None:
    deadline = time.monotonic() + wait
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise CopyError("the upgrade copies are in use by another Latet process") from None
            time.sleep(0.05)


def _release(handle: int) -> None:
    with contextlib.suppress(OSError):  # closing the file lets it go as well
        if sys.platform == "win32":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_whole(path: Path, text: str) -> None:
    """``text`` in ``path``, whole or not at all: written under a name of its own, then renamed into place."""
    temporary = path.with_name(f".{path.name}.{os.urandom(8).hex()}.new")
    try:
        temporary.write_text(text, encoding="utf-8")
        _private(temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sources(data: Path, config_file: Path) -> dict[str, Path]:
    """The files a copy holds, those there are, by their name in it."""
    found = {name: data / name for name in (*DATABASES, APPROVED)}
    found[SETTINGS] = config_file
    return {name: path for name, path in found.items() if path.is_file()}


def _private(path: Path) -> None:
    """A file of a copy, readable by you alone where the system has modes (on Windows it takes its folder's access)."""
    if os.name != "nt":
        path.chmod(0o600)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            sha.update(block)
    return sha.hexdigest()


def _backup(source: Path, target: Path) -> None:
    """``source``'s database into ``target`` through SQLite's backup API: one consistent state of it."""
    origin = sqlite3.connect(source, isolation_level=None)
    try:
        copy = sqlite3.connect(target, isolation_level=None)
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()
    _private(target)


def _size(path: Path) -> int:
    """A file's bytes, a database's with its journal beside it (what its copy may take)."""
    total = path.stat().st_size if path.is_file() else 0
    if path.name in DATABASES:
        total += sum(side.stat().st_size for side in (path.with_name(path.name + s) for s in _SIDES) if side.is_file())
    return total


def needed(data: Path, config_file: Path) -> int:
    """The free bytes an upgrade needs for its copy: what the copy takes, and ``MARGIN`` beside it."""
    return sum(_size(path) for path in _sources(data, config_file).values()) + MARGIN


def _free(path: Path) -> int:
    """The free bytes of the disk ``path`` is on."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free


def room(data: Path, config_file: Path) -> tuple[int, int]:
    """The bytes an upgrade's copy needs, and those the disk has free: the older copies are deleted only once it is
    whole, so the room they take is not counted."""
    return needed(data, config_file), _free(data)


def _manifest(copy_folder: Path, installed: str, installing: str, created: str, files: dict[str, dict]) -> None:
    text = json.dumps(
        {"kind": KIND, "format": FORMAT, "installed": installed, "installing": installing, "created": created,
         "files": files},
        indent=1,
    )  # fmt: skip
    _write_whole(copy_folder / MANIFEST, text)


def take(data: Path, config_file: Path, installed: str, installing: str) -> Copy | None:
    """A copy of the vault, the history and the settings as they are, before ``installing`` is installed in the place
    of ``installed``; None when there is none of them yet. Raises CopyError, OSError or sqlite3.Error when it cannot be
    made: nothing of it is left then."""
    if _VERSION.fullmatch(installing) is None:
        raise CopyError(f"{installing!r} is not a version of Latet")
    sources = _sources(data, config_file)
    if not sources:
        return None
    with locked(data):
        root = make_private_folder(data / FOLDER)
        moment = _clock()
        while True:
            folder = root / f"before-{installing}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(moment))}"
            if not os.path.lexists(folder):
                break
            moment += 1
        partial = root / f".{folder.name}.partial"
        try:
            if os.path.lexists(partial):
                shutil.rmtree(partial)
            make_private_folder(partial)
            files: dict[str, dict] = {}
            for name, source in sources.items():
                target = partial / name
                if name in DATABASES:
                    _backup(source, target)
                else:
                    target.write_bytes(source.read_bytes())
                    _private(target)
                files[name] = {"bytes": target.stat().st_size, "sha256": _digest(target)}
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))
            _manifest(partial, installed, installing, created, files)
            os.replace(partial, folder)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
    return Copy(folder, installed, installing, created, {name: str(f["sha256"]) for name, f in files.items()})


def size(copy: Copy) -> int:
    """The bytes a copy's files take."""
    return sum(_size(copy.folder / name) for name in copy.files)


def _plain_folder(path: Path) -> bool:
    """A folder of its own, not a link nor a junction: never gone through, nor deleted through."""
    try:
        found = path.lstat()
    except OSError:
        return False
    linked = stat.S_ISLNK(found.st_mode) or bool(getattr(found, "st_file_attributes", 0) & 0x400)
    return stat.S_ISDIR(found.st_mode) and not linked


def _read_manifest(folder: Path) -> dict | None:
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) and manifest.get("kind") == KIND else None


def read(folder: Path) -> Copy | None:
    """The copy ``folder`` is, or None when it is not one, or no longer one: its name and its manifest say so (a copy
    set aside, ``_set_aside``, is never put back)."""
    if _NAME.fullmatch(folder.name) is None or not _plain_folder(folder):
        return None
    manifest = _read_manifest(folder)
    if manifest is None or manifest.get("withdrawn"):
        return None
    try:
        files = manifest["files"]
        named = {str(name): str(entry["sha256"]) for name, entry in files.items()}
        copy = Copy(folder, str(manifest["installed"]), str(manifest["installing"]), str(manifest["created"]), named)
    except (KeyError, TypeError, AttributeError):
        return None
    return copy if set(copy.files) <= set(NAMES) else None


def copies(data: Path) -> list[Copy]:
    """The copies in the data folder, the newest first."""
    root = data / FOLDER
    if not _plain_folder(root):
        return []
    found = [copy for folder in root.iterdir() if (copy := read(folder)) is not None]
    return sorted(found, key=lambda copy: copy.stamp, reverse=True)


def _older(data: Path, keep: int, spare: Copy | None) -> list[Copy]:
    """The copies past the ``keep`` newest, but ``spare``."""
    others = [copy for copy in copies(data) if spare is None or copy.folder != spare.folder]
    return others[keep:]


def _set_aside_ones(root: Path) -> list[Path]:
    """The copies set aside (``_set_aside``): their name and manifest say they were copies."""
    found = []
    for folder in root.iterdir():
        if _NAME.fullmatch(folder.name) and _plain_folder(folder):
            manifest = _read_manifest(folder)
            if manifest is not None and manifest.get("withdrawn"):
                found.append(folder)
    return found


def prune(data: Path, keep: int = KEEP, spare: Copy | None = None) -> list[Path]:
    """Delete the copies older than the ``keep`` newest (``spare`` kept besides), and those alone, any copy cut off
    while it was made (a crash), and those set aside: the folders deleted."""
    gone = []
    root = data / FOLDER
    if not _plain_folder(root):
        return gone
    with locked(data):
        for folder in root.iterdir():
            if _PARTIAL.fullmatch(folder.name) and _plain_folder(folder):
                shutil.rmtree(folder)
                gone.append(folder)
        for folder in _set_aside_ones(root):
            shutil.rmtree(folder)
            gone.append(folder)
        for copy in _older(data, keep, spare):
            shutil.rmtree(copy.folder)
            gone.append(copy.folder)
    return gone


def is_copies_own(path: Path, data: Path) -> bool:
    """Whether ``path`` is the copies' (``is_copied_database``) or what Latet keeps beside them, the lock its processes
    take turns on and the work kept for a copy in use (``.asked-*.json``, ids of values, and the files they are written
    through): Latet's own, never a backup of an agent's settings, so `latet disconnect` reads none (another process
    may hold the lock as it reads)."""
    if is_copied_database(path, data):
        return True
    named = path.name == LOCK or _ASKED.fullmatch(path.name) is not None or _ASKED_NEW.fullmatch(path.name) is not None
    return named and path.parent == data / FOLDER


def is_copied_database(path: Path, data: Path) -> bool:
    """Whether ``path`` is the vault or the history (or a journal of theirs) in a copy's folder, one being made or cut
    off included: read as the vault and the history in the data folder are (`latet disconnect` looks for this
    computer's token in the backups there, and the fakes in a vault are in clear, one of which may have a token's
    shape)."""
    folder = path.parent
    database = any(path.name == name or path.name in (name + side for side in _SIDES) for name in DATABASES)
    named = _NAME.fullmatch(folder.name) is not None or _PARTIAL.fullmatch(folder.name) is not None
    return database and folder.parent == data / FOLDER and named and _plain_folder(folder)


def _ids(path: Path) -> set[str] | None:
    """The ids of the values a vault holds (keyed hashes, never a value); None when it cannot be read."""
    if not path.is_file():
        return set()
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None)
    except sqlite3.Error:
        return None
    try:
        return {str(row[0]) for row in db.execute("SELECT id FROM entities")}
    except sqlite3.Error:
        return None
    finally:
        db.close()


def learnt_since(copy: Copy, data: Path) -> int | None:
    """How many values the vault holds that the copy's did not: those that putting the copy back would lose. None when
    one of the two vaults cannot be read: they cannot be counted."""
    now, then = _ids(data / "latet.db"), _ids(copy.folder / "latet.db")
    if now is None or then is None:
        return None
    return len(now - then)


def _place(name: str, data: Path, config_file: Path) -> Path:
    return config_file if name == SETTINGS else data / name


def _with_sides(path: Path, name: str) -> list[Path]:
    """``path`` and, for a database, its journal files beside it."""
    return [path, *(path.with_name(path.name + side) for side in _SIDES)] if name in DATABASES else [path]


def put_back(copy: Copy, data: Path, config_file: Path) -> None:
    """The copy's files in the place of the current ones, the daemon stopped and those copied first (the caller's).
    What was removed while the copy was in use is taken out of it first (``_settle``): a copy that still holds it is not
    put back. Each of the copy's files is checked against its digest and staged beside its place
    (``.<name>.putting-back``); then, file by file, the live one (a database with its journal files) is renamed aside
    (``.<name>.put-aside``) and the staged one renamed into its place; a file the copy does not hold is set aside alone
    (it was not there then). Once every one is in place, what was set aside is deleted. On any failure the files set
    aside go back where they were, and the staged ones are deleted: CopyError then says that nothing was put back, or,
    where a file could not go back, exactly which are the copy's and where yours are."""
    with locked(data):
        _settle(copy, data)
        current = read(copy.folder)
        if current is None:
            raise CopyError("the copy is no longer there: nothing was put back")
        _swap_in(current, data, config_file)


def _swap_in(copy: Copy, data: Path, config_file: Path) -> None:
    staged: dict[str, Path] = {}
    try:
        for name in copy.files:
            source = copy.folder / name
            if not source.is_file() or _digest(source) != copy.files[name]:
                raise CopyError(f"{source} is not as the copy was made: nothing was put back")
            target = _place(name, data, config_file)
            stage = target.with_name(f".{target.name}.putting-back")
            shutil.copyfile(source, stage)
            _private(stage)
            staged[name] = stage
    except BaseException:
        for stage in staged.values():
            stage.unlink(missing_ok=True)
        raise
    moved: list[tuple[Path, Path]] = []  # (live path, where it was set aside), in order
    placed: list[Path] = []  # the copy's files moved into place
    try:
        for name in NAMES:
            target = _place(name, data, config_file)
            for live in _with_sides(target, name):
                if os.path.lexists(live):
                    aside = live.with_name(f".{live.name}.put-aside")
                    os.replace(live, aside)
                    moved.append((live, aside))
            if name in staged:
                os.replace(staged[name], target)
                del staged[name]
                placed.append(target)
    except OSError as exc:
        failed = _undo(moved, placed)
        for stage in staged.values():
            with contextlib.suppress(OSError):
                stage.unlink(missing_ok=True)
        why = exc.strerror or type(exc).__name__
        if not failed:
            raise CopyError(f"{why}: nothing was put back, your files are as they were") from None
        kept = ", ".join(f"{live.name} in {aside}" for live, aside in failed)
        raise CopyError(
            f"{why}: the copy was put back in part and could not all be undone; yours that are not in place: {kept}"
        ) from None
    for _, aside in moved:
        with contextlib.suppress(OSError):
            aside.unlink()


def _undo(moved: list[tuple[Path, Path]], placed: list[Path]) -> list[tuple[Path, Path]]:
    """The files set aside put back where they were, the copy's moved into place taken out first: those that could not
    go back."""
    for path in placed:
        with contextlib.suppress(OSError):
            path.unlink()
    failed = []
    for live, aside in reversed(moved):
        try:
            os.replace(aside, live)
        except OSError:
            failed.append((live, aside))
    return failed


# --- the copies follow what you remove --------------------------------------------------------------------------


class _InUse(Exception):
    """A copy's database another process holds, past the tries (``_retried``)."""


@dataclass(slots=True)
class _Work:
    """What a pass does to a copy: the values to forget (their ids), the history to clear, or to prune before
    ``cutoff``."""

    forget: set[str] = field(default_factory=set)
    clear: bool = False
    cutoff: float | None = None

    def add(self, entry: dict) -> None:
        """What was asked of a copy earlier and could not be done then (``_ask_later``), done with this."""
        self.forget |= {str(entity_id) for entity_id in entry.get("forget") or ()}
        self.clear = self.clear or bool(entry.get("clear"))

    def kept(self) -> bool:
        """Whether, not done now, it must be done later: a value forgotten, a history cleared. The retention needs no
        keeping: the next pass prunes past it."""
        return bool(self.forget) or self.clear


class Follow:
    """What Forget, clearing the history and the history's retention do to the live vault and history, done to every
    upgrade copy's as well (``storage.db.Store`` and ``storage.exchanges.ExchangeStore`` call it once their own is
    done), one copy at a time and each opened once for all of it (``locked``). Plain statements, the live files' own:
    a copy keeps its schema, and its digests are made again whenever its file changed. A copy in use is left as it is
    and what was asked of it kept for the next pass; one that cannot be opened for it, or whose vault this computer's
    key does not open, is deleted, or set aside where it cannot be (``_set_aside``); the log says so in one line, by the
    copy's versions and time."""

    def __init__(self, data: Path, root_key: bytes) -> None:
        self.data = data
        self._key = root_key

    def forget_entity(self, entity_id: str) -> None:
        self.forget_entities([entity_id])

    def forget_entities(self, entity_ids: Iterable[str]) -> None:
        ids = set(entity_ids)
        if ids:
            self._follow(_Work(forget=ids), "forget a value")

    def clear(self) -> None:
        self._follow(_Work(clear=True), "clear the history")

    def prune(self, retention_days: int, now: float | None) -> None:
        cutoff = (now or time.time()) - retention_days * 86400  # as the live history's prune
        self._follow(_Work(cutoff=cutoff), "prune the history past its retention")

    def _follow(self, work: _Work, doing: str) -> None:
        from latet.storage.db import sealer

        if not _plain_folder(self.data / FOLDER):
            return  # no copy was ever taken here
        aead = sealer(self._key)
        try:
            with locked(self.data, _FOLLOW_WAIT):
                asked = _asked(self.data)
                busy = []
                for copy in copies(self.data):
                    todo = _Work(set(work.forget), work.clear, work.cutoff)
                    for _, entry in asked:
                        if _targets(entry, copy):
                            todo.add(entry)
                    if not _one(copy, todo, aead, doing):
                        busy.append(copy)
                _answered(asked, busy)
                if busy:
                    _ask_later(self.data, work, [copy.folder.name for copy in busy])
                for copy in busy:
                    log.warning(
                        "the upgrade copy of %s taken before %s (%s) is in use by another process: what was asked to "
                        "%s is done to it the next time, and before it is put back",
                        copy.installed, copy.installing, copy.created, doing,
                    )  # fmt: skip
        except CopyError:  # the copies held by another of Latet's processes (a `latet upgrade`) or threads, still
            with contextlib.suppress(OSError):
                _ask_later(self.data, work, None)
            log.warning(
                "the upgrade copies are in use by another of Latet's processes or threads: what was asked to %s is "
                "done to them the next time, and before one is put back",
                doing,
            )
        except OSError as exc:  # the folder of the copies itself (the lock's file, what was asked)
            log.warning("the upgrade copies could not be reached to %s (%s)", doing, type(exc).__name__)


def _one(copy: Copy, todo: _Work, aead: AESGCM | None, doing: str) -> bool:
    """``todo`` done to ``copy``: True once done, or once the copy is gone (deleted or set aside, since it could not be
    opened); False when it is in use, left as it is."""
    try:
        _work_on(copy, todo, aead)
    except _InUse:
        return False
    except (sqlite3.Error, OSError, ValueError, CopyError):
        if copy.folder.exists():  # not deleted meanwhile (an upgrade's prune)
            _set_aside(copy, doing)
    return True


def _work_on(copy: Copy, todo: _Work, aead: AESGCM | None) -> None:
    """``todo`` done to the copy's vault and history with plain statements, its digests made again for each file that
    changed. ``aead`` (``db.sealer``) checks that its vault opens with this computer's key; None does not (a put back:
    deleting by ids needs no key). Raises _InUse, or what says the copy cannot be opened."""
    changed: list[str] = []
    try:
        if todo.forget and "latet.db" in copy.files:
            path = copy.folder / "latet.db"
            if _retried(lambda: _forget_in(path, todo.forget, aead)):
                changed.append("latet.db")
        if (todo.clear or todo.cutoff is not None) and "exchanges.db" in copy.files:
            path = copy.folder / "exchanges.db"
            if _retried(lambda: _history_in(path, todo.clear, todo.cutoff)):
                changed.append("exchanges.db")
    finally:
        if changed:
            _redigest(copy, *changed)


def _retried(act: Callable[[], bool]) -> bool:
    """``act`` done, tried again while another process holds its database (SQLite's busy and locked, Windows' file in
    use); _InUse past ``_TRIES``. Any other error is the copy's: it cannot be opened."""
    for attempt in range(_TRIES):
        try:
            return act()
        except (sqlite3.OperationalError, PermissionError, _InUse) as exc:
            busy = not isinstance(exc, sqlite3.OperationalError) or (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in (
                5,  # SQLITE_BUSY
                6,  # SQLITE_LOCKED
            )
            if not busy:
                raise
            if attempt + 1 == _TRIES:
                raise _InUse from None
            time.sleep(_BUSY_SECONDS)
    raise _InUse


def _connect(path: Path) -> sqlite3.Connection:
    """A copy's database as it is: no schema, no migration. What its statements delete is overwritten as they delete
    it (``secure_delete``)."""
    if not path.is_file():
        raise CopyError("a file of it is missing")
    db = sqlite3.connect(path, timeout=_BUSY_SECONDS, isolation_level=None)
    try:
        db.execute("PRAGMA secure_delete = ON")
    except BaseException:
        db.close()
        raise
    return db


def _stamp(path: Path) -> tuple[int, int]:
    found = path.stat()
    return found.st_size, found.st_mtime_ns


def _deleted(db: sqlite3.Connection, statements: list[tuple[str, tuple]]) -> bool:
    """``statements`` in one transaction: whether they deleted anything."""
    before = db.total_changes
    db.execute("BEGIN IMMEDIATE")
    try:
        for statement, parameters in statements:
            db.execute(statement, parameters)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                db.execute("ROLLBACK")
        raise
    return db.total_changes > before


def _settled(db: sqlite3.Connection) -> None:
    """What the statements wrote is in the database's file itself, not in a journal beside it (a put back copies the
    file alone); _InUse where another connection keeps it from being."""
    row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is not None and row[0] == 1:
        raise _InUse


def _forget_in(path: Path, ids: set[str], aead: AESGCM | None) -> bool:
    """The values of ``ids`` out of a copy's vault, and the local reviewer's answers that hold one (opened with this
    computer's key; without it, a put back's work kept for later, every answer: each text is read again), overwritten
    where they were (``secure_delete``; no VACUUM, which needs room for a second vault and, failing on a nearly full
    disk, would have the copy deleted for nothing): whether its file changed."""
    from latet.storage.db import FORGET, FORGET_ANSWER, answers_holding, opened_values, opens_with

    before = _stamp(path)
    db = _connect(path)
    try:
        if aead is not None:
            row = db.execute("SELECT original FROM entities LIMIT 1").fetchone()
            if row is not None and not opens_with(aead, row[0]):
                raise CopyError("its vault does not open with this computer's key")
        statements: list[tuple[str, tuple]] = [(FORGET, (entity_id,)) for entity_id in sorted(ids)]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reviews'").fetchone():
            if aead is None:
                statements.append(("DELETE FROM reviews", ()))
            else:
                answers = answers_holding(db, aead, opened_values(db, aead, sorted(ids)))
                statements += [(FORGET_ANSWER, (key,)) for key in answers]
        _deleted(db, statements)
        _settled(db)
    finally:
        db.close()
    return _stamp(path) != before


def _history_in(path: Path, clear: bool, cutoff: float | None) -> bool:
    """A copy's history cleared (then VACUUM, as the live one), or pruned before ``cutoff``, by the tables and columns
    it has (an older one may lack some): whether its file changed."""
    from latet.storage.exchanges import CLEARED, PRUNED

    before = _stamp(path)
    db = _connect(path)
    try:
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if clear:
            statements = [(f"DELETE FROM {table}", ()) for table in CLEARED if table in tables]  # noqa: S608
        else:
            statements = [
                (f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))  # noqa: S608
                for table, column in PRUNED
                if table in tables and column in {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
            ]
        _deleted(db, statements)
        if clear:
            db.execute("VACUUM")
        _settled(db)
    finally:
        db.close()
    return _stamp(path) != before


def _set_aside(copy: Copy, doing: str) -> None:
    """A copy that cannot be opened to do what was asked: deleted, rather than left holding what was removed; where it
    cannot be deleted, set aside first (its manifest says so: never offered back, and the next upgrade deletes it). The
    log says which in one line, by the copy's versions and time, never its path (a folder can hold a person's name)."""
    named = (copy.installed, copy.installing, copy.created)
    aside = _withdraw(copy.folder)
    try:
        shutil.rmtree(copy.folder)
    except OSError:
        if aside:
            log.warning(
                "the upgrade copy of %s taken before %s (%s) could not be opened to %s, nor deleted: set aside, it is "
                "never offered back, and the next upgrade deletes it",
                *named, doing,
            )  # fmt: skip
        else:
            log.warning(
                "the upgrade copy of %s taken before %s (%s) could not be opened to %s, nor deleted, nor set aside: "
                "delete it from the data folder's backups yourself",
                *named, doing,
            )  # fmt: skip
        return
    log.warning("the upgrade copy of %s taken before %s (%s) could not be opened to %s: deleted", *named, doing)


def _withdraw(folder: Path) -> bool:
    """A copy marked as one no longer (``read`` then never gives it): its manifest says it was set aside, or, where it
    cannot be written, is deleted. Whether it is marked."""
    manifest = _read_manifest(folder)
    if manifest is not None:
        with contextlib.suppress(OSError):
            _write_whole(folder / MANIFEST, json.dumps({**manifest, "withdrawn": True}, indent=1))
            return True
    try:
        (folder / MANIFEST).unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _redigest(copy: Copy, *names: str) -> None:
    """``copy``'s manifest with the size and digest of each of ``names`` made again (the copy changed by ``Follow``)."""
    manifest = _read_manifest(copy.folder)
    if manifest is None:
        raise CopyError("its manifest cannot be read")
    for name in names:
        path = copy.folder / name
        manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": _digest(path)}
    _manifest(
        copy.folder, str(manifest["installed"]), str(manifest["installing"]), str(manifest["created"]),
        manifest["files"],
    )  # fmt: skip


# --- what could not be done to a copy yet -----------------------------------------------------------------------


def _asked(data: Path) -> list[tuple[Path, dict]]:
    """What was asked of the copies and could not be done then (``_ask_later``), each with its file."""
    root = data / FOLDER
    found = []
    for path in root.iterdir():
        if _ASKED.fullmatch(path.name) and path.is_file():
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(entry, dict):
                found.append((path, entry))
    return found


def _taken_at(copy: Copy) -> float:
    try:
        return float(calendar.timegm(time.strptime(copy.created, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return 0.0  # a time not read: as if taken before anything was asked


def _targets(entry: dict, copy: Copy) -> bool:
    """Whether what ``entry`` asked is for ``copy``: the copies it names, or, where it names none (the copies were all
    held), those taken before it was asked."""
    names = entry.get("copies")
    if isinstance(names, list):
        return copy.folder.name in names
    try:
        return _taken_at(copy) <= float(entry.get("asked", 0))
    except (TypeError, ValueError):
        return True


def _ask_later(data: Path, work: _Work, names: list[str] | None) -> None:
    """``work``, which could not be done now, kept beside the copies for the next pass: for the copies ``names``, or,
    None, every copy taken before now. Ids of values, never a value; written whole, under a name of its own."""
    if not work.kept():
        return
    entry = {"asked": _clock(), "copies": names, "forget": sorted(work.forget), "clear": work.clear}
    _write_whole(data / FOLDER / f".asked-{os.urandom(16).hex()}.json", json.dumps(entry))


def _answered(asked: list[tuple[Path, dict]], left: list[Copy]) -> None:
    """What was asked earlier, once done: deleted, or kept for the copies of ``left`` it is for (still in use) alone."""
    for path, entry in asked:
        still = [copy.folder.name for copy in left if _targets(entry, copy)]
        with contextlib.suppress(OSError):
            if still:
                _write_whole(path, json.dumps({**entry, "copies": still}))
            else:
                path.unlink(missing_ok=True)


def _settle(copy: Copy, data: Path) -> None:
    """What was asked of ``copy`` while it was in use, done before it is put back (by ids: no key needed). CopyError
    when it is still in use."""
    root = data / FOLDER
    if not _plain_folder(root):
        return
    asked = _asked(data)
    todo = _Work()
    for _, entry in asked:
        if _targets(entry, copy):
            todo.add(entry)
    if not todo.kept():
        return
    try:
        _work_on(copy, todo, None)
    except _InUse:
        raise CopyError(
            "the copy is in use by another process, and still holds what you removed since: nothing was put back"
        ) from None
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise CopyError(
            f"what you removed since cannot be taken out of the copy ({type(exc).__name__}): nothing was put back"
        ) from None
    others = [other for other in copies(data) if other.folder != copy.folder]
    _answered([(path, entry) for path, entry in asked if _targets(entry, copy)], others)
