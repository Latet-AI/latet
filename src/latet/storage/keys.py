"""The per-install root key: in the OS keyring, or a private file when no keyring is available."""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import secrets
from pathlib import Path

from latet.fileio import FileReadError, read_regular_bytes
from latet.logline import Paced, described
from latet.secret_text import KEY_BYTES, KEY_REQUIREMENT, key_problem
from latet.storage.folders import make_private_folder

log = logging.getLogger(__name__)

_SERVICE = "latet"
_USER = "root-key"
_REVIEWER = "reviewer-api-key"
_REVIEWER_FILE = "reviewer.key"
_REVIEWER_KEY_UNAVAILABLE = Paced()


def _from_keyring() -> bytes | None:
    try:
        import keyring

        value = keyring.get_password(_SERVICE, _USER)
    except Exception as exc:  # no backend, locked keychain, headless Linux...
        log.info("keyring unavailable: %s", described(exc))
        return None
    return base64.b64decode(value) if value else None


def _to_keyring(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(_SERVICE, _USER, base64.b64encode(key).decode())
    except Exception as exc:
        log.info("keyring unavailable: %s", described(exc))
        return False
    return True


def load_bridge_token(data_dir: Path) -> str:
    """The token the Claude Code plugin presents to the local bridge (stable across restarts)."""
    env = os.environ.get("LATET_BRIDGE_TOKEN")
    if env:
        return env
    path = data_dir / "bridge.token"
    if path.exists():
        return path.read_text().strip()
    make_private_folder(data_dir)
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return token


def _keyring_on() -> bool:
    # Off in the container: a keyring there would forget the key at every restart, and the vault
    # written with it could no longer be read.
    return os.environ.get("LATET_KEYRING", "").lower() not in ("off", "0", "false", "no")


def load_root_key(data_dir: Path) -> bytes:
    """The root key, created on first use. Losing it only makes new surrogates differ."""
    env = os.environ.get("LATET_ROOT_KEY")
    if env:
        return base64.b64decode(env)
    if secret := os.environ.get("LATET_ROOT_KEY_FILE"):  # a Docker secret, for one
        return base64.b64decode(Path(secret).read_text().strip())
    key = _from_keyring() if _keyring_on() else None
    if key:
        return key
    fallback = data_dir / "root.key"
    if fallback.exists():
        return base64.b64decode(fallback.read_text().strip())
    key = secrets.token_bytes(32)
    if not _keyring_on() or not _to_keyring(key):
        make_private_folder(data_dir)
        fallback.write_text(base64.b64encode(key).decode())
        with contextlib.suppress(OSError):
            os.chmod(fallback, 0o600)
    return key


def keyring_state() -> str:
    """``off`` (LATET_KEYRING), ``ok``, or ``unreachable`` (no backend, a locked keychain, a
    session without D-Bus...): then a key saved there could not be read back."""
    if not _keyring_on():
        return "off"
    try:
        import keyring

        keyring.get_password(_SERVICE, _USER)
    except Exception as exc:
        log.info("keyring unavailable: %s", described(exc))
        return "unreachable"
    return "ok"


def save_root_key(data_dir: Path, key: bytes, aside: Path | None = None) -> str:
    """Make ``key`` the root key ``load_root_key`` finds (``latet restore``): in the keyring when it
    is on, else in ``root.key``. Returns where. A ``root.key`` left next to a keyring is moved to
    ``aside`` (it would be read if the keyring ever failed). Checked by reading it back."""
    from latet.config import write_atomic

    fallback = data_dir / "root.key"
    if _keyring_on():
        if not _to_keyring(key):
            raise OSError("the system's keyring cannot be written")
        if fallback.exists():
            if aside is None:
                raise OSError("root.key would be left next to the keyring")
            make_private_folder(aside)
            os.replace(fallback, aside / "root.key")
        where = "keyring"
    else:
        make_private_folder(data_dir)
        write_atomic(fallback, base64.b64encode(key).decode(), private=True)
        where = "file"
    if load_root_key(data_dir) != key:
        raise OSError("the key read back is another one (LATET_ROOT_KEY or LATET_ROOT_KEY_FILE is set?)")
    return where


def load_reviewer_key(data_dir: Path) -> str | None:
    """The local reviewer's API key saved from the dashboard (or `latet llm key`), if any."""
    if _keyring_on():
        try:
            import keyring

            value = keyring.get_password(_SERVICE, _REVIEWER)
        except Exception as exc:
            log.info("keyring unavailable: %s", described(exc))
            value = None
        if value:
            if key_problem(value) is None:
                return value
            if _REVIEWER_KEY_UNAVAILABLE.due():
                log.warning("the local reviewer's saved keyring key is unusable")
            return None  # an invalid selected value never falls through to a different credential
    try:
        content = read_regular_bytes(data_dir / _REVIEWER_FILE, limit=KEY_BYTES, follow=True)
        value = content.decode("utf-8-sig").strip()
    except FileReadError as exc:
        if exc.reason == "missing":
            return None
        value = None
    except UnicodeDecodeError:
        value = None
    if value == "":  # a genuinely empty saved file retains its absent-source behavior
        return None
    if value is not None and key_problem(value) is None:
        return value
    if _REVIEWER_KEY_UNAVAILABLE.due():
        log.warning("the local reviewer's saved file key is unusable")
    return None


def save_reviewer_key(data_dir: Path, key: str) -> str:
    """Keep the reviewer's API key in the OS keyring, or in a private file when there is none.
    Returns where: ``keyring`` or ``file``."""
    if key_problem(key) is not None:
        raise ValueError(KEY_REQUIREMENT)
    if _keyring_on():
        try:
            import keyring

            keyring.set_password(_SERVICE, _REVIEWER, key)
        except Exception as exc:
            log.info("keyring unavailable: %s", described(exc))
        else:
            (data_dir / _REVIEWER_FILE).unlink(missing_ok=True)
            return "keyring"
    make_private_folder(data_dir)
    path = data_dir / _REVIEWER_FILE
    path.write_text(key, "utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return "file"


def remove_reviewer_key(data_dir: Path) -> bool:
    """Forget the saved key. Whether there was one."""
    had = load_reviewer_key(data_dir) is not None
    if _keyring_on():
        try:
            import keyring

            keyring.delete_password(_SERVICE, _REVIEWER)
        except Exception as exc:  # none saved there, or no keyring
            log.debug("no reviewer key in the keyring: %s", described(exc))
    (data_dir / _REVIEWER_FILE).unlink(missing_ok=True)
    return had
