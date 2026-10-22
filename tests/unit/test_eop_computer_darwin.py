"""EC5-D existing-caller simulations; no native Darwin ACL, loader or filesystem evidence."""

import ctypes
import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.integrations import base
from latet.storage import folders

SAFE = 0x00100A8A


class _PublicCall:
    def __init__(self, name):
        self.name = name

    def __call__(self, *arguments):
        return getattr(_DarwinACL.active, self.name)(*arguments)


class _DarwinACL:
    """Bounded public opaque-API double; all directory UID/mode/fd/ACL observations are synthetic."""

    active = None
    O_RDONLY, O_DIRECTORY, O_NOFOLLOW, O_CLOEXEC = 0, 0x100000, 0x100, 0x1000000

    def __init__(self, selected, acl_path, entries, selected_mode):
        type(self).active = self
        self.selected, self.acl_path, self.entries = selected, acl_path, entries
        self.selected_mode = selected_mode
        self.actual_lstat, self.actual_fstat, self.actual_close = Path.lstat, os.fstat, os.close
        self.descriptors, self.acls, self.handles = {}, {}, {}
        self.opens, self.closed, self.freed, self.retrievals, self.loads = [], [], [], [], []
        self.chmods: list[tuple[Path, int]] = []
        self.library = SimpleNamespace(
            **{
                name: _PublicCall(name)
                for name in (
                    "acl_get_fd_np",
                    "acl_valid",
                    "acl_get_entry",
                    "acl_get_tag_type",
                    "acl_get_permset_mask_np",
                    "acl_free",
                )
            }
        )

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def scalar(value):
        return int(getattr(value, "value", value))

    def getuid(self):
        return 10001

    def metadata(self, path):
        path = Path(path)
        found = self.actual_lstat(path)
        if not stat.S_ISDIR(found.st_mode):
            return found
        return SimpleNamespace(
            st_dev=found.st_dev,
            st_ino=found.st_ino,
            st_uid=0 if path.parent == path else 10001,
            st_gid=10001,
            st_mode=stat.S_IFDIR | (self.selected_mode if path == self.selected else 0o755),
            st_file_attributes=getattr(found, "st_file_attributes", 0),
        )

    def stat(self, path, *arguments, **keywords):
        return self.metadata(path)

    def open(self, path, flags, *arguments, **keywords):
        path = Path(path)
        required = self.O_DIRECTORY | self.O_NOFOLLOW | self.O_CLOEXEC
        assert flags & required == required, "simulated Darwin metadata open omitted a required binding flag"
        assert flags == self.O_RDONLY | required, (
            "simulated Darwin metadata open included flags outside its readonly set"
        )
        assert stat.S_ISDIR(self.metadata(path).st_mode)
        descriptor = 50000 + len(self.opens)
        self.descriptors[descriptor] = path
        self.opens.append(path)
        assert len(self.opens) <= 128, "finite witness exceeded its directory-open observation ceiling"
        return descriptor

    def fstat(self, descriptor):
        descriptor = self.scalar(descriptor)
        return (
            self.metadata(self.descriptors[descriptor])
            if descriptor in self.descriptors
            else self.actual_fstat(descriptor)
        )

    def close(self, descriptor):
        descriptor = self.scalar(descriptor)
        if descriptor not in self.descriptors:
            return self.actual_close(descriptor)
        self.closed.append(descriptor)
        self.descriptors.pop(descriptor)

    def fchmod(self, descriptor, mode):
        """R2's repair of Latet's own data folder, on the folder a bound descriptor names (never a path)."""
        path = self.descriptors[self.scalar(descriptor)]
        assert path == self.selected, "a simulated repair reached a folder that is not Latet's data folder"
        self.chmods.append((path, mode))
        self.selected_mode = mode

    def load(self, path, *arguments, **keywords):
        assert path == "/usr/lib/libSystem.B.dylib", (
            "Darwin ACL reader did not select its trusted absolute system library"
        )
        assert keywords.get("use_errno") is True
        self.loads.append(path)
        return self.library

    def acl_get_fd_np(self, descriptor, kind):
        path = self.descriptors[self.scalar(descriptor)]
        assert self.scalar(kind) == 0x100
        self.retrievals.append(path)
        entries = self.entries if path == self.acl_path else None
        if entries is None:
            ctypes.set_errno(errno.ENOENT)
            return None
        handle = 1000 + len(self.acls)
        self.acls[handle] = {"entries": entries, "index": -1}
        return handle

    def acl_valid(self, handle):
        assert self.scalar(handle) in self.acls
        return 0

    def acl_get_entry(self, handle, selector, pointer):
        handle, selector = self.scalar(handle), self.scalar(selector)
        assert selector in (0, -1), "simulated Darwin cursor used a Linux iterator convention"
        row = self.acls[handle]
        row["index"] = 0 if selector == 0 else row["index"] + 1
        if row["index"] >= len(row["entries"]):
            ctypes.set_errno(errno.EINVAL)
            return -1
        entry = 100000 + handle * 100 + row["index"]
        self.handles[entry] = row["entries"][row["index"]]
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = entry
        return 0

    def acl_get_tag_type(self, entry, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = self.handles[self.scalar(entry)][0]
        return 0

    def acl_get_permset_mask_np(self, entry, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint64))[0] = self.handles[self.scalar(entry)][1]
        return 0

    def acl_free(self, handle):
        handle = self.scalar(handle)
        assert handle in self.acls
        self.freed.append(handle)
        return 0


def _dispatch(monkeypatch, selected, acl_path, entries, mode=0o700):
    runtime = _DarwinACL(selected, acl_path, entries, mode)
    actual_metadata = base._metadata
    namespace, private = folders.namespace_folder_problem, folders.private_folder_problem
    observed = {"metadata": [], "namespace": [], "private": [], "content": []}
    actual_open = os.open

    def metadata(path):
        found = actual_metadata(path)
        observed["metadata"].append(Path(path))
        return runtime.metadata(path) if found is not None else None

    def namespace_check(path, found, **kwargs):
        observed["namespace"].append(Path(path))
        return namespace(path, found, **kwargs)

    def private_check(path):
        observed["private"].append(Path(path))
        return private(path)

    def content_open(path, flags, *arguments, **keywords):
        observed["content"].append(Path(path))
        return actual_open(path, flags, *arguments, **keywords)

    monkeypatch.setattr(folders, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(folders, "os", runtime)
    monkeypatch.setattr(ctypes, "CDLL", runtime.load)
    monkeypatch.setattr(base, "_metadata", metadata)
    monkeypatch.setattr(folders, "namespace_folder_problem", namespace_check)
    monkeypatch.setattr(folders, "private_folder_problem", private_check)
    monkeypatch.setattr(base.os, "open", content_open)
    return runtime, observed


@pytest.mark.parametrize(
    ("location", "entries", "mode", "allowed", "repaired"),
    [
        ("selected", None, 0o700, True, False),
        ("selected", (), 0o700, True, False),
        ("selected", ((2, 0x10),), 0o700, True, False),
        ("ancestor", ((1, SAFE),), 0o700, True, False),
        ("ancestor", ((1, 0x4),), 0o700, False, False),
        ("ancestor", ((1, 1 << 63),), 0o700, False, False),
        ("selected", ((1, 0x2),), 0o700, False, False),
        ("selected", ((1, 0),), 0o700, False, False),
        # R2: an older Latet's data folder that nobody else could write into is tightened, then read.
        ("selected", None, 0o755, True, True),
        ("selected", None, 0o711, True, True),
        # ... but one its group could write into, or whose access list lets another account change it, is refused.
        ("selected", None, 0o775, False, False),
        ("selected", ((1, 0x4),), 0o755, False, False),
    ],
    ids=[
        "no-acl",
        "empty-acl",
        "deny-delete-only",
        "read-only-ancestor",
        "mutation-ancestor",
        "unknown-high-mask",
        "selected-read-allow",
        "selected-zero-allow",
        "selected0755-tightened",
        "selected0711-tightened",
        "selected0775-refused",
        "selected0755-write-allow-refused",
    ],
)
def test_simulated_darwin_acl_policy_reaches_existing_record_caller_before_content(
    computer, monkeypatch, location, entries, mode, allowed, repaired
):
    selected = computer / "data"
    selected.mkdir(mode=0o700)
    manifest = selected / "integrations.json"
    manifest.write_bytes(b"{}")
    # A computer connected to a Latet server: an ancestor another account may change is refused there; in local mode
    # it is gone past (R28, the test below).
    (selected / "connection.yaml").write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")
    before = manifest.stat().st_ino, manifest.read_bytes()
    failure, records = None, None
    with monkeypatch.context() as scoped:
        runtime, observed = _dispatch(scoped, selected, computer if location == "ancestor" else selected, entries, mode)
        try:
            records = base.load_records()
        except base.RecordsUnavailable as error:
            failure = error
    assert observed["namespace"], "existing real namespace entrypoint was never reached"
    print(
        f"EC5_DARWIN_SIMULATION namespace_calls={len(observed['namespace'])} "
        f"private_calls={len(observed['private'])} acl_api_reads={len(runtime.retrievals)} "
        f"actual_content_opens={len(observed['content'])} refused={failure is not None} native=False"
    )
    assert (manifest.stat().st_ino, manifest.read_bytes()) == before
    assert runtime.chmods == ([(selected, 0o700)] if repaired else [])
    if allowed:
        assert failure is None, "adopted no/empty/deny-only/read-ancestor positive was refused"
        assert records == {}
        assert observed["content"] == [manifest], "positive did not read the actual owned manifest"
    else:
        assert failure is not None, "unsafe simulated Darwin ACL or selected mode was accepted"
        assert observed["content"] == [], "unsafe simulated Darwin history reached actual content"
        assert "Integration records unavailable" in str(failure)
    assert not runtime.descriptors, "simulated bound directory descriptors were retained"
    assert sorted(runtime.freed) == sorted(runtime.acls), "simulated allocated ACL was not freed exactly once"


def test_simulated_darwin_mutable_ancestor_is_gone_past_in_local_mode(computer, monkeypatch):
    """R28: with no connection file, an ancestor whose access list lets another account change it is said by `latet
    doctor` and the records are read; the data folder's own entry stays a refusal (above)."""
    selected = computer / "data"
    selected.mkdir(mode=0o700)
    manifest = selected / "integrations.json"
    manifest.write_bytes(b"{}")
    with monkeypatch.context() as scoped:
        _, observed = _dispatch(scoped, selected, computer, ((1, 0x4),), 0o700)
        assert base.load_records() == {}
    assert observed["content"] == [manifest]


def test_simulated_darwin_new_selected_allow_refuses_before_existing_save_output(computer, monkeypatch):
    selected = computer / "data"
    assert not selected.exists()
    writes, failure = [], None

    class OutputReached(RuntimeError):
        pass

    def before_output(path, *arguments, **keywords):
        writes.append(Path(path))
        raise OutputReached("existing save reached content output")

    with monkeypatch.context() as scoped:
        runtime, observed = _dispatch(scoped, selected, selected, ((1, 0x2),))
        scoped.setattr(base, "write_atomic", before_output)
        try:
            base.save_records({"owned": base.Record("owned", 1)})
        except base.RecordsUnavailable as error:
            failure = error
        except OutputReached:
            pass  # Explicit ordering trap, never a controlled metadata-refusal result.
    assert selected in observed["metadata"], "existing missing-writer metadata boundary was never reached"
    print(
        f"EC5_DARWIN_WRITER_SIMULATION acl_api_reads={len(runtime.retrievals)} output_attempts={len(writes)} native=False"
    )
    assert writes == [], "new selected directory with simulated inherited read ALLOW reached secret output"
    assert failure is not None, "new selected ALLOW did not produce controlled unavailability"
    assert selected.is_dir(), "normal private missing-component creation never occurred"
    assert not (selected / "integrations.json").exists()
    assert not runtime.descriptors
    assert sorted(runtime.freed) == sorted(runtime.acls)
