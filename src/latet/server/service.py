"""Machine service installation, with a separate policy for the service's private files.

Command construction is pure. Installation accepts only trusted machine paths and
never repairs an unsafe existing tree by recursively taking its ownership or ACL.
Windows descriptors are read by SID through an open, no-follow metadata handle;
the personal-computer policy in storage.folders remains unchanged.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from latet.config import write_atomic
from latet.storage.folders import ADMINISTRATORS, SYSTEM, TRUSTED_INSTALLER

UNIT_NAME = "latet-server.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
LINUX_CONFIG = Path("/etc/latet/server.yaml")
SERVICE_NAME = "LatetServer"
# Windows' service SID is the five little-endian words of the uppercase UTF-16
# service name's SHA-1. It exists independently of service registration or locale.
_SERVICE_DIGEST = hashlib.sha1(SERVICE_NAME.upper().encode("utf-16-le"), usedforsecurity=False).digest()
SERVICE_SID = "S-1-5-80-" + "-".join(str(int.from_bytes(_SERVICE_DIGEST[n : n + 4], "little")) for n in range(0, 20, 4))
FULL, READ_EXECUTE, MODIFY = 0x1F01FF, 0x1200A9, 0x1301BF
_NAMESPACE_SAFE = 0xA01200AD  # Read/traverse and add-subdirectory, never replacement or delete-child.
_READ_ONLY = 0xA01200A9
_MOST_ENTRIES = 10_000
SECRET_SETTINGS = (
    "master_key_file",
    "database_password_file",
    "valkey_password_file",
    "metrics_token_file",
    "reviewer_key_file",
    "key",
)


class InstallError(RuntimeError):
    """An installation refusal whose words are fixed and contain no process output."""


@dataclass(frozen=True)
class WindowsACE:
    sid: str
    mask: int
    flags: int
    kind: int = 0


@dataclass(frozen=True)
class WindowsSecurity:
    owner: str | None
    protected: bool
    entries: tuple[WindowsACE, ...]
    directory: bool


def platform() -> str:
    if sys.platform == "win32":
        return "windows"
    return "linux" if sys.platform.startswith("linux") else "other"


def _system_directory() -> PureWindowsPath:
    if sys.platform != "win32":
        return PureWindowsPath("C:/Windows/System32")  # Pure construction on other platforms.
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise InstallError("The Windows system directory cannot be determined.")
    return PureWindowsPath(buffer.value)


def windows_folder() -> Path:
    if sys.platform != "win32":
        return Path("C:/ProgramData/Latet")
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(260)
    if shell.SHGetFolderPathW(None, 0x23, None, 0, buffer) != 0:  # CSIDL_COMMON_APPDATA, from the OS, not environment.
        raise InstallError("The Windows service directory cannot be determined.")
    return Path(buffer.value) / "Latet"


def homes(kind: str) -> list[Path]:
    roots = [Path.home()]
    if kind == "windows":
        roots.append(Path(str(_system_directory().anchor)) / "Users")
    elif sys.platform != "win32":
        import pwd

        roots.extend(Path(entry.pw_dir) for entry in pwd.getpwall() if entry.pw_dir and entry.pw_dir != "/")
        roots.extend((Path("/home"), Path("/root")))
    return roots


def in_a_home(places: Sequence[PurePath], homes: Sequence[PurePath]) -> PurePath | None:
    return next((place for place in places if any(place.is_relative_to(home) for home in homes)), None)


def privileged(kind: str) -> bool:
    if kind == "windows" and sys.platform == "win32":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return kind == "linux" and getattr(os, "geteuid", lambda: -1)() == 0


def _arguments(command: list[str]) -> None:
    if not command or not command[0]:
        raise ValueError("The service command has no executable.")
    if any(
        not isinstance(word, str) or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in word)
        for word in command
    ):
        raise ValueError("Service arguments cannot contain control characters.")


def systemd_unit(command: list[str]) -> str:
    _arguments(command)

    def argument(word: str) -> str:
        escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        return f'"{escaped}"'

    return (
        "[Unit]\n"
        "Description=Latet server\n"
        "Documentation=https://docs.latet.ai/concepts/server-operations/\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=exec\nUser=latet\nGroup=latet\n"
        f"ExecStart={' '.join(argument(word) for word in command)}\n"
        "Environment=LATET_DATA_DIR=/var/lib/latet LATET_KEYRING=off\n"
        "StateDirectory=latet\nRestart=on-failure\nRestartSec=5\n"
        "NoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
        "PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\n"
        "ProtectControlGroups=yes\nRestrictSUIDSGID=yes\nLockPersonality=yes\nUMask=0077\n"
        "CapabilityBoundingSet=\nRestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n"
        "SystemCallFilter=@system-service\n\n[Install]\nWantedBy=multi-user.target\n"
    )


def windows_commands(command: list[str], folder: Path | None = None) -> list[list[str]]:
    """SCM commands only; private directories are created atomically with a full DACL."""
    _arguments(command)
    if folder is not None:
        _arguments([str(folder)])
    sc = str(_system_directory() / "sc.exe")
    return [
        [
            sc,
            "create",
            SERVICE_NAME,
            "binPath=",
            subprocess.list2cmdline(command),
            "start=",
            "disabled",
            "obj=",
            f"NT SERVICE\\{SERVICE_NAME}",
            "DisplayName=",
            "Latet server",
        ],
        [sc, "description", SERVICE_NAME, "Latet on a server: masks what AI agents send."],
        [sc, "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/60000"],
        [sc, "failureflag", SERVICE_NAME, "1"],
        [sc, "config", SERVICE_NAME, "start=", "delayed-auto"],
    ]


def windows_sddl(
    *, writable: bool = False, directory: bool = True, owner: str | None = ADMINISTRATORS, principal: str = SERVICE_SID
) -> str:
    """The complete descriptor used at creation, displayed in the installation plan."""
    flags = "OICI" if directory else ""
    mask = MODIFY if writable else READ_EXECUTE
    if not principal.startswith("S-1-") or any(part and not part.isdecimal() for part in principal.split("-")[1:]):
        raise ValueError("A numeric Windows principal is required.")
    ownership = f"O:{owner}G:BA" if owner is not None else ""
    return f"{ownership}D:P(A;{flags};0x{FULL:x};;;SY)(A;{flags};0x{FULL:x};;;BA)(A;{flags};0x{mask:x};;;{principal})"


def acl_problem(metadata: WindowsSecurity | object, folder: str, *, writable: bool = False) -> str | None:
    """Exact service descriptor policy. Text from icacls is never authorization."""
    return _exact_acl_problem(metadata, SERVICE_SID, writable=writable)


def _exact_acl_problem(metadata: object, principal: str, *, writable: bool) -> str | None:
    if not isinstance(metadata, WindowsSecurity):
        return "service access metadata cannot be read"
    owners = {SYSTEM, ADMINISTRATORS, principal} if writable else {SYSTEM, ADMINISTRATORS}
    if metadata.owner not in owners:
        return "service path has an untrusted owner"
    if metadata.protected is not True:
        return "service access list is not protected"
    flags = 3 if metadata.directory else 0
    expected = {
        WindowsACE(SYSTEM, FULL, flags),
        WindowsACE(ADMINISTRATORS, FULL, flags),
        WindowsACE(principal, MODIFY if writable else READ_EXECUTE, flags),
    }
    if len(metadata.entries) != 3 or set(metadata.entries) != expected:
        return "service access list does not match its required principals and rights"
    return None


def log_file_problem(path: Path, principal: str) -> str | None:
    """A protected log leaf inside the stream's already verified private directory.

    The caller supplies its actual current SID; this separate writable-file policy
    also serves standalone streams without widening the service or personal policy.
    """
    try:
        metadata = windows_security(path)
        if metadata.directory:
            return "log path is not a regular file"
        return _exact_acl_problem(metadata, principal, writable=True)
    except (OSError, ValueError, TypeError):
        return "log file metadata cannot be read"


def _windows_api() -> tuple[Any, Any]:
    """Use the existing SID reader's libraries, adding only handle-bound metadata calls."""
    if sys.platform != "win32":
        raise OSError("Windows metadata unavailable")
    from ctypes import wintypes

    from latet.storage import folders

    advapi, kernel = folders._windows()
    pointer = ctypes.POINTER(ctypes.c_void_p)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        pointer,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    return advapi, kernel


def windows_security(path: Path) -> WindowsSecurity:
    """Read all descriptor entries and file type, never file bytes or a followed reparse target."""
    if sys.platform != "win32":
        raise OSError("Windows metadata unavailable")
    from ctypes import wintypes

    from latet.storage import folders

    advapi, kernel = _windows_api()
    handle = kernel.CreateFileW(str(path), 0x20080, 3, None, 3, 0x02200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError("Windows metadata unavailable")
    descriptor = ctypes.c_void_p()
    try:
        # FILE_ATTRIBUTE_TAG_INFO is two DWORDs; asking by handle binds type to the ACL read below.
        attributes = (wintypes.DWORD * 2)()
        if (
            not kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes))
            or attributes[0] & 0x400
        ):
            raise OSError("Windows metadata unavailable")
        owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        if advapi.GetSecurityInfo(
            handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        ):
            raise OSError("Windows metadata unavailable")
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError("Windows metadata unavailable")
        if not owner.value or not dacl.value or not control.value & 4:
            raise OSError("Windows metadata unavailable")
        acl = folders._Acl.from_address(dacl.value)
        if acl.AclRevision not in (2, 4) or acl.AclSize < ctypes.sizeof(folders._Acl) or acl.AceCount > 128:
            raise OSError("Windows metadata unavailable")
        end, previous = dacl.value + acl.AclSize, dacl.value + ctypes.sizeof(folders._Acl)
        entries = []
        for index in range(acl.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise OSError("Windows metadata unavailable")
            if ace.value < previous or ace.value + 8 > end:
                raise OSError("Windows metadata unavailable")
            header = folders._AceHeader.from_address(ace.value)
            if header.AceSize < 16 or ace.value + header.AceSize > end or header.AceType not in (0, 1):
                raise OSError("Windows metadata unavailable")
            count = ctypes.c_ubyte.from_address(ace.value + 9).value
            if ctypes.c_ubyte.from_address(ace.value + 8).value != 1 or count > 15 or header.AceSize != 16 + 4 * count:
                raise OSError("Windows metadata unavailable")
            entries.append(
                WindowsACE(
                    folders._sid_text(ace.value + 8),
                    wintypes.DWORD.from_address(ace.value + 4).value,
                    header.AceFlags,
                    header.AceType,
                )
            )
            previous = ace.value + header.AceSize
        return WindowsSecurity(
            folders._sid_text(owner.value), bool(control.value & 0x1000), tuple(entries), bool(attributes[0] & 0x10)
        )
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        kernel.CloseHandle(handle)


def _windows_trust(metadata: WindowsSecurity, *, namespace: bool, writable: bool = False) -> str | None:
    trusted = {SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER}
    if writable:
        trusted.add(SERVICE_SID)
    if metadata.owner not in trusted:
        return "machine path has an untrusted owner"
    safe = _NAMESPACE_SAFE if namespace else _READ_ONLY
    for entry in metadata.entries:
        if entry.kind not in (0, 1) or not entry.sid or entry.flags & ~0x1F:
            return "machine path access metadata cannot be read"
        if entry.kind == 1 or entry.flags & 8:
            continue
        if entry.sid not in trusted and entry.mask & ~safe:
            return "machine path can be changed by another account"
    return None


def _plain(path: Path) -> os.stat_result:
    found = path.lstat()
    if stat.S_ISLNK(found.st_mode) or getattr(found, "st_file_attributes", 0) & 0x400:
        raise OSError("linked paths are not accepted")
    if not (stat.S_ISREG(found.st_mode) or stat.S_ISDIR(found.st_mode)):
        raise OSError("plain files or directories are required")
    if stat.S_ISREG(found.st_mode) and found.st_nlink != 1:
        raise OSError("multiply linked files are not accepted")
    return found


def _namespace_problem(
    path: Path, *, writable: bool = False, personal: bool = False, service_identity: tuple[int, int] | None = None
) -> str | None:
    try:
        if not path.is_absolute():
            return "an absolute machine path is required"
        for parent in reversed(path.parents):
            found = _plain(parent)
            if not stat.S_ISDIR(found.st_mode):
                return "machine path ancestor is not a directory"
            if platform() == "windows":
                metadata = windows_security(parent)
                if not metadata.directory:
                    return "machine path ancestor is not a directory"
                if problem := _windows_trust(metadata, namespace=True, writable=writable):
                    return problem
            elif personal:
                from latet.storage.folders import namespace_folder_problem

                if problem := namespace_folder_problem(parent, found):
                    return problem
            else:
                if found.st_uid != 0 or found.st_mode & 0o022:
                    return "machine path ancestor can be changed by another account"
                if service_identity is not None:
                    uid, gid = service_identity
                    execute = 0o100 if found.st_uid == uid else 0o010 if found.st_gid == gid else 0o001
                    if not found.st_mode & execute:
                        return "machine path ancestor cannot be traversed by the service"
    except (OSError, ValueError):
        return "machine path metadata cannot be read"
    return None


def windows_path_problem(path: Path, *, writable: bool = False) -> str | None:
    if problem := _namespace_problem(path, writable=writable):
        return problem
    try:
        return acl_problem(windows_security(path), "", writable=writable)
    except (OSError, ValueError, TypeError):
        return "service access metadata cannot be read"


def folder_problem(path: Path, *, writable: bool = False) -> str | None:
    try:
        if platform() == "windows":
            metadata = windows_security(path)
            if not metadata.directory:
                return "service path is not a directory"
            return _namespace_problem(path, writable=writable) or acl_problem(metadata, "", writable=writable)
        found = _plain(path)
        if not stat.S_ISDIR(found.st_mode):
            return "service path is not a directory"
        if found.st_uid not in (0, getattr(os, "getuid", lambda: -1)()) or found.st_mode & 0o077:
            return "service directory is not private"
        return _namespace_problem(path, personal=True)
    except (OSError, ValueError, TypeError):
        return "service access metadata cannot be read"


def _linux_service_file_problem(path: Path, found: os.stat_result) -> str | None:
    """The systemd unit explicitly runs with User=latet and Group=latet."""
    try:
        import grp
        import pwd

        getpwnam, getgrnam = getattr(pwd, "getpwnam", None), getattr(grp, "getgrnam", None)
        if not callable(getpwnam) or not callable(getgrnam):
            return "the service identity cannot be verified"
        uid = getattr(getpwnam("latet"), "pw_uid", None)
        gid = getattr(getgrnam("latet"), "gr_gid", None)
        if type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0:
            return "the service identity cannot be verified"
    except (ImportError, KeyError, OSError, ValueError, TypeError, AttributeError):
        return "the service identity cannot be verified"
    if found.st_uid != 0 or found.st_gid != gid or stat.S_IMODE(found.st_mode) not in (0o440, 0o640):
        return "the file must be root-owned and readable only by root and the service group"
    return _namespace_problem(path, service_identity=(uid, gid))


def secret_file_problem(path: Path, *, machine_service: bool = False) -> str | None:
    """Inspect metadata only; this function must never open a secret for its contents."""
    try:
        if platform() == "windows":
            metadata = windows_security(path)
            if metadata.directory:
                return "secret path is not a regular file"
            return _namespace_problem(path) or acl_problem(metadata, "")
        found = _plain(path)
        if not stat.S_ISREG(found.st_mode):
            return "secret path is not a regular file"
        if machine_service or (found.st_uid == 0 and stat.S_IMODE(found.st_mode) in (0o440, 0o640)):
            return _linux_service_file_problem(path, found)
        if found.st_uid not in (0, getattr(os, "getuid", lambda: -1)()) or found.st_mode & 0o077:
            return "secret file is not private"
        return _namespace_problem(path, personal=True)
    except (OSError, ValueError, TypeError):
        return "secret file metadata cannot be read"


def trusted_path_problem(path: Path) -> str | None:
    """Machine provenance: ownership and replacement rights, including each ancestor."""
    if problem := _namespace_problem(path):
        return problem
    try:
        # A machine-owned Python symlink is normal in POSIX virtual environments.
        # Its parent, its owner and its fully resolved destination must all be trusted.
        if platform() != "windows" and stat.S_ISLNK(path.lstat().st_mode):
            if path.lstat().st_uid != 0:
                return "machine path has an untrusted owner"
            path = path.resolve(strict=True)
            if problem := _namespace_problem(path):
                return problem
        found = _plain(path)
        if platform() == "windows":
            return _windows_trust(windows_security(path), namespace=False)
        if found.st_uid != 0 or found.st_mode & 0o022:
            return "machine path can be changed by another account"
    except (OSError, ValueError):
        return "machine path metadata cannot be read"
    return None


def _package_path() -> Path:
    import latet

    return Path(latet.__file__).parent


def configuration_values(config: Path | None = None) -> dict[str, Any]:
    """Bounded configuration metadata and environment selection, with no secret validators.

    The installer checks the configuration's trusted provenance first. Doctor
    uses the same selection semantics solely for a read-only diagnostic snapshot.
    """
    import yaml

    from latet.config import load_yaml
    from latet.fileio import read_regular_bytes
    from latet.server.settings import _VARIABLES

    selected = config
    if selected is None and os.environ.get("LATET_SERVER_CONFIG"):
        selected = Path(os.environ["LATET_SERVER_CONFIG"])
    values: dict[str, Any] = {}
    try:
        if selected is not None:
            loaded = load_yaml(read_regular_bytes(selected, limit=1024 * 1024, follow=True).decode("utf-8"))
            if loaded is not None:
                if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
                    raise ValueError("server settings must be a mapping")
                values.update(loaded)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError):
        raise InstallError("The server configuration cannot be safely inspected.") from None
    for key, variable in _VARIABLES.items():
        if value := os.environ.get(variable):
            values[key] = value
    return values


def configuration_problem(config: Path) -> str | None:
    """Check every selected secret's metadata before the settings model can read it."""
    try:
        values = configuration_values(config)
    except InstallError:
        return "The server configuration cannot be safely inspected."
    for name in SECRET_SETTINGS:
        value = values.get(name)
        if value is not None and (
            not isinstance(value, str) or not value or secret_file_problem(Path(value), machine_service=True)
        ):
            return f"{name} must name a private, trusted file readable by the service."
    return None


def _package_problem(package: Path) -> str | None:
    """Inspect Latet's actual code tree, including replaceable files below a safe directory."""
    pending, seen = [(package, 0)], 0
    try:
        while pending:
            directory, depth = pending.pop()
            if depth > 64:
                return "package is too deep to verify"
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _MOST_ENTRIES:
                        return "package is too large to verify"
                    path = Path(entry.path)
                    found = _plain(path)
                    if platform() == "windows":
                        if problem := _windows_trust(windows_security(path), namespace=False):
                            return problem
                    elif found.st_uid != 0 or found.st_mode & 0o022:
                        return "package can be changed by another account"
                    if stat.S_ISDIR(found.st_mode):
                        pending.append((path, depth + 1))
    except (OSError, ValueError):
        return "package metadata cannot be read"
    return None


def _account_lookups() -> tuple[Callable[[str], Any], Callable[[str], Any]] | None:
    """The system's lookups of a user and of a group by name; None where there are none (Windows)."""
    try:
        import grp
        import pwd
    except ImportError:
        return None
    getpwnam, getgrnam = getattr(pwd, "getpwnam", None), getattr(grp, "getgrnam", None)
    return (getpwnam, getgrnam) if callable(getpwnam) and callable(getgrnam) else None


def service_identity_problem() -> str | None:
    """Which of the latet user and group that the systemd unit runs as (``User=latet``, ``Group=latet``) this machine
    lacks, in words that say how to create it; None when both are there."""
    lookups = _account_lookups()
    if lookups is None:
        return "The service identity cannot be verified."
    found = []
    for lookup in lookups:
        try:
            lookup("latet")
            found.append(True)
        except KeyError:
            found.append(False)
        except (OSError, ValueError, TypeError):
            return "The service identity cannot be verified."
    user, group = found
    nologin = "--no-create-home --shell /usr/sbin/nologin latet"
    if not user and not group:
        return (
            "The service runs as the latet user and group, which this machine does not have: create them first, as "
            f"root: `useradd --system --user-group {nologin}`."
        )
    if not user:
        return (
            "The service runs as the latet user, which this machine does not have (its latet group is there): create "
            f"it first, as root: `useradd --system --gid latet {nologin}`."
        )
    if not group:
        return (
            "The service runs with the latet group, which this machine does not have (its latet user is there): "
            "create it first, as root: `groupadd --system latet`, then `usermod --gid latet latet`."
        )
    return None


def installation_problem(kind: str, config: Path) -> str | None:
    paths = [Path(sys.executable), Path(sys.prefix), Path(sys.base_prefix), _package_path(), config]
    try:
        resolved = [path.resolve(strict=True) for path in paths]
        if in_a_home([*paths, *resolved], homes(kind)) is not None:
            return (
                "Install Latet for the machine, outside personal homes (UV_TOOL_DIR, UV_TOOL_BIN_DIR and "
                "UV_PYTHON_INSTALL_DIR)."
            )
    except (OSError, ValueError):
        return "The machine installation paths cannot be checked."
    for path in paths:
        if trusted_path_problem(path):
            return "The executable, Python installation, package or configuration is not a trusted machine path."
    if _package_problem(paths[3]):
        return "The Latet package contains an unsafe or unreadable entry."
    if kind == "windows" and secret_file_problem(config):
        return (
            "Protect the configuration's access list: SYSTEM and Administrators own it and have full control; "
            "NT SERVICE\\LatetServer has read-only access. Remove every other entry and inherited access."
        )
    if kind == "linux" and (missing := service_identity_problem()) is not None:
        return missing
    if kind == "linux" and secret_file_problem(config, machine_service=True):
        return "The configuration must be root-owned, readable by the latet group, and protected from other access."
    return None


def _run(command: list[str]) -> None:
    _arguments(command)
    if not Path(command[0]).is_absolute() and not PureWindowsPath(command[0]).is_absolute():
        raise InstallError("The service manager must have an absolute path.")
    try:
        subprocess.run(  # noqa: S603 - fixed absolute system tool, no shell
            command,
            check=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise InstallError("The service manager did not finish within 30 seconds.") from None
    except (subprocess.SubprocessError, OSError):
        raise InstallError("The service manager refused installation.") from None


def install_systemd(text: str, path: Path | None = None) -> None:
    target = path or UNIT_PATH
    tool = Path("/usr/bin/systemctl")
    if _namespace_problem(target) or trusted_path_problem(tool):
        raise InstallError("The systemd installation paths are not trusted.")
    if os.path.lexists(target) and trusted_path_problem(target):
        raise InstallError("The existing systemd unit is unsafe; it was left unchanged.")
    try:
        write_atomic(target, text, private=True)
    except OSError:
        raise InstallError("The systemd unit could not be written.") from None
    _run([str(tool), "daemon-reload"])
    _run([str(tool), "enable", UNIT_NAME])


def _create_private_directory(path: Path, *, writable: bool) -> None:
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    advapi, kernel = _windows_api()
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        windows_sddl(writable=writable), 1, ctypes.byref(descriptor), None
    ):
        raise InstallError("The private service descriptor could not be created.")
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor.value, False)
        kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        kernel.CreateDirectoryW.restype = wintypes.BOOL
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise InstallError("The private service directory could not be created.")
    finally:
        kernel.LocalFree(descriptor)


def _tree_problem(base: Path) -> str | None:
    """Bounded inspection of existing entries. Nothing is repaired, deleted or reassigned."""
    pending, seen = [(base, 0)], 0
    try:
        while pending:
            parent, depth = pending.pop()
            if depth > 64:
                return "service tree is too deep to verify"
            with os.scandir(parent) as children:
                for child in children:
                    seen += 1
                    if seen > _MOST_ENTRIES:
                        return "service tree is too large to verify"
                    path = Path(child.path)
                    relative = path.relative_to(base)
                    writable = relative.parts[0] in ("data", "logs")
                    if problem := windows_path_problem(path, writable=writable):
                        return problem
                    if windows_security(path).directory:
                        pending.append((path, depth + 1))
    except (OSError, ValueError):
        return "service tree metadata cannot be read"
    return None


def install_windows(steps: list[list[str]], folder: Path | None = None) -> None:
    base = folder or windows_folder()
    tool = str(_system_directory() / "sc.exe")
    if trusted_path_problem(Path(tool)):
        raise InstallError("The Windows service manager is not a trusted machine executable.")
    if _namespace_problem(base):
        raise InstallError("The service directory has an unsafe ancestor.")
    existing = os.path.lexists(base)
    if existing and (windows_path_problem(base) or _tree_problem(base)):
        raise InstallError("The existing service directory is unsafe; it was left unchanged.")
    for path, writable in ((base, False), (base / "data", True), (base / "logs", True)):
        if not os.path.lexists(path):
            _create_private_directory(path, writable=writable)
        if windows_path_problem(path, writable=writable):
            raise InstallError("The service directory protection could not be verified.")
    for step in steps:
        if not step or step[0] != tool:
            raise InstallError("Only the Windows service manager may perform these steps.")
        _run(step)
