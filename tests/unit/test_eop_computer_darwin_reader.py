"""Post-implementation EC5-D public-API simulations, not pre-fix RED or native macOS evidence."""

import ctypes
import errno
import stat
from types import SimpleNamespace

import pytest

from latet.storage import folders

from .test_eop_computer_darwin import SAFE, _dispatch


def _reader_fixture(computer, monkeypatch, entries):
    selected = computer / "acl-directory"
    selected.mkdir(mode=0o700)
    runtime, observed = _dispatch(monkeypatch, selected, selected, entries)
    return selected, runtime.metadata(selected), runtime, observed


def _finished(runtime, observed):
    assert observed["content"] == [], "metadata reader attempted actual content access"
    assert not runtime.descriptors, "simulated directory descriptor was retained"
    assert len(runtime.closed) == len(runtime.opens), "simulated directory close was omitted or repeated"
    assert sorted(runtime.freed) == sorted(runtime.acls), "simulated ACL free was omitted or repeated"


def test_simulated_darwin_reader_uses_opaque_public_abi_and_full_uint64(computer, monkeypatch):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((1, 1 << 63), (2, 0x10), (1, 0)))
    selectors, original = [], runtime.acl_get_entry

    def entry(handle, selector, pointer):
        selectors.append(runtime.scalar(selector))
        return original(handle, selector, pointer)

    monkeypatch.setattr(runtime, "acl_get_entry", entry)
    assert folders._darwin_allows(selected, found) == [1 << 63, 0]
    assert selectors == [0, -1, -1, -1]
    expected = {
        "acl_get_fd_np": ((ctypes.c_int, ctypes.c_int), ctypes.c_void_p),
        "acl_valid": ((ctypes.c_void_p,), ctypes.c_int),
        "acl_get_entry": ((ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)), ctypes.c_int),
        "acl_get_tag_type": ((ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)), ctypes.c_int),
        "acl_get_permset_mask_np": ((ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)), ctypes.c_int),
        "acl_free": ((ctypes.c_void_p,), ctypes.c_int),
    }
    for name, signature in expected.items():
        function = getattr(runtime.library, name)
        assert (tuple(function.argtypes), function.restype) == signature
    _finished(runtime, observed)


@pytest.mark.parametrize("number", [errno.ENOENT, 0, errno.EINVAL, errno.EACCES, errno.ENOTSUP, errno.EIO])
def test_simulated_darwin_absent_acl_is_only_null_enoent_on_bound_fd(computer, monkeypatch, number):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, None)
    original, reached = runtime.acl_get_fd_np, []

    def absent(descriptor, kind):
        assert ctypes.get_errno() == 0, "ACL retrieval did not clear stale errno immediately before its call"
        reached.append(descriptor)
        assert original(descriptor, kind) is None  # Independently verifies a currently owned bound descriptor.
        ctypes.set_errno(number)
        return None

    monkeypatch.setattr(runtime, "acl_get_fd_np", absent)
    ctypes.set_errno(errno.EIO)
    failure, result = None, None
    try:
        result = folders._darwin_allows(selected, found)
    except OSError as error:
        failure = error
    assert len(reached) == 1
    if number == errno.ENOENT:
        assert failure is None
        assert result == []
    else:
        assert failure is not None, "non-ENOENT or zero-errno ACL failure became mode-only trust"
        assert str(failure) == "Darwin directory ACL metadata unavailable"
    _finished(runtime, observed)


@pytest.mark.parametrize(
    ("result", "number"),
    [(-1, 0), (-1, errno.EIO), (1, 0), (0, 0)],
    ids=["end-without-einval", "error-is-not-end", "linux-one-is-not-entry", "null-entry"],
)
def test_simulated_darwin_iterator_accepts_only_documented_terminal_pair(computer, monkeypatch, result, number):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((1, 2),))
    reached = []

    def incorrect(handle, selector, pointer):
        reached.append(runtime.scalar(selector))
        ctypes.set_errno(number)
        return result  # Leaves the output entry NULL, including the malformed success case.

    monkeypatch.setattr(runtime, "acl_get_entry", incorrect)
    with pytest.raises(OSError, match=r"^Darwin directory ACL metadata unavailable$"):
        folders._darwin_allows(selected, found)
    assert reached == [0]
    _finished(runtime, observed)


@pytest.mark.parametrize("count", [128, 129])
def test_simulated_darwin_acl_has_128_entries_and_one_exhaustion_check(computer, monkeypatch, count):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((2, 0x10),) * count)
    original, calls = runtime.acl_get_entry, []

    def entry(handle, selector, pointer):
        calls.append(runtime.scalar(selector))
        assert len(calls) <= 129, "reader exceeded the finite ACL cursor budget"
        return original(handle, selector, pointer)

    monkeypatch.setattr(runtime, "acl_get_entry", entry)
    if count == 128:
        assert folders._darwin_allows(selected, found) == []
    else:
        with pytest.raises(OSError, match=r"^Darwin directory ACL metadata unavailable$"):
            folders._darwin_allows(selected, found)
    assert len(calls) == 129
    assert calls[0] == 0
    assert all(selector == -1 for selector in calls[1:])
    _finished(runtime, observed)


@pytest.mark.parametrize(
    "phase",
    [
        "load",
        "missing-symbol",
        "missing-flag",
        "open",
        "fstat",
        "identity-device",
        "identity-inode",
        "identity-owner",
        "identity-mode",
        "identity-type",
        "valid",
        "tag",
        "unknown-tag",
        "mask",
        "free-return",
        "free-error",
        "close-error",
        "both-cleanup-errors",
        "body-plus-cleanup",
    ],
)
def test_simulated_darwin_reader_refuses_errors_and_attempts_independent_cleanup(computer, monkeypatch, phase):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((1, 2),))

    def denied(*arguments, **keywords):
        raise OSError("planted_Darwin_API_failure_9741")

    if phase == "load":
        monkeypatch.setattr(ctypes, "CDLL", denied)
    elif phase == "missing-symbol":
        monkeypatch.delattr(runtime.library, "acl_get_permset_mask_np")
    elif phase == "missing-flag":
        monkeypatch.setattr(runtime, "O_NOFOLLOW", 0)
    elif phase in ("open", "fstat"):
        monkeypatch.setattr(runtime, phase, denied)
    elif phase.startswith("identity-"):
        original = runtime.fstat
        field = {"device": "st_dev", "inode": "st_ino", "owner": "st_uid", "mode": "st_mode", "type": "st_mode"}[
            phase.removeprefix("identity-")
        ]

        def different(descriptor):
            changed = vars(original(descriptor)).copy()
            changed[field] = stat.S_IFREG | 0o700 if phase == "identity-type" else changed[field] + 1
            return SimpleNamespace(**changed)

        monkeypatch.setattr(runtime, "fstat", different)
    elif phase in ("valid", "tag", "mask"):
        name = {"valid": "acl_valid", "tag": "acl_get_tag_type", "mask": "acl_get_permset_mask_np"}[phase]
        monkeypatch.setattr(runtime, name, lambda *arguments: -1)
    elif phase == "unknown-tag":

        def unknown(entry, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = 99
            return 0

        monkeypatch.setattr(runtime, "acl_get_tag_type", unknown)
    if phase in ("free-return", "free-error", "both-cleanup-errors", "body-plus-cleanup"):
        original_free = runtime.acl_free

        def free(handle):
            original_free(handle)  # Records the attempted native cleanup; an error is still ambiguous.
            if phase == "free-return":
                return -1
            raise OSError("planted_Darwin_free_failure_9741")

        monkeypatch.setattr(runtime, "acl_free", free)
    if phase in ("close-error", "both-cleanup-errors", "body-plus-cleanup"):
        original_close = runtime.close

        def close(descriptor):
            original_close(descriptor)
            raise OSError("planted_Darwin_close_failure_9741")

        monkeypatch.setattr(runtime, "close", close)
    if phase == "body-plus-cleanup":
        monkeypatch.setattr(runtime, "acl_valid", lambda handle: -1)
    with pytest.raises(OSError, match=r"^Darwin directory ACL metadata unavailable$"):
        folders._darwin_allows(selected, found)
    _finished(runtime, observed)


@pytest.mark.parametrize(
    ("mask", "allowed"),
    [
        (0, True),
        (2, True),
        (8, True),
        (0x80, True),
        (0x200, True),
        (0x800, True),
        (0x100000, True),
        (SAFE, True),
        (4, False),
        (0x10, False),
        (0x20, False),
        (0x40, False),
        (0x100, False),
        (0x400, False),
        (0x1000, False),
        (0x2000, False),
        (1, False),
        (1 << 63, False),
    ],
    ids=[
        "zero",
        "list",
        "search",
        "read-attributes",
        "read-extended",
        "read-security",
        "synchronize",
        "all-safe",
        "add-file",
        "delete",
        "add-subdir",
        "delete-child",
        "write-attributes",
        "write-extended",
        "write-security",
        "change-owner",
        "unknown-low",
        "unknown-high",
    ],
)
def test_simulated_darwin_ancestor_mask_classification_is_full_width(computer, monkeypatch, mask, allowed):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((1, mask),))
    assert (folders.namespace_folder_problem(selected, found) is None) is allowed
    _finished(runtime, observed)


@pytest.mark.parametrize("tag", [0, 3, 0xFFFFFFFF])
def test_simulated_darwin_unknown_tag_refuses_even_with_zero_mask(computer, monkeypatch, tag):
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, ((tag, 0),))
    assert folders.namespace_folder_problem(selected, found) is not None
    _finished(runtime, observed)


@pytest.mark.parametrize("scope", ["ancestor", "private"])
def test_simulated_darwin_deny_never_cancels_any_potentially_applicable_allow(computer, monkeypatch, scope):
    # Qualifier/flags are deliberately not resolved: self/root/inherit-only ALLOWs receive this same conservative rule.
    entries = ((2, 0xFFFFFFFFFFFFFFFF), (1, 0x10 if scope == "ancestor" else 0))
    selected, found, runtime, observed = _reader_fixture(computer, monkeypatch, entries)
    result = (
        folders.namespace_folder_problem(selected, found)
        if scope == "ancestor"
        else folders.private_folder_problem(selected)
    )
    assert result is not None
    _finished(runtime, observed)
