"""Your installation in one encrypted file: ``latet backup`` writes it, ``latet restore`` reads it.

The file is one line ``LATET-BACKUP``, one line of header (the format's version, and the Argon2id
parameters and salt the key is derived with from your passphrase), then the body: a tar stream cut in
chunks of 1 MiB, each sealed with AES-256-GCM. A chunk's nonce is its number and whether it is the
last one, and every chunk is bound to the header: a chunk changed, removed, moved or cut off is
refused before anything is used (the STREAM construction).

Inside: a manifest, config.yaml, the reviewer's trusted certificates and, only when asked, the vault
with its root key (and the dashboard's history). Never the bridge token, the reviewer's API key,
logs, models, or what global-install wrote in the agents' settings: each machine has its own.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from latet.storage.folders import make_private_folder

MAGIC = b"LATET-BACKUP\n"
FORMAT = 1
CHUNK = 1 << 20
TAG = 16
MIN_PASSPHRASE = 12
# Argon2id: memory in KiB. A file's own parameters are refused past the limits: chosen by whoever
# made the file, they could otherwise exhaust this machine's memory.
KDF = {"memory": 128 * 1024, "iterations": 3, "lanes": 4}
KDF_LIMITS = {"memory": (8 * 1024, 1024 * 1024), "iterations": (1, 20), "lanes": (1, 16)}
RESTORE_JOURNAL = "restore.json"  # while it exists, the daemon does not start: a restore is under way
# What a backup may hold, each with its largest size.
_SMALL = 4 * 1024 * 1024
_MEMBERS = {
    "manifest.json": _SMALL,
    "config.yaml": _SMALL,
    "root.key": 1024,
    "latet.db": 4 * 1024**3,
    "exchanges.db": 32 * 1024**3,
}
_CERTIFICATE = re.compile(r"^certificates/[0-9a-f]{64}\.pem$")
_MAX_CERTIFICATES = 100


class BackupError(ValueError):
    """A backup that cannot be read: another passphrase, a damaged file, a newer format."""


def restoring(data: Path) -> bool:
    """Whether a restore is under way (or was cut off): the daemon must not start on half of it."""
    return (data / RESTORE_JOURNAL).exists()


# --- the sealed stream ------------------------------------------------------------------------------


def _key(passphrase: str, salt: bytes, kdf: dict[str, int]) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    return Argon2id(
        salt=salt, length=32, iterations=kdf["iterations"], lanes=kdf["lanes"], memory_cost=kdf["memory"]
    ).derive(passphrase.encode("utf-8"))


def _nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


class _Sealer(io.RawIOBase):
    """Where tarfile writes: each full chunk sealed as it fills, the last one by ``finish``."""

    def __init__(self, out: BinaryIO, seal: Callable[[bytes, bytes], bytes]) -> None:
        self._out = out
        self._seal = seal
        self._buffer = bytearray()
        self._counter = 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._buffer += data
        while len(self._buffer) > CHUNK:  # one byte at least stays: the last chunk is sealed at the end
            self._write(bytes(self._buffer[:CHUNK]), last=False)
            del self._buffer[:CHUNK]
        return len(data)

    def finish(self) -> None:
        self._write(bytes(self._buffer), last=True)
        self._buffer.clear()

    def _write(self, chunk: bytes, last: bool) -> None:
        self._out.write(self._seal(_nonce(self._counter, last), chunk))
        self._counter += 1


class _Opener(io.RawIOBase):
    """What tarfile reads: each chunk opened (and so verified) before any of it is given out."""

    def __init__(self, src: BinaryIO, open_: Callable[[bytes, bytes], bytes]) -> None:
        self._src = src
        self._open = open_
        self._counter = 0
        self._next_record = src.read(CHUNK + TAG)
        self._plain = b""
        self._pos = 0
        self.done = False

    def readable(self) -> bool:
        return True

    def _advance(self) -> None:
        from cryptography.exceptions import InvalidTag

        record = self._next_record
        if not record:
            raise BackupError("the file is cut short")
        following = self._src.read(CHUNK + TAG)
        last = not following
        try:
            self._plain = self._open(_nonce(self._counter, last), record)
        except InvalidTag:
            raise BackupError("the passphrase is wrong, or the file is damaged") from None
        self._pos = 0
        self._counter += 1
        self._next_record = following
        self.done = last

    def readinto(self, buffer: Any) -> int:
        while self._pos >= len(self._plain):
            if self.done:
                return 0
            self._advance()
        n = min(len(buffer), len(self._plain) - self._pos)
        buffer[:n] = self._plain[self._pos : self._pos + n]
        self._pos += n
        return n

    def drain(self) -> None:
        """Read to the end: tar stops at its own end marker, the last chunk must still be seen."""
        while not self.done:
            self._advance()
        self._pos = len(self._plain)


def _header(kdf: dict[str, int], salt: bytes) -> bytes:
    body = {"format": FORMAT, "kdf": {"name": "argon2id", **kdf}, "salt": base64.b64encode(salt).decode()}
    return MAGIC + json.dumps(body, separators=(",", ":")).encode() + b"\n"


def _read_header(src: BinaryIO) -> tuple[bytes, dict[str, int], bytes]:
    magic = src.readline(64)
    if magic != MAGIC:
        raise BackupError("not a Latet backup")
    line = src.readline(4096)
    try:
        header = json.loads(line)
        version, kdf, salt = header["format"], header["kdf"], base64.b64decode(header["salt"])
    except (ValueError, KeyError, TypeError):
        raise BackupError("its header cannot be read") from None
    if not isinstance(version, int) or version > FORMAT:
        raise BackupError("made by a newer Latet: update Latet to restore it")
    if not isinstance(kdf, dict) or kdf.get("name") != "argon2id" or not 16 <= len(salt) <= 64:
        raise BackupError("its key derivation is not one Latet knows")
    params = {}
    for name, (low, high) in KDF_LIMITS.items():
        value = kdf.get(name)
        if not isinstance(value, int) or not low <= value <= high:
            raise BackupError(f"its key derivation asks for {name} {value}, outside what Latet accepts")
        params[name] = value
    return magic + line, params, salt


# --- writing ------------------------------------------------------------------------------------------


@dataclass
class Contents:
    """What a backup holds, as its manifest says."""

    created: str
    version: str
    platform: str
    vault: bool
    history: bool
    values: int | None = None  # in the vault
    certificates: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT, "created": self.created, "latet": self.version, "platform": self.platform,
            "vault": self.vault, "history": self.history, "values": self.values, "certificates": self.certificates,
        }  # fmt: skip


def _info(name: str, size: int) -> tarfile.TarInfo:
    """A member as tar records it: no owner, no user name, private."""
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(time.time())
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def snapshot(source: Path, into: Path) -> int | None:
    """A consistent copy of a database, also while the daemon writes to it (one read transaction):
    the rows stay sealed as they are. The number of values, for the vault; None when it is not there."""
    if not source.exists():
        return None
    into.unlink(missing_ok=True)
    db = sqlite3.connect(source, isolation_level=None)
    try:
        db.execute("VACUUM INTO ?", (str(into),))
    finally:
        db.close()
    copy = sqlite3.connect(into)
    try:
        has = copy.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entities'").fetchone()
        return int(copy.execute("SELECT count(*) FROM entities").fetchone()[0]) if has else 0
    finally:
        copy.close()


def write_backup(
    path: Path,
    passphrase: str,
    data: Path,
    config_file: Path,
    *,
    vault: bool,
    history: bool,
    root_key: bytes | None = None,
    kdf: dict[str, int] | None = None,
) -> Contents:
    """Write the backup to ``path`` (whole or not at all, readable by you alone)."""
    from latet import __version__

    if history and not vault:
        raise BackupError("the history needs the vault: it is sealed with the vault's key")
    if vault and root_key is None:
        raise BackupError("the vault needs its root key")
    kdf = kdf or KDF
    salt = secrets.token_bytes(16)
    header = _header(kdf, salt)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead = AESGCM(_key(passphrase, salt, kdf))
    temps: list[Path] = []
    partial = path.with_name(path.name + ".partial")
    try:
        members: list[tuple[str, Path | bytes]] = []
        contents = Contents(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), __version__, sys.platform, vault, history
        )
        if config_file.exists():
            members.append(("config.yaml", config_file.read_bytes()))
        certificates = sorted((data / "certificates").glob("*.pem")) if (data / "certificates").is_dir() else []
        for pem in certificates[:_MAX_CERTIFICATES]:
            if _CERTIFICATE.match(f"certificates/{pem.name}"):
                members.append((f"certificates/{pem.name}", pem.read_bytes()))
                contents.certificates += 1
        if vault:
            assert root_key is not None
            copy = data / f".latet.db.{os.getpid()}.backup"
            temps.append(copy)
            contents.values = snapshot(data / "latet.db", copy)
            if contents.values is None:
                raise BackupError("there is no vault here yet")
            members.append(("root.key", base64.b64encode(root_key)))
            members.append(("latet.db", copy))
        if history:
            copy = data / f".exchanges.db.{os.getpid()}.backup"
            temps.append(copy)
            if snapshot(data / "exchanges.db", copy) is not None:
                members.append(("exchanges.db", copy))
            else:
                contents.history = False
        members.insert(0, ("manifest.json", json.dumps(contents.as_dict(), indent=1).encode()))
        with partial.open("wb") as out:
            out.write(header)
            sealer = _Sealer(out, lambda nonce, chunk: aead.encrypt(nonce, chunk, header))
            with tarfile.open(fileobj=sealer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for name, source in members:
                    if isinstance(source, bytes):
                        tar.addfile(_info(name, len(source)), io.BytesIO(source))
                    else:
                        with source.open("rb") as handle:
                            tar.addfile(_info(name, source.stat().st_size), handle)
            sealer.finish()
            out.flush()
            os.fsync(out.fileno())
        partial.chmod(0o600)
        os.replace(partial, path)
        return contents
    finally:
        partial.unlink(missing_ok=True)
        for temp in temps:
            temp.unlink(missing_ok=True)


# --- reading ------------------------------------------------------------------------------------------


@dataclass
class Opened:
    """A backup read and verified: small parts in memory, the databases staged next to their place."""

    contents: Contents
    config: bytes | None
    root_key: bytes | None
    staged: dict[str, Path] = field(default_factory=dict)  # latet.db, exchanges.db -> a file in ``data``
    certificates: dict[str, bytes] = field(default_factory=dict)  # file name -> PEM

    def discard(self) -> None:
        for staged in self.staged.values():
            staged.unlink(missing_ok=True)
        self.staged.clear()


def read_backup(path: Path, passphrase: str, data: Path) -> Opened:
    """Open and verify the whole backup. The databases are written next to their place in ``data``
    (``.latet.db.restoring``...), and are only used once every chunk was verified."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    make_private_folder(data)
    small: dict[str, bytes] = {}
    certificates: dict[str, bytes] = {}
    staged: dict[str, Path] = {}
    try:
        with path.open("rb") as src:
            header, kdf, salt = _read_header(src)
            aead = AESGCM(_key(passphrase, salt, kdf))
            opener = _Opener(src, lambda nonce, record: aead.decrypt(nonce, record, header))
            try:
                with tarfile.open(fileobj=opener, mode="r|") as tar:
                    for member in tar:
                        name = member.name
                        if not member.isreg() or name in small or name in staged or name in certificates:
                            raise BackupError(f"it holds something it should not: {name}")
                        limit = _MEMBERS.get(name, 1024 * 1024 if _CERTIFICATE.match(name) else -1)
                        if limit < 0 or member.size > limit:
                            raise BackupError(f"it holds something it should not: {name}")
                        handle = tar.extractfile(member)
                        assert handle is not None
                        if name in ("latet.db", "exchanges.db"):
                            target = data / f".{name}.restoring"
                            staged[name] = target
                            with target.open("wb") as out:
                                while block := handle.read(CHUNK):
                                    out.write(block)
                        elif name.startswith("certificates/"):
                            if len(certificates) >= _MAX_CERTIFICATES:
                                raise BackupError("it holds too many certificates")
                            certificates[name.removeprefix("certificates/")] = handle.read()
                        else:
                            small[name] = handle.read()
                opener.drain()
            except tarfile.TarError as exc:
                raise BackupError(f"its contents cannot be read: {exc}") from None
        manifest = _manifest(small.get("manifest.json"))
        root_key = base64.b64decode(small["root.key"]) if "root.key" in small else None
        if manifest.vault and (root_key is None or len(root_key) != 32 or "latet.db" not in staged):
            raise BackupError("its vault is incomplete")
        return Opened(manifest, small.get("config.yaml"), root_key, staged, certificates)
    except BaseException:
        for staged_file in staged.values():
            staged_file.unlink(missing_ok=True)
        raise


def _manifest(raw: bytes | None) -> Contents:
    try:
        data = json.loads(raw or b"")
        return Contents(
            str(data["created"]), str(data["latet"]), str(data["platform"]), bool(data["vault"]),
            bool(data["history"]), data.get("values"), int(data.get("certificates") or 0),
        )  # fmt: skip
    except (ValueError, KeyError, TypeError):
        raise BackupError("its manifest cannot be read") from None
