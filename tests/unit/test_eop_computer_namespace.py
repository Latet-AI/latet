"""Finite EC5 existing-caller native Windows witnesses; no future namespace helper API."""

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.config import data_dir
from latet.integrations import base
from latet.storage import folders

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="actual Windows ACL/junction/parser boundary")
USERS = "S-1-5-32-545"


def _icacls(path, *arguments):
    result = subprocess.run(["icacls", str(path), *arguments], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 0, "owned native ACL fixture operation failed"


def _store(folder, monkeypatch):
    folder.mkdir(mode=0o700)
    monkeypatch.setenv("LATET_DATA_DIR", str(folder))
    manifest = folder / "integrations.json"
    manifest.write_bytes(b"{}")
    assert folders.private_folder_problem(folder) is None
    return manifest


def _connected(folder):
    """A connection file in the data folder: an ancestor another account may change is refused on a connected
    computer, and gone past in local mode (R28)."""
    (folder / "connection.yaml").write_text("url: https://latet.acme.lan\nuser: u_emilycarte\nuser_name: E\n")


def _load_observed(monkeypatch):
    opened, failure, records = [], None, None
    actual_open = os.open

    def observed(path, flags, *arguments, **keywords):
        opened.append(Path(path))
        return actual_open(path, flags, *arguments, **keywords)

    with monkeypatch.context() as scoped:
        scoped.setattr(base.os, "open", observed)
        try:
            records = base.load_records()
        except base.RecordsUnavailable as error:
            failure = error
    return opened, failure, records


@pytest.mark.parametrize(
    ("rights", "mask", "allowed"),
    [
        ("R", 0x1, True),
        ("AD", 0x4, True),
        ("DC", 0x40, False),
        ("WDAC", 0x40000, False),
        ("WD", 0x2, False),
        ("WA", 0x100, False),
    ],
    ids=["read", "add-subdirectory", "delete-child", "write-dac", "add-file", "write-attributes"],
)
def test_native_ancestor_grants_are_checked_before_record_content(computer, monkeypatch, rights, mask, allowed):
    parent = computer / "native-ancestor"
    parent.mkdir(mode=0o700)
    manifest = _store(parent / "data", monkeypatch)
    before = manifest.stat().st_ino, manifest.read_bytes()
    assert base.load_records() == {}
    _icacls(parent, "/grant", f"*{USERS}:({rights})")
    try:
        _, grants = folders.folder_grants(parent)
        actual = [grant for grant in grants if grant.sid == USERS and not grant.inherit_only]
        assert actual, "actual native ancestor grant was not applied"
        assert any(grant.mask & mask for grant in actual), "native grant does not contain the selected bit"
        assert folders.private_folder_problem(data_dir()) is None
        local = _load_observed(monkeypatch)  # R28: in local mode the ancestor is gone past, whatever it grants
        assert local[1] is None and local[2] == {} and local[0] == [manifest]
        _connected(data_dir())
        opened, failure, records = _load_observed(monkeypatch)
        print(f"EC5_NATIVE_ANCESTOR rights={rights} observed=True opens={len(opened)} refused={failure is not None}")
        assert (manifest.stat().st_ino, manifest.read_bytes()) == before
        if allowed:
            assert failure is None, "harmless native ancestor access was refused"
            assert records == {}
            assert opened == [manifest], "harmless native ancestor positive did not read the actual manifest"
        else:
            assert failure is not None, "mutable native ancestor was accepted before record content"
            assert opened == [], "mutable native ancestor allowed a record open"
            assert "Integration records unavailable" in str(failure)
    finally:
        _icacls(parent, "/remove:g", "*" + USERS)


def test_native_inherit_only_parent_and_effective_child_are_distinguished(computer, monkeypatch):
    parent = computer / "inheritance-parent"
    parent.mkdir(mode=0o700)
    _icacls(parent, "/grant", f"*{USERS}:(OI)(CI)(IO)(WD)")
    try:
        _, grants = folders.folder_grants(parent)
        assert any(grant.sid == USERS and grant.inherit_only and grant.mask & 0x2 for grant in grants)
        direct = _store(parent / "direct-private", monkeypatch)
        assert base.load_records() == {}, "inherit-only parent grant rejected a genuinely private child"
        assert direct.read_bytes() == b"{}"
        child = parent / "effective-child"
        child.mkdir()  # Ordinary inheritance, rather than Python's special private-0700 ACL.
        _, grants = folders.folder_grants(child)
        assert any(grant.sid == USERS and not grant.inherit_only and grant.mask & 0x2 for grant in grants)
        manifest = _store(child / "private-data", monkeypatch)
        _connected(child / "private-data")
        before = manifest.stat().st_ino, manifest.read_bytes()
        opened, failure, _ = _load_observed(monkeypatch)
        print(f"EC5_NATIVE_INHERITANCE effective_child=True opens={len(opened)} refused={failure is not None}")
        assert (manifest.stat().st_ino, manifest.read_bytes()) == before
        assert failure is not None, "effective inherited mutation on an ancestor was ignored"
        assert opened == [], "effective inherited mutation permitted manifest content access"
    finally:
        _icacls(parent, "/remove:g", "*" + USERS)


def test_safe_missing_native_store_creation_preserves_private_components(computer, monkeypatch):
    parent = computer / "new-private-ancestor"
    folder = parent / "data"
    monkeypatch.setenv("LATET_DATA_DIR", str(folder))
    assert not parent.exists()
    assert base.load_records() == {}
    assert not parent.exists(), "read-only absence created an ancestor"
    base.save_records({"owned": base.Record("owned", 1, data={"marker": "native private creation"})})
    for path in (parent, folder):
        assert not path.is_symlink()
        assert not path.is_junction()
        assert folders.private_folder_problem(path) is None
    assert base.load_records()["owned"].data == {"marker": "native private creation"}


def test_missing_native_component_junction_race_refuses_before_descent(computer, monkeypatch):
    missing = computer / "missing-component"
    folder = missing / "data"
    outside = computer / "owned-outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "untouched.txt"
    sentinel.write_bytes(b"outside native sentinel")
    before = sentinel.stat().st_ino, sentinel.read_bytes()
    monkeypatch.setenv("LATET_DATA_DIR", str(folder))
    actual_mkdir, planted = Path.mkdir, []

    def at_creation(path, *arguments, **keywords):
        if path == missing and not planted:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(missing), str(outside)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if result.returncode:
                pytest.skip("owned native missing-component junction creation unavailable")
            assert missing.is_junction()
            planted.append(True)
        return actual_mkdir(path, *arguments, **keywords)

    failure = None

    def no_output(*arguments, **keywords):
        pytest.fail("integration save reached a content write through the substituted namespace")

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "mkdir", at_creation)
            scoped.setattr(base, "write_atomic", no_output)
            try:
                base.save_records({"owned": base.Record("owned", 1)})
            except base.RecordsUnavailable as error:
                failure = error
        assert planted == [True], "actual existing mkdir boundary was not reached"
        descended = (outside / "data").exists()
        print(f"EC5_NATIVE_MISSING_RACE junction=True outside_child_created={descended} refused={failure is not None}")
        assert (sentinel.stat().st_ino, sentinel.read_bytes()) == before
        assert not descended, "missing-component race created a data folder through the planted junction"
        assert failure is not None, "missing-component substitution was accepted"
        assert not (outside / "integrations.json").exists()
    finally:
        if planted:
            assert missing.is_junction(), "owned missing-component cleanup lost its junction identity"
            missing.rmdir()  # The exact owned junction only; never recursively delete its target.
        created = outside / "data"
        if created.exists():
            assert created.is_dir()
            assert not created.is_symlink()
            assert not created.is_junction()
            created.rmdir()  # Current RED creates this empty owned directory before its later link refusal.
        assert (sentinel.stat().st_ino, sentinel.read_bytes()) == before


@pytest.mark.parametrize(("kind", "declared"), [(0x00, 4), (0x01, 2)], ids=["short-allow", "short-deny"])
def test_existing_acl_parser_refuses_short_declared_ace_layouts(computer, monkeypatch, kind, declared):
    # Allocated backing buffers exceed every legacy offset: the malformed declaration never causes a real OOB read.
    owner = ctypes.create_string_buffer(32)
    acl = ctypes.create_string_buffer(128)
    ace = (ctypes.c_char * 64).from_buffer(acl, 8)
    header = folders._Acl.from_buffer(acl)
    header.AclRevision, header.AclSize, header.AceCount = 2, 128, 1
    entry = folders._AceHeader.from_buffer(ace)
    entry.AceType, entry.AceFlags, entry.AceSize = kind, 0, declared
    sid_calls, released = [], []
    me = folders.my_sid()

    def descriptor(_path, _kind, _information, owner_pointer, _group, acl_pointer, _sacl, result_pointer):
        ctypes.cast(owner_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(owner)
        ctypes.cast(acl_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(acl)
        ctypes.cast(result_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(acl)
        return 0

    def get_ace(_acl, _index, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(ace)
        return True

    def sid_text(pointer):
        sid_calls.append(pointer)
        return me

    api = SimpleNamespace(GetNamedSecurityInfoW=descriptor, GetAce=get_ace)
    kernel = SimpleNamespace(LocalFree=lambda pointer: released.append(pointer))
    failure = None
    with monkeypatch.context() as scoped:
        scoped.setattr(folders, "_windows", lambda: (api, kernel))
        scoped.setattr(folders, "_sid_text", sid_text)
        try:
            folders.folder_grants(computer)
        except OSError as error:
            failure = error
    print(f"EC5_ACE_LAYOUT declared={declared} kind={kind} sid_decodes={len(sid_calls)} refused={failure is not None}")
    assert len(released) == 1, "existing descriptor ownership was not released exactly once"
    assert failure is not None, "existing ACL parser accepted a short declared ACE layout"
    assert len(sid_calls) == 1, "malformed ACE reached SID decoding after owner decoding"
