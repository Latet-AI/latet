"""Trusted Claude setup's private, non-secret helper binding. This module never starts the helper."""

from __future__ import annotations

import hashlib
import json
import ntpath
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from latet.server.connection import _PreparedConnection

HELPER_KEY = "LATET_BRIDGE_HELPER"
_DESCRIPTOR_BYTES = 16 * 1024
_NAME = re.compile(r"(?:permanent|session-[0-9a-f]{32})\.json")
_EMPTY_ENV = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "__COMPAT_LAYER",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSLKEYLOGFILE",
    "OPENSSL_CONF",
    "OPENSSL_CONF_INCLUDE",
    "OPENSSL_ENGINES",
    "OPENSSL_MODULES",
    "OPENSSL_TRACE",
    "LATET_DATA_DIR",
    "LATET_CONNECTION",
    "LATET_CONFIG",
    "LATET_SERVER_URL",
    "LATET_SERVER_FINGERPRINT",
    "LATET_SERVER_ADMIN_TOKEN",
    "LATET_BRIDGE_URL",
    "LATET_BRIDGE_TOKEN",
    HELPER_KEY,
    "LATET_TRUSTED_ACCOUNTS",  # read from the registry alone (R30): blank here all the same
)


@dataclass(frozen=True)
class _BindingPlan:
    prepared: _PreparedConnection = field(repr=False)  # Its token is removed before construction.
    path: Path
    text: str = field(repr=False)
    setting: str = field(repr=False)


_INSTALL_FIX = (
    "install Latet in a folder that only you, SYSTEM and the Administrators can change, and whose parents only they, "
    "the Administrators' direct members and the accounts in LATET_TRUSTED_ACCOUNTS can change, or remove the entry "
    "that lets another account change it (Troubleshooting: Claude Code's helper binding)"
)


# Claude Code's helper is bound on Windows alone in E0c (no binding is made elsewhere): said up front where it matters
# (R5), and no token is written where no binding can be made.
WINDOWS_ONLY = "Claude Code through a Latet server needs a Windows terminal in this version"
HELD_BACK = (
    f"{WINDOWS_ONLY}: on this computer its plugin holds back every prompt, so nothing reaches its provider without "
    "Latet; `latet codex` and `latet opencode` go through the server"
)


def platform_problem() -> str | None:
    """Why Claude Code cannot go through a Latet server from this computer (``HELD_BACK``), or None on Windows."""
    return None if sys.platform == "win32" else HELD_BACK


def installation_problem() -> str | None:
    """Why no helper binding can be made for Claude Code here, as far as it is known before anything is written: the
    platform, or the part of the installation that fails and why (``_install_path``). None when one can be."""
    from latet.server.connection import NotConnected

    if (problem := platform_problem()) is not None:
        return problem
    try:
        _installation()
    except NotConnected as exc:
        return exc.message
    except (OSError, ValueError):
        return "Claude Code's helper binding is unavailable: the installation cannot be inspected"
    return None


def binding_problem(prepared: _PreparedConnection) -> str | None:
    """For `latet doctor`: why Claude Code, applied for good on this connected computer with the token in its settings,
    has no valid helper binding (none outside Windows; none written; one made for another installation, version or
    connection), in words that say what to do. None when the binding on disk is the one `latet agents apply claude`
    would write now. Reads the descriptor, never writes or repairs it."""
    from latet.integrations import base
    from latet.server.connection import NotConnected

    if (problem := platform_problem()) is not None:
        return problem
    descriptor = _binding_path(prepared.selected.data)
    try:
        plan = _plan_binding(prepared, descriptor)
    except NotConnected as exc:
        return exc.message
    if plan is None:
        return "Claude Code's helper binding cannot be made: this computer's token is missing (`latet status`)"
    again = "`latet agents apply claude` makes it again"
    try:
        if base._metadata(descriptor) is None:
            return f"Claude Code's helper binding is missing, so its plugin holds back every prompt: {again}"
        raw = base._checked_bytes(descriptor, _DESCRIPTOR_BYTES + 1, connected=True)
    except base.RecordsUnavailable as exc:
        return f"Claude Code's helper binding cannot be read ({exc.said or exc.reason}): {again}"
    if raw != plan.text.encode("utf-8"):
        return (
            "Claude Code's helper binding was made for another installation, version or connection (an upgrade or a "
            f"new connection since), so its plugin holds back every prompt: {again}"
        )
    return None


def _installation() -> tuple[Path, Path, Path, Path, Path]:
    """Python's program, its environment and installation, Latet's package and the Windows folder, each admitted by
    ``_install_path``: what the helper runs. Raises NotConnected naming the part that failed and why."""
    if not sys.executable:
        raise _refused("Python", "this Python does not say where its program is (sys.executable)")
    return (
        _install_path(Path(sys.executable), directory=False, what="Python (sys.executable)"),
        _install_path(Path(sys.prefix), directory=True, what="Python's environment (sys.prefix)"),
        _install_path(Path(sys.base_prefix), directory=True, what="Python's installation (sys.base_prefix)"),
        _install_path(Path(__file__).parent, directory=True, what="Latet's package"),
        _windows_root(),
    )


def _refused(what: str | None = None, why: str | None = None):
    """The helper binding refused: with ``what`` (which part of the installation: Python, sys.prefix,
    sys.base_prefix, Latet's package, the Windows folder) and ``why`` (the folder and what makes it unsafe: its owner,
    the account and rights its access list grants), the fix; the fixed words otherwise. None of it is a secret."""
    from latet.server.connection import NotConnected

    if what is not None and why is not None:
        return NotConnected(f"Claude Code's helper binding is unavailable: {what}: {why}; {_INSTALL_FIX}")
    return NotConnected(
        "Claude Code's helper binding is unavailable: use trusted private installation and data folders"
    )


def _install_path(path: Path, *, directory: bool, what: str = "an installation folder") -> Path:
    """Resolve installation links only during setup; read permission is allowed, foreign mutation is not. A refusal
    names ``what`` failed, the folder and why (R3)."""
    from latet.integrations import base
    from latet.storage import folders

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise _refused(what, f"{path} cannot be found") from None
    if not re.fullmatch(r"[A-Za-z]:", resolved.drive):
        raise _refused(what, f"{resolved} is not on a drive letter of this computer")
    try:  # an installation's own fix follows (``_refused``): never a data folder's
        if not base._namespace(resolved if directory else resolved.parent, fix="", connected=True):
            raise _refused(what, f"{resolved} cannot be found")
        found = base._plain_path(resolved, directory=directory, fix="")
    except base.RecordsUnavailable as exc:
        raise _refused(what, exc.said or f"{resolved} cannot be inspected") from None
    if not directory and not stat.S_ISREG(found.st_mode):
        raise _refused(what, f"{resolved} is not a plain file")
    if folders.namespace_folder_problem(resolved, found) is not None:
        said = folders.namespace_refusal(resolved, found, fix="")
        raise _refused(what, said or f"another account may change {resolved}")
    trusted = {folders.my_sid(), folders.SYSTEM, folders.ADMINISTRATORS, folders.TRUSTED_INSTALLER}
    try:
        owner, grants = folders.folder_grants(resolved)
    except OSError:
        raise _refused(what, f"the owner and access list of {resolved} cannot be read") from None
    if owner not in trusted:
        raise _refused(what, f"{resolved} belongs to {folders.account_name(owner) if owner else 'no owner'}")
    for grant in grants:
        if (
            not isinstance(grant.sid, str)
            or not grant.sid
            or type(grant.mask) is not int
            or not 0 <= grant.mask <= 0xFFFFFFFF
            or type(grant.inherit_only) is not bool
        ):
            raise _refused(what, f'{resolved} has an access-list entry Latet cannot read (`icacls "{resolved}"`)')
        if (
            grant.inherit_only
            or grant.sid in trusted
            or grant.sid == folders.OWNER_RIGHTS
            or folders._app_grant(grant.sid)
        ):
            continue
        if grant.mask & (~0xA01200A9 & 0xFFFFFFFF):  # Existing namespace read/traverse mask, without add-subdirectory.
            raise _refused(
                what,
                f"{resolved} lets {folders.account_name(grant.sid)} change it (its access list grants "
                f'{folders._rights(grant.mask)}; `icacls "{resolved}"` lists it)',
            )
    return resolved


def _windows_root() -> Path:
    if sys.platform != "win32":
        raise _refused("the Windows folder", "this is not Windows")
    import ctypes
    from ctypes import wintypes

    get = ctypes.WinDLL("kernel32", use_last_error=True).GetWindowsDirectoryW
    get.argtypes, get.restype = (wintypes.LPWSTR, wintypes.UINT), wintypes.UINT
    text = ctypes.create_unicode_buffer(32768)
    length = get(text, len(text))
    if not 0 < length < len(text):
        raise _refused("the Windows folder", "Windows does not say where it is")
    root = _install_path(Path(text.value), directory=True, what="the Windows folder")
    _install_path(root / "System32", directory=True, what="the Windows folder")
    return root


def _launch_env(root: Path, windows: Path) -> dict[str, str]:
    user = root / "homes" / "user"
    env: dict[str, str] = {name: "" for name in _EMPTY_ENV}
    env.update(
        {
            "PATH": str(windows / "System32"),
            "SystemRoot": str(windows),
            "WINDIR": str(windows),
            "HOME": str(user),
            "USERPROFILE": str(user),
            "HOMEDRIVE": user.drive,
            "HOMEPATH": str(user)[len(user.drive) :],
            "TEMP": str(root / "temp"),
            "TMP": str(root / "temp"),
            "TMPDIR": str(root / "temp"),
            "LATET_KEYRING": "off",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )
    for name, child in (
        ("CODEX_HOME", "codex"),
        ("CLAUDE_CONFIG_DIR", "claude"),
        ("XDG_CONFIG_HOME", "xdg"),
        ("APPDATA", "roaming"),
        ("LOCALAPPDATA", "local"),
    ):
        env[name] = str(root / "homes" / child)
    return env


def _binding_path(data: Path, name: str = "permanent.json") -> Path:
    if not _NAME.fullmatch(name):
        raise _refused()
    return data / "connection" / "claude-helper" / name


def _plan_binding(prepared: _PreparedConnection, descriptor: Path) -> _BindingPlan | None:
    """Compute without creating output or retaining the eligible token; unsupported surfaces have no binding."""
    if prepared.connection is None or prepared.token is None or sys.platform != "win32":
        return None
    from dataclasses import replace

    from latet import __version__
    from latet.integrations.base import RecordsUnavailable
    from latet.server.connection import _agent_base, _prepare_output, _require_selection

    _require_selection(prepared)
    for selected in (prepared.selected.data, prepared.selected.path):
        if not selected.is_absolute() or not re.fullmatch(r"[A-Za-z]:", selected.drive) or ".." in selected.parts:
            raise _refused()
    if descriptor != _binding_path(prepared.selected.data, descriptor.name):
        raise _refused()
    try:
        _prepare_output(descriptor.parent, "Claude Code's helper binding is not written")
        python, prefix, base_prefix, package, windows = _installation()
        launch: dict[str, Any] = {
            "v": 1,
            "python": str(python),
            "cwd": str(descriptor.parent / "cwd"),
            "descriptor": str(descriptor),
            "env": _launch_env(descriptor.parent, windows),
        }
        bound = prepared.connection
        space = _agent_base(prepared)
        body = {
            "v": 1,
            "kind": "claude-bridge",
            "data": str(prepared.selected.data),
            "connection": str(prepared.selected.path),
            "origin": bound.url,
            "identity": bound.user,
            "fingerprint": bound.fingerprint,
            "space_sha256": hashlib.sha256(space.encode("utf-8")).hexdigest(),
            "install": {
                "python": str(python),
                "prefix": str(prefix),
                "base_prefix": str(base_prefix),
                "latet_root": str(package),
                "latet_version": __version__,
            },
            "launch": launch,
        }
        text = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > _DESCRIPTOR_BYTES or any(
            secret in text for secret in (prepared.token, space, space.rsplit("/", 1)[-1])
        ):
            raise _refused()
        _require_selection(prepared)
        return _BindingPlan(replace(prepared, token=None), descriptor, text, json.dumps(launch, separators=(",", ":")))
    except (OSError, ValueError, RecordsUnavailable):
        raise _refused() from None


def _remove_binding(path: Path) -> None:
    """Only a derived owned descriptor name, beneath admitted private state; never a record-selected path."""
    from latet.integrations import base
    from latet.server.connection import _prepare_output

    if not _NAME.fullmatch(path.name) or path.parent.name != "claude-helper" or path.parent.parent.name != "connection":
        raise _refused()
    try:
        _prepare_output(path.parent, "Claude Code's helper binding is not removed")
        found = base._metadata(path)
        if found is None:
            return
        found = base._plain_path(path)
        if not stat.S_ISREG(found.st_mode):
            raise _refused()
        path.unlink()
    except (OSError, base.RecordsUnavailable):
        raise _refused() from None


def _remove_owned_bindings(data: Path) -> None:
    """Disconnect's fixed helper names only; an unsafe/unavailable tree is retained, never traversed or repaired."""
    from latet.integrations import base
    from latet.server.connection import NotConnected

    try:
        if ".." in data.parts:
            raise _refused()
        data = data if data.is_absolute() else Path.cwd() / data
        root = _binding_path(data).parent
        if not base._namespace(root, connected=True):
            return
        base._trusted_folder(root, connected=True)
        for path in root.iterdir():
            if _NAME.fullmatch(path.name):
                _remove_binding(path)  # Revalidates the parent and plain regular descriptor before unlinking.
    except (OSError, base.RecordsUnavailable, NotConnected):
        raise base.RecordsUnavailable(
            "Claude Code's helper bindings retained: the helper folder or an owned descriptor is unsafe or unavailable."
        ) from None


def _publish_binding(plan: _BindingPlan) -> None:
    from latet.config import write_atomic
    from latet.integrations import base
    from latet.server.connection import _prepare_output, _require_selection

    _require_selection(plan.prepared)
    root = plan.path.parent
    children = (
        "",
        "cwd",
        "homes",
        "homes/user",
        "homes/codex",
        "homes/claude",
        "homes/xdg",
        "homes/roaming",
        "homes/local",
        "temp",
    )
    for child in children:
        _prepare_output(root / child, "Claude Code's helper binding is not written", create=True)
    if any((root / "cwd").iterdir()):
        raise _refused()
    _remove_binding(plan.path)
    _require_selection(plan.prepared)
    write_atomic(plan.path, plan.text, private=True)
    try:
        if base._checked_bytes(plan.path, _DESCRIPTOR_BYTES + 1, connected=True) != plan.text.encode("utf-8"):
            raise _refused()
        _require_selection(plan.prepared)
    except Exception:
        _remove_binding(plan.path)
        raise


def _read_binding(path: Path, decode, incoming: dict[str, str | None]) -> tuple[_PreparedConnection, str]:
    """Read one bounded private descriptor snapshot; never repair state or obtain a credential."""
    from latet import __version__
    from latet.integrations import base
    from latet.server.connection import _load_connection, _PreparedConnection, _SelectedConnection

    # The binding's own data folder, named explicitly: the helper reads it while LATET_DATA_DIR names its empty cwd,
    # and the data folder and its connection folder are checked with no trust in another account as parents (M3).
    own = path.parent.parent.parent
    base._trusted_folder(path.parent, connected=True, data_folder=own)
    raw = base._checked_bytes(path, _DESCRIPTOR_BYTES + 1, connected=True, data_folder=own)
    value = decode(raw, _DESCRIPTOR_BYTES)
    fields = {
        "v",
        "kind",
        "data",
        "connection",
        "origin",
        "identity",
        "fingerprint",
        "space_sha256",
        "install",
        "launch",
    }
    if not isinstance(value, dict) or set(value) != fields or type(value["v"]) is not int or value["v"] != 1:
        raise _refused()
    if value["kind"] != "claude-bridge":
        raise _refused()

    def absolute(text):
        if not isinstance(text, str) or "\0" in text:
            raise _refused()
        selected = Path(text)
        if not selected.is_absolute() or not re.fullmatch(r"[A-Za-z]:", selected.drive) or ".." in selected.parts:
            raise _refused()
        return selected

    data, selected = absolute(value["data"]), absolute(value["connection"])
    if path != _binding_path(data, path.name):
        raise _refused()
    launch, install = value["launch"], value["install"]
    if not isinstance(launch, dict) or set(launch) != {"v", "python", "cwd", "descriptor", "env"}:
        raise _refused()
    if type(launch["v"]) is not int or launch["v"] != 1:
        raise _refused()
    if absolute(launch["cwd"]) != path.parent / "cwd" or absolute(launch["descriptor"]) != path:
        raise _refused()
    if Path.cwd() != absolute(launch["cwd"]):
        raise _refused()
    expected_env = _launch_env(path.parent, _windows_root())
    # CPython consumes this one launcher selector during startup; the launch map
    # still requires its explicit empty value before exec, like every other selector.
    if launch["env"] != expected_env or any(
        (current := incoming.get(key)) != text
        and not (key == "__PYVENV_LAUNCHER__" and text == "" and current is None)
        and not (key == "SystemRoot" and isinstance(current, str) and ntpath.normcase(current) == ntpath.normcase(text))
        for key, text in expected_env.items()
    ):
        raise _refused()
    if not isinstance(install, dict) or set(install) != {
        "python",
        "prefix",
        "base_prefix",
        "latet_root",
        "latet_version",
    }:
        raise _refused()
    expected = {
        "python": _install_path(Path(sys.executable), directory=False),
        "prefix": _install_path(Path(sys.prefix), directory=True),
        "base_prefix": _install_path(Path(sys.base_prefix), directory=True),
        "latet_root": _install_path(Path(__file__).parent, directory=True),
    }
    if any(absolute(install[key]) != current for key, current in expected.items()):
        raise _refused()
    if absolute(launch["python"]) != expected["python"] or install["latet_version"] != __version__:
        raise _refused()
    if not isinstance(value["space_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["space_sha256"]):
        raise _refused()
    bound = _SelectedConnection(data, selected)
    connection = _load_connection(bound)
    if connection is None or (connection.url, connection.user, connection.fingerprint) != (
        value["origin"],
        value["identity"],
        value["fingerprint"],
    ):
        raise _refused()
    # Environment-based legacy imports can now see only validated binding roots, not inherited selections.
    import os

    os.environ["LATET_DATA_DIR"], os.environ["LATET_CONNECTION"] = str(data), str(selected)
    return _PreparedConnection(bound, connection), value["space_sha256"]
