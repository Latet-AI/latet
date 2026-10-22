"""Prepared native metadata and quarantine collision witnesses; execution needs a new root slot."""

import errno
import importlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.config import Config, data_dir
from latet.integrations import agents, base
from latet.storage import folders

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="native Windows owner/DACL/junction evidence")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="native POSIX modes")


def _manifest():
    folder = data_dir()
    folder.mkdir(mode=0o700, parents=True)
    path = base.manifest_path()
    path.write_bytes(b"{}")
    return path


def _forbidden(*args, **kwargs):
    pytest.fail("content traversal or a side effect preceded native trust refusal")


@WINDOWS
@pytest.mark.parametrize("grant", ["*S-1-5-32-545:(OI)(CI)R", "*S-1-5-11:(OI)(CI)M"])
def test_native_windows_manifest_and_direct_apply_refuse_extra_access(computer, monkeypatch, grant):
    path = _manifest()
    assert folders.private_folder_problem(data_dir()) is None
    owner, _ = folders.folder_grants(data_dir())
    assert owner in {folders.my_sid(), folders.SYSTEM, folders.ADMINISTRATORS}
    sid = grant.split(":", 1)[0]

    def icacls(*arguments):
        result = subprocess.run(
            ["icacls", str(data_dir()), *arguments], capture_output=True, text=True, check=False, timeout=10
        )
        assert result.returncode == 0, result.stderr

    icacls("/grant", grant)
    try:
        assert folders.private_folder_problem(data_dir()) is not None
        with monkeypatch.context() as scoped:
            opened = Path.open

            def content(target, *args, **kwargs):
                assert target != path, "record content was read after native DACL refusal"
                return opened(target, *args, **kwargs)

            scoped.setattr(Path, "open", content)
            scoped.setattr(agents.Codex, "outdated", _forbidden)
            with pytest.raises(base.RecordsUnavailable):
                base.load_records()
            with pytest.raises(base.RecordsUnavailable):
                agents.Codex().apply(config=Config(), chatgpt=False)
    finally:
        icacls("/remove:g", sid)
    assert path.read_bytes() == b"{}"
    assert folders.private_folder_problem(data_dir()) is None


@POSIX
@pytest.mark.parametrize("mode", [0o700, 0o770, 0o702])
def test_native_posix_record_folder_modes(computer, mode):
    path = _manifest()
    data_dir().chmod(mode)
    try:
        if mode == 0o700:
            assert base.load_records() == {}
        else:
            with pytest.raises(base.RecordsUnavailable):
                base.load_records()
    finally:
        data_dir().chmod(0o700)
    assert path.read_bytes() == b"{}"


def test_native_manifest_link_never_reads_its_target(computer):
    target = computer / "outside.json"
    target.write_bytes(b"private outside sentinel")
    path = _manifest()
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"native symlink unavailable: {type(exc).__name__}")
    with pytest.raises(base.RecordsUnavailable):
        base.load_records()
    assert target.read_bytes() == b"private outside sentinel"


@WINDOWS
def test_native_record_root_junction_never_enters_its_target(computer):
    target = computer / "outside-folder"
    target.mkdir(mode=0o700)
    sentinel = target / "integrations.json"
    sentinel.write_bytes(b"private outside sentinel")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(data_dir()), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        pytest.skip("native junction creation unavailable")
    try:
        assert data_dir().is_junction()
        with pytest.raises(base.RecordsUnavailable):
            base.load_records()
    finally:
        data_dir().rmdir()  # this test-owned junction alone, never a recursive removal of its target
    assert sentinel.read_bytes() == b"private outside sentinel"


@WINDOWS
def test_native_quarantine_refuses_a_data_root_junction_without_touching_its_target(computer, monkeypatch, capsys):
    command = importlib.import_module("latet.cli.connect")
    target = computer / "outside-quarantine-folder"
    target.mkdir(mode=0o700)
    manifest = target / "integrations.json"
    manifest.write_bytes(b"private junction manifest")
    backups = target / "backups"
    backups.mkdir(mode=0o700)
    backup = backups / "owned.txt"
    backup.write_bytes(b"private junction backup")
    sentinel = computer / "outside-quarantine-sentinel.txt"
    sentinel.write_bytes(b"outside sentinel")
    identities = [(path.stat().st_dev, path.stat().st_ino) for path in (manifest, backups)]
    junction = data_dir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        pytest.skip("native quarantine junction creation unavailable")
    aliases = {junction / "integrations.json", junction / "backups"}
    protected = (junction, target)
    renamed = []
    rename, opened, native_open, scanned, listed = Path.rename, Path.open, os.open, os.scandir, Path.iterdir

    def inside(candidate):
        if isinstance(candidate, int):
            return False
        path = Path(os.fsdecode(candidate))
        return any(path == root or root in path.parents for root in protected)

    def observed_rename(path, destination):
        renamed.append((path, destination))
        return rename(path, destination)  # forward actual native Windows rename; do not simulate refusal

    def no_content(operation):
        def guarded(path, *args, **kwargs):
            assert not inside(path), "quarantine entered content through an unsafe data-root junction"
            return operation(path, *args, **kwargs)

        return guarded

    try:
        assert junction.is_junction(), "the actual native junction fixture was not established"
        assert all(path.exists() for path in aliases), "junction does not expose both real synthetic roots"
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "rename", observed_rename)
            scoped.setattr(Path, "open", no_content(opened))
            scoped.setattr(os, "open", no_content(native_open))
            scoped.setattr(os, "scandir", no_content(scanned))
            scoped.setattr(Path, "iterdir", no_content(listed))
            scoped.setattr(Path, "rglob", _forbidden)
            scoped.setattr(shutil, "rmtree", _forbidden)
            scoped.setattr(shutil, "copytree", _forbidden)
            retained = command._quarantine_records()
        assert renamed == [], f"actual rename crossed the unsafe junction: {renamed}"
        assert manifest.read_bytes() == b"private junction manifest"
        assert backup.read_bytes() == b"private junction backup"
        assert [(path.stat().st_dev, path.stat().st_ino) for path in (manifest, backups)] == identities
        assert not os.path.lexists(target / "integrations.json.untrusted")
        assert not os.path.lexists(target / "backups.untrusted")
        assert sentinel.read_bytes() == b"outside sentinel"
        assert junction in retained or aliases <= set(retained), "unsafe record roots were not retained as unavailable"
        captured = capsys.readouterr()
        words = captured.out + captured.err
        assert any(word in words.lower() for word in ("retain", "unavailable", "untrusted"))
        assert "private junction" not in words
    finally:
        junction.rmdir()  # this exact newly created junction only, never recursively remove its owned target


@WINDOWS
@pytest.mark.parametrize("state", ["private-looking", "extra-access", "handoff-junction"])
def test_known_unsafe_disconnect_keeps_its_windows_quarantine_refusal(computer, monkeypatch, capsys, state):
    command = importlib.import_module("latet.cli.connect")
    _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    (backups / "owned.txt").write_bytes(b"original backup")
    assert folders.private_folder_problem(data_dir()) is None
    outside = computer / "handoff-outside"
    outside.mkdir(mode=0o700)
    (outside / "integrations.json").write_bytes(b"outside manifest")
    (outside / "backups").mkdir(mode=0o700)
    (outside / "backups" / "owned.txt").write_bytes(b"outside backup")
    sentinel = computer / "handoff-sentinel.txt"
    sentinel.write_bytes(b"outside sentinel")
    originals_before = [
        (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
        for path in (data_dir() / "integrations.json", backups)
    ]
    outside_before = [
        (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
        for path in (outside / "integrations.json", outside / "backups")
    ]
    profile = computer / "codex-profile" / "config.toml"
    profile.parent.mkdir(mode=0o700)
    monkeypatch.setattr(agents.Codex, "config_path", lambda self: profile)
    original, rename = command._quarantine_records, Path.rename
    handed, renamed, swapped = [], [], []
    displaced = computer / "original-displaced-data"

    def icacls(*arguments):
        result = subprocess.run(
            ["icacls", str(data_dir()), *arguments], capture_output=True, text=True, check=False, timeout=10
        )
        assert result.returncode == 0, "native test-owned DACL operation failed"

    def at_handoff(*args, **kwargs):
        handed.append(True)
        if state == "handoff-junction":
            rename(data_dir(), displaced)  # fixture move, not an operation attributed to production
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(data_dir()), str(outside)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode:
                pytest.skip("native handoff junction creation unavailable")
            assert data_dir().is_junction()
            swapped.append(True)
        return original(*args, **kwargs)  # forwards only the actual caller's existing arguments; no missing-API RED

    def observed_rename(path, destination):
        renamed.append((path, destination))
        return rename(path, destination)

    opened = Path.open

    def no_record_content(path, *args, **kwargs):
        protected = (data_dir(), outside, displaced)
        assert not any(path == root or root in path.parents for root in protected), "unsafe record content was opened"
        return opened(path, *args, **kwargs)

    try:
        if state == "extra-access":
            icacls("/grant", "*S-1-5-32-545:(OI)(CI)R")
            assert folders.private_folder_problem(data_dir()) is not None
        with monkeypatch.context() as scoped:
            scoped.setattr(command, "_quarantine_records", at_handoff)
            scoped.setattr(Path, "rename", observed_rename)
            scoped.setattr(Path, "open", no_record_content)
            scoped.setattr(Path, "rglob", _forbidden)
            scoped.setattr(shutil, "rmtree", _forbidden)
            scoped.setattr(shutil, "copytree", _forbidden)
            command._forget_files([], unsafe=True)  # existing actual caller, even while the current path looks private
            left = command._still_holding([], [], [], unsafe=True)
        assert handed == [True], "actual disconnect-to-quarantine handoff was not reached"
        assert swapped == ([True] if state == "handoff-junction" else [])
        assert renamed == [], f"known-unsafe context permitted actual Windows renames: {renamed}"
        assert {data_dir() / "integrations.json", data_dir() / "backups"} <= {path for path, _ in left}
        originals = displaced if swapped else data_dir()
        assert (originals / "integrations.json").read_bytes() == b"{}"
        assert (originals / "backups" / "owned.txt").read_bytes() == b"original backup"
        assert [
            (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
            for path in (originals / "integrations.json", originals / "backups")
        ] == originals_before
        assert (outside / "integrations.json").read_bytes() == b"outside manifest"
        assert (outside / "backups" / "owned.txt").read_bytes() == b"outside backup"
        assert [
            (path.stat().st_dev, path.stat().st_ino, path.stat().st_mode)
            for path in (outside / "integrations.json", outside / "backups")
        ] == outside_before
        assert sentinel.read_bytes() == b"outside sentinel"
        assert not os.path.lexists(outside / "integrations.json.untrusted")
        assert not os.path.lexists(outside / "backups.untrusted")
        captured = capsys.readouterr()
        assert any(word in (captured.out + captured.err).lower() for word in ("retain", "unavailable", "untrusted"))
        assert "outside manifest" not in captured.out + captured.err
    finally:
        if swapped:
            data_dir().rmdir()  # owned junction only; no recursive removal or target rename
        if state == "extra-access":
            icacls("/remove:g", "*S-1-5-32-545")


@WINDOWS
def test_native_private_windows_quarantine_preserves_both_root_identities(computer):
    command = importlib.import_module("latet.cli.connect")
    assert not data_dir().exists()
    assert command._quarantine_records() == []
    assert not data_dir().exists(), "absent private quarantine created a data folder"
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    (backups / "owned.txt").write_bytes(b"original backup")
    assert folders.private_folder_problem(data_dir()) is None
    identities = [(path.stat().st_dev, path.stat().st_ino, path.stat().st_mode) for path in (manifest, backups)]
    assert command._quarantine_records() == []
    destinations = [path.with_name(path.name + ".untrusted") for path in (manifest, backups)]
    assert all(not os.path.lexists(path) for path in (manifest, backups))
    assert [(path.stat().st_dev, path.stat().st_ino, path.stat().st_mode) for path in destinations] == identities
    assert destinations[0].read_bytes() == b"{}"
    assert (destinations[1] / "owned.txt").read_bytes() == b"original backup"


@WINDOWS
@pytest.mark.parametrize("name", ["integrations.json", "backups"])
def test_native_quarantine_collision_created_at_rename_keeps_both_versions(computer, monkeypatch, name):
    command = importlib.import_module("latet.cli.connect")
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir()
    original_backup = backups / "original.txt"
    original_backup.write_bytes(b"original backup")
    source = data_dir() / name
    destination = source.with_name(name + ".untrusted")
    rename = Path.rename
    created = []

    def collision(path, target):
        if path == source:
            if name == "backups":
                destination.mkdir()
                (destination / "collision.txt").write_bytes(b"earlier quarantine")
            else:
                destination.write_bytes(b"earlier quarantine")
            created.append(path)
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", collision)
    monkeypatch.setattr(Path, "rglob", _forbidden)
    monkeypatch.setattr(shutil, "rmtree", _forbidden)
    monkeypatch.setattr(shutil, "copytree", _forbidden)
    retained = command._quarantine_records()
    assert created == [source]
    assert source in retained
    if name == "backups":
        assert original_backup.read_bytes() == b"original backup"
        assert (destination / "collision.txt").read_bytes() == b"earlier quarantine"
    else:
        assert manifest.read_bytes() == b"{}"
        assert destination.read_bytes() == b"earlier quarantine"


@POSIX
def test_posix_quarantine_requires_a_non_overwriting_native_operation(computer):
    command = importlib.import_module("latet.cli.connect")
    path = _manifest()
    retained = command._quarantine_records()
    assert retained == []
    assert not os.path.lexists(path)
    assert path.with_name("integrations.json.untrusted").read_bytes() == b"{}"


def _unavailable_metadata(scoped, root):
    # CPython/Windows lexists is a native binding, not the Python os.lstat wrapper.
    exists, inspect = os.path.lexists, Path.lstat
    observed = []

    def existence(candidate):
        if Path(candidate) == root:
            observed.append("lexists")
            return False  # the lossy native answer when inspection was denied
        return exists(candidate)

    def metadata(candidate, *args, **kwargs):
        if candidate == root:
            observed.append("lstat")
            raise OSError(errno.EACCES, "planted_metadata_error_9741")
        return inspect(candidate, *args, **kwargs)

    scoped.setattr(os.path, "lexists", existence)
    scoped.setattr(Path, "lstat", metadata)
    return observed


@pytest.mark.parametrize("target", ["folder", "manifest"])
def test_metadata_inspection_failure_is_unavailable_rather_than_absent(computer, monkeypatch, target):
    assert base.load_records() == {}  # genuine absence is permitted, without creating the store
    assert not data_dir().exists()
    path = _manifest()
    raw = b'{"claude":{"integration":"claude","applied_at":1}}'
    path.write_bytes(raw)
    assert set(base.load_records()) == {"claude"}  # no-error metadata reaches real manifest content
    root = data_dir() if target == "folder" else path
    failure = None
    with monkeypatch.context() as scoped:
        observed = _unavailable_metadata(scoped, root)
        try:
            base.load_records()
        except base.RecordsUnavailable as exc:
            failure = exc
    assert observed, "neither actual existence nor independent metadata boundary was reached"
    assert failure is not None, f"unavailable metadata was treated as absence; observed={observed}"
    assert "planted_metadata_error_9741" not in str(failure)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("name", ["integrations.json", "backups"])
def test_unsafe_final_check_retains_roots_whose_metadata_is_unavailable(computer, monkeypatch, name):
    command = importlib.import_module("latet.cli.connect")
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir()
    sentinel = backups / "sensitive.txt"
    sentinel.write_bytes(b"private content marker")
    root = data_dir() / name
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rglob", _forbidden)
        normal = command._still_holding([], [], [], unsafe=True)
        assert {manifest, backups} <= {candidate for candidate, _ in normal}
        observed = _unavailable_metadata(scoped, root)
        left = command._still_holding([], [], [], unsafe=True)
    assert observed, "neither actual existence nor independent metadata boundary was reached"
    assert root in {candidate for candidate, _ in left}, f"unavailable root was omitted; observed={observed}"
    assert all(
        "planted_metadata_error_9741" not in reason and "private content marker" not in reason for _, reason in left
    )
    assert manifest.read_bytes() == b"{}"
    assert sentinel.read_bytes() == b"private content marker"


@WINDOWS
@pytest.mark.parametrize("name", ["integrations.json", "backups"])
def test_windows_quarantine_retains_a_root_with_unavailable_metadata(computer, monkeypatch, capsys, name):
    command = importlib.import_module("latet.cli.connect")
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    sentinel = backups / "owned.txt"
    sentinel.write_bytes(b"private quarantine marker")
    assert folders.private_folder_problem(data_dir()) is None
    root = data_dir() / name
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "rglob", _forbidden)
        scoped.setattr(shutil, "rmtree", _forbidden)
        scoped.setattr(shutil, "copytree", _forbidden)
        observed = _unavailable_metadata(scoped, root)
        retained = command._quarantine_records()
    assert observed, "the actual lossy-existence or independent metadata boundary was never reached"
    assert root in retained, f"unavailable quarantine metadata was treated as absence; observed={observed}"
    if name == "integrations.json":
        assert manifest.read_bytes() == b"{}"
    else:
        assert sentinel.read_bytes() == b"private quarantine marker"
    captured = capsys.readouterr()
    assert "planted_metadata_error_9741" not in captured.out + captured.err
    assert "private quarantine marker" not in captured.out + captured.err


def _final_scan_observed(monkeypatch, protected):
    """Forward safe I/O; stop a forbidden attempt before any unsafe enumeration or content actually happens."""
    command = importlib.import_module("latet.cli.connect")
    scan, listed, opened, path_open, fdopen = os.scandir, Path.iterdir, os.open, Path.open, os.fdopen
    observed = {"attempted": [], "backup_opens": [], "backup_read_requests": []}
    backup_fds = set()

    def checked(name, operation):
        if isinstance(name, (Path, str, bytes)):
            path = Path(os.fsdecode(name))
            if any(path == root or root in path.parents for root in protected):
                observed["attempted"].append((operation, path))
                raise PermissionError(errno.EACCES, "planted_final_backup_error_9741")
            return path
        return None

    def checked_scan(name):
        checked(name, "enumeration")
        return scan(name)

    def checked_iterdir(path):
        checked(path, "enumeration")
        return listed(path)

    def checked_open(name, *args, **kwargs):
        path = checked(name, "descriptor content")
        descriptor = opened(name, *args, **kwargs)
        if path is not None and (data_dir() / "backups") in path.parents and stat.S_ISREG(os.fstat(descriptor).st_mode):
            observed["backup_opens"].append(path)
            backup_fds.add(descriptor)
        return descriptor

    class ObservedBackup:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, most=-1):
            observed["backup_read_requests"].append(most)
            return self.stream.read(most)

    def checked_fdopen(descriptor, *args, **kwargs):
        stream = fdopen(descriptor, *args, **kwargs)
        if descriptor in backup_fds:
            backup_fds.remove(descriptor)
            return ObservedBackup(stream)
        return stream

    def checked_path_open(path, *args, **kwargs):
        checked(path, "path content")
        return path_open(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", checked_scan)
        scoped.setattr(Path, "iterdir", checked_iterdir)
        scoped.setattr(os, "open", checked_open)
        scoped.setattr(os, "fdopen", checked_fdopen)
        scoped.setattr(Path, "open", checked_path_open)
        left = command._still_holding([], [], [], unsafe=False)
    return left, observed


@pytest.mark.parametrize("state", ["root-write", "child-write", "child-link"])
def test_trusted_final_scan_refuses_untrusted_backup_directories_before_entry(computer, monkeypatch, state):
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    outside = computer / "outside-backup-target"
    outside.mkdir(mode=0o700)
    sentinel = outside / "owned.txt"
    sentinel.write_bytes(b"outside bytes must remain untouched")
    target = backups if state == "root-write" else backups / "child"
    if state == "child-link":
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 0 and target.is_junction(), "owned native junction fixture unavailable"
        else:
            target.symlink_to(outside, target_is_directory=True)
            assert target.is_symlink()
    else:
        target.mkdir(mode=0o700, exist_ok=True)
        (target / "owned.txt").write_bytes(b"unsafe backup bytes must remain untouched")
        if os.name == "nt":
            result = subprocess.run(
                ["icacls", str(target), "/grant", "*S-1-5-32-545:(OI)(CI)W"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 0, "owned native writable-directory fixture unavailable"
        else:
            target.chmod(0o777)
        assert folders.private_folder_problem(target) is not None
    try:
        base.records_preflight()  # Existing real admission: the selected store and manifest remain safe.
        assert folders.private_folder_problem(data_dir()) is None
        left, observed = _final_scan_observed(monkeypatch, [target, outside])
        print(f"EC_FINAL_BACKUP_SCAN state={state} attempted={observed['attempted']} retained={left}")
        assert manifest.read_bytes() == b"{}"
        assert sentinel.read_bytes() == b"outside bytes must remain untouched"
        if state != "child-link":
            assert (target / "owned.txt").read_bytes() == b"unsafe backup bytes must remain untouched"
        assert observed["attempted"] == [], "final scan reached unsafe backup enumeration or content"
        assert any(path in (target, backups) for path, _ in left), "unavailable backups were reported clean"
        assert all("planted_final_backup_error_9741" not in why for _, why in left)
    finally:
        if state == "child-link":
            if os.name == "nt":
                target.rmdir()  # Remove only this owned junction; never its target.
            else:
                target.unlink()
        elif os.name == "nt":
            result = subprocess.run(
                ["icacls", str(target), "/remove:g", "*S-1-5-32-545"],
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 0, "owned native DACL cleanup failed"
        else:
            target.chmod(0o700)
        assert sentinel.read_bytes() == b"outside bytes must remain untouched"
        print(f"EC_FINAL_BACKUP_CLEANUP state={state} outside_preserved=True")
    assert manifest.read_bytes() == b"{}"
    assert sentinel.read_bytes() == b"outside bytes must remain untouched"
    if state != "child-link":
        assert (target / "owned.txt").read_bytes() == b"unsafe backup bytes must remain untouched"


def test_trusted_final_scan_refuses_a_backup_leaf_marked_link_by_metadata(computer, monkeypatch):
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    leaf = backups / "owned.txt"
    leaf.write_bytes(b"owned leaf bytes")
    found, lstat = leaf.lstat(), Path.lstat
    linked = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0x400)
    observed_metadata = []

    def marked(path, *args, **kwargs):
        if path == leaf:
            observed_metadata.append(path)
            return linked
        return lstat(path, *args, **kwargs)

    base.records_preflight()
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", marked)
        left, observed = _final_scan_observed(scoped, [leaf])
    assert leaf.stat().st_ino == found.st_ino and leaf.read_bytes() == b"owned leaf bytes"
    assert manifest.read_bytes() == b"{}"  # API metadata simulation only, not native symbolic-link evidence.
    assert observed_metadata, "simulated existing leaf metadata boundary was never reached"
    assert observed["attempted"] == [], "final scan attempted content after link-marked leaf metadata"
    assert any(path in (leaf, backups) for path, _ in left), "link-marked backup was reported clean"


def test_trusted_final_scan_reads_a_safe_nested_backup_and_reports_its_token(computer, monkeypatch):
    manifest = _manifest()
    backups = data_dir() / "backups"
    backups.mkdir(mode=0o700)
    child = backups / "private"
    child.mkdir(mode=0o700)
    leaf = child / "owned.txt"
    token = b"latet_pat_" + b"z" * 43
    leaf.write_bytes(token)
    base.records_preflight()
    left, observed = _final_scan_observed(monkeypatch, [])
    assert leaf in observed["backup_opens"], "safe actual backup content was never reached"
    command = importlib.import_module("latet.cli.connect")
    assert observed["backup_read_requests"] == [command._READ_MOST], "final scan lost its existing read bound"
    assert (leaf, "this computer's token") in left
    assert observed["attempted"] == [] and leaf.read_bytes() == token and manifest.read_bytes() == b"{}"
