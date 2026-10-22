"""Post-implementation metadata classifications; simulations are not native platform evidence or pre-fix RED."""

import ctypes
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from latet.integrations import base
from latet.storage import folders

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows predicate and API-buffer classifications")
USERS = "S-1-5-32-545"
PACKAGE, CAPABILITY = "S-1-15-2-1", "S-1-15-3-123"
SAFE = 0xA01200AD


def _decision(computer, monkeypatch, grants, owner="self"):
    me = folders.my_sid()
    if owner == "self":
        owner = me
    monkeypatch.setattr(folders, "folder_grants", lambda path: (owner, grants))
    return folders.namespace_folder_problem(computer, computer.lstat())


@pytest.mark.parametrize(
    "mask",
    [0, 0x1, 0x4, 0x8, 0x20, 0x80, 0x20000, 0x100000, 0x20000000, 0x80000000, SAFE],
    ids=[
        "zero",
        "list",
        "add-subdir",
        "read-ea",
        "traverse",
        "read-attributes",
        "read-control",
        "synchronize",
        "generic-execute",
        "generic-read",
        "all-safe",
    ],
)
def test_namespace_mask_keeps_each_adopted_safe_bit(computer, monkeypatch, mask):
    assert _decision(computer, monkeypatch, [folders.Grant(USERS, mask, False)]) is None


@pytest.mark.parametrize(
    "mask",
    [
        0x2,
        0x10,
        0x40,
        0x100,
        0x10000,
        0x40000,
        0x80000,
        0x10000000,
        0x40000000,
        0x2000000,
        0x1000000,
        0x8000000,
        0x500D0152,
    ],
    ids=[
        "add-file",
        "write-ea",
        "delete-child",
        "write-attributes",
        "delete",
        "write-dac",
        "write-owner",
        "generic-all",
        "generic-write",
        "maximum-allowed",
        "system-security",
        "reserved",
        "known-refused-union",
    ],
)
def test_namespace_mask_refuses_mutation_and_unknown_bits(computer, monkeypatch, mask):
    assert _decision(computer, monkeypatch, [folders.Grant(USERS, mask, False)]) is not None


@pytest.mark.parametrize(
    ("owner", "allowed"),
    [
        ("self", True),
        (folders.SYSTEM, True),
        (folders.ADMINISTRATORS, True),
        (folders.TRUSTED_INSTALLER, True),
        ("S-1-5-21-111-222-333-9999", False),
        (PACKAGE, False),
        (CAPABILITY, False),
        (None, False),
    ],
    ids=["self", "system", "administrators", "trusted-installer", "other-account", "package", "capability", "missing"],
)
def test_namespace_owner_is_checked_independently_of_read_grants(computer, monkeypatch, owner, allowed):
    result = _decision(computer, monkeypatch, [folders.Grant(USERS, 1, False)], owner)
    assert (result is None) is allowed


@pytest.mark.parametrize(
    ("grant", "allowed"),
    [
        (folders.Grant(folders.CREATOR_OWNER, 0, False), False),
        (folders.Grant("S-1-3-1", 0, False), False),
        (folders.Grant(folders.CREATOR_OWNER, 0x1F01FF, True), True),
        (folders.Grant(USERS, 0x1F01FF, True), True),
        (folders.Grant(folders.OWNER_RIGHTS, 0x1F01FF, False), True),
        (folders.Grant("", 0, True), False),
        (folders.Grant("", 0, False), False),
        (folders.Grant(USERS, -1, False), False),
        (folders.Grant(USERS, 0x100000000, False), False),
        (folders.Grant(USERS, True, False), False),
        (folders.Grant(USERS, 0, 1), False),
        (folders.Grant(None, 0, True), False),
    ],
    ids=[
        "effective-creator-owner",
        "effective-creator-group",
        "inherited-creator-owner",
        "inherit-only-other",
        "owner-rights",
        "unknown-before-inherit",
        "unknown-zero",
        "negative-mask",
        "wide-mask",
        "boolean-mask",
        "invalid-inherit-flag",
        "invalid-sid",
    ],
)
def test_namespace_unknown_metadata_precedes_inheritance_and_mask_shortcuts(computer, monkeypatch, grant, allowed):
    assert (_decision(computer, monkeypatch, [grant]) is None) is allowed


@pytest.mark.parametrize(
    ("app", "ordinary_mask", "allowed"),
    [
        (PACKAGE, 1, True),
        (CAPABILITY, 1, True),
        (PACKAGE, 0x40, False),
        (CAPABILITY, 0x100, False),
        ("S-1-15-20-1", 1, False),
        ("S-1-15-2-0001", 1, False),
    ],
    ids=[
        "package-and-read",
        "capability-and-read",
        "package-does-not-waive-delete",
        "capability-does-not-waive-reparse",
        "lookalike-prefix",
        "noncanonical-app-sid",
    ],
)
def test_app_grants_never_waive_ordinary_account_mutation(computer, monkeypatch, app, ordinary_mask, allowed):
    grants = [folders.Grant(app, 0x1F01FF, False), folders.Grant(USERS, ordinary_mask, False)]
    assert (_decision(computer, monkeypatch, grants) is None) is allowed


def test_trusted_installer_ancestor_does_not_broaden_selected_store_privacy(computer, monkeypatch):
    assert _decision(computer, monkeypatch, [], folders.TRUSTED_INSTALLER) is None
    assert folders.private_folder_problem(computer) is not None


def _parsed_acl(computer, monkeypatch, entries):
    """Exercise the real parser with bounded owned API buffers, never malformed OS ACLs."""
    me = folders.my_sid()
    owner = ctypes.create_string_buffer(32)
    acl = ctypes.create_string_buffer(8 + 64 * len(entries or []))
    header = folders._Acl.from_buffer(acl)
    header.AclRevision, header.AclSize, header.AceCount = 2, len(acl), len(entries or [])
    for index, (kind, mask, flags) in enumerate(entries or []):
        offset = 8 + 64 * index
        entry = folders._AceHeader.from_buffer(acl, offset)
        entry.AceType, entry.AceFlags, entry.AceSize = kind, flags, 20
        ctypes.c_uint32.from_buffer(acl, offset + 4).value = mask
        sid = (ctypes.c_ubyte * 12).from_buffer(acl, offset + 8)
        sid[0], sid[1] = 1, 1  # Actual declared SID length/revision; textual decode is the explicit API simulation.
    released = []

    def descriptor(_path, _kind, _information, owner_pointer, _group, acl_pointer, _sacl, result_pointer):
        ctypes.cast(owner_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(owner)
        ctypes.cast(acl_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = (
            ctypes.addressof(acl) if entries is not None else None
        )
        ctypes.cast(result_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(acl)
        return 0

    def get_ace(_acl, index, pointer):
        assert entries is not None
        assert 0 <= index < len(entries)
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(acl) + 8 + 64 * index
        return True

    api = SimpleNamespace(GetNamedSecurityInfoW=descriptor, GetAce=get_ace)
    kernel = SimpleNamespace(LocalFree=lambda pointer: released.append(pointer))
    with monkeypatch.context() as scoped:
        scoped.setattr(folders, "_windows", lambda: (api, kernel))
        scoped.setattr(folders, "_sid_text", lambda pointer: me if pointer == ctypes.addressof(owner) else USERS)
        result = folders.folder_grants(computer)
    assert len(released) == 1
    return result


@pytest.mark.parametrize(
    ("entries", "allowed"),
    [
        ([(0x09, SAFE, 0)], True),
        ([(0x09, 0x40, 0)], False),
        ([(0x01, 0x40, 0), (0x00, 0x40, 0)], False),
        ([(0x7F, 0, 8)], False),
        (None, False),
        ([], True),
        ([(0x01, 0x40, 0)], True),
    ],
    ids=[
        "callback-read",
        "callback-delete",
        "deny-does-not-cancel-allow",
        "unknown-inherit-only",
        "null-dacl",
        "empty-dacl",
        "known-deny-alone",
    ],
)
def test_existing_parser_results_keep_conservative_namespace_classification(computer, monkeypatch, entries, allowed):
    owner, grants = _parsed_acl(computer, monkeypatch, entries)
    assert (_decision(computer, monkeypatch, grants, owner) is None) is allowed


@pytest.mark.parametrize(
    ("owner", "mode", "allowed"),
    [
        (0, 0o755, True),
        (10001, 0o755, True),
        (10001, 0o1777, True),
        (10001, 0o777, False),
        (10002, 0o755, False),
        (10002, 0o1777, False),
    ],
    ids=["root-plain", "self-plain", "self-sticky", "self-nonsticky-writable", "foreign-plain", "foreign-sticky"],
)
def test_posix_namespace_metadata_classification_is_explicitly_simulated(computer, monkeypatch, owner, mode, allowed):
    # Metadata classifications only. Actual Linux ancestor-swap/mode/owner evidence has its separate isolated harness.
    monkeypatch.setattr(folders, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(folders, "os", SimpleNamespace(getuid=lambda: 10001))
    found = SimpleNamespace(st_uid=owner, st_mode=stat.S_IFDIR | mode)
    assert (folders.namespace_folder_problem(computer, found) is None) is allowed


ME = SimpleNamespace(pw_name="person", pw_uid=10001, pw_gid=10001)
SOMEONE = SimpleNamespace(pw_name="someone", pw_uid=10002, pw_gid=10002)


@pytest.mark.parametrize(
    ("gid", "members", "accounts", "alone"),
    [
        (10001, [], [ME, SOMEONE], True),  # Ubuntu's, Debian's, Fedora's own group of each account
        (10001, ["person"], [ME], True),  # listed as its own member too
        (10001, ["someone"], [ME, SOMEONE], False),  # another member
        (10001, [], [ME, SimpleNamespace(pw_name="other", pw_uid=10003, pw_gid=10001)], False),  # another's group
        (20, [], [ME, SOMEONE], False),  # not the account's own group (macOS's staff, a shared group)
    ],
    ids=["own-group", "own-group-listing-itself", "another-member", "another-accounts-primary-group", "not-its-own"],
)
def test_a_group_is_the_accounts_alone_only_when_nobody_else_is_in_it(gid, members, accounts, alone):
    assert folders._group_holds_alone(gid, ME, members, accounts) is alone


@pytest.mark.parametrize(
    ("mode", "gid", "allowed"),
    [(0o775, 10001, True), (0o775, 100, False), (0o777, 10001, False), (0o757, 10001, False)],
    ids=["group-writable-own-group", "group-writable-shared-group", "anyone-writable", "others-writable"],
)
def test_a_folder_its_own_group_may_write_is_changed_by_nobody_else(computer, monkeypatch, mode, gid, allowed):
    """Ubuntu makes `~/.local` 0775 for an account whose group is its own: `latet doctor` warned of it to every person
    there, though nobody else may write in it."""
    monkeypatch.setattr(folders, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(folders, "os", SimpleNamespace(getuid=lambda: 10001))
    monkeypatch.setattr(folders, "_group_is_mine_alone", lambda group: group == 10001)
    found = SimpleNamespace(st_uid=10001, st_gid=gid, st_mode=stat.S_IFDIR | mode)
    assert (folders.namespace_folder_problem(computer, found) is None) is allowed


def test_relative_store_path_keeps_each_actual_component_before_parent_traversal(computer, monkeypatch):
    prefix = computer / "relative-prefix"
    prefix.mkdir(mode=0o700)
    folder = computer / "data"
    folder.mkdir(mode=0o700)
    (folder / "integrations.json").write_bytes(b"{}")
    monkeypatch.chdir(computer)
    monkeypatch.setenv("LATET_DATA_DIR", "relative-prefix/../data")
    actual_metadata, observed = base._metadata, []

    def metadata(path):
        observed.append(path)
        return actual_metadata(path)

    def no_resolve(*arguments, **keywords):
        pytest.fail("record trust normalized a namespace before inspecting its actual components")

    with monkeypatch.context() as scoped:
        scoped.setattr(base, "_metadata", metadata)
        scoped.setattr(Path, "resolve", no_resolve)
        assert base.load_records() == {}
    assert prefix in observed, "actual relative-prefix inspection was normalized away"
    assert prefix / ".." in observed, "actual parent traversal component was normalized away"
    assert (folder / "integrations.json").read_bytes() == b"{}"


def test_foreign_drive_relative_store_refuses_before_any_metadata(computer, monkeypatch):
    drive = "Z:" if computer.drive.upper() != "Z:" else "Y:"
    monkeypatch.setenv("LATET_DATA_DIR", drive + "relative-owned-store")

    def no_metadata(*arguments, **keywords):
        pytest.fail("unanchored drive-relative path reached filesystem metadata")

    monkeypatch.setattr(base, "_metadata", no_metadata)
    with pytest.raises(base.RecordsUnavailable, match="Integration records unavailable"):
        base.load_records()


def test_namespace_acl_unavailability_uses_fixed_metadata_words(computer, monkeypatch):
    def denied(path):
        raise PermissionError("planted_namespace_metadata_9741")

    monkeypatch.setattr(folders, "folder_grants", denied)
    result = folders.namespace_folder_problem(computer, computer.lstat())
    assert result == "namespace access metadata unavailable"
    assert "planted_namespace_metadata_9741" not in result
