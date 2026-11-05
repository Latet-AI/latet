"""Identity-sealed launch bindings and a single stock Redis mutation attempt."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from sys import getsizeof
from typing import TYPE_CHECKING, Literal, TypeGuard, cast

from cryptography.exceptions import InvalidTag
from redis._parsers import _RESP2Parser, _RESP3Parser
from redis.connection import (
    BlockingConnectionPool,
    CacheProxyConnection,
    Connection,
    PythonRespSerializer,
    SSLConnection,
)

from latet.gateway.launch_keys import _CONTENT_KEY, BINDING_BYTES, FOLDER_BYTES
from latet.server.launch_records import ISSUER, LaunchUnavailable, exact_bytes, packed, uint
from latet.server.sealing import DERIVE, Sealer, derived

if TYPE_CHECKING:
    from latet.server.bus import Bus

_PREFIX = re.compile(rb"[a-zA-Z0-9_:.-]{1,96}")
_PROJECT = re.compile(r"[0-9a-f]{12}")
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)
_PARSERS = (_RESP2Parser, _RESP3Parser)
_BorrowedConnection = Connection | CacheProxyConnection


def _component(segment: str, windows: bool) -> None:
    if not segment or segment in (".", ".."):
        raise LaunchUnavailable()
    if windows and (
        segment.endswith((" ", ".")) or _DEVICE.match(segment) or any(char in ':<>"|?*' for char in segment)
    ):
        raise LaunchUnavailable()


def _canonical_depth(path: str, flavor: str) -> int:
    if type(path) is not str or not 0 < len(path) <= FOLDER_BYTES:
        raise LaunchUnavailable()
    encoded = path.encode("utf-8")
    if len(encoded) > FOLDER_BYTES or _CONTENT_KEY.search(encoded) is not None:
        raise LaunchUnavailable()
    del encoded
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise LaunchUnavailable()
    if flavor == "posix":
        separator, windows = "/", False
        if not path.startswith("/") or path.startswith("//"):
            raise LaunchUnavailable()
        start = 1
    elif flavor == "windows":
        separator, windows = "\\", True
        if "/" in path or path.startswith((separator * 2 + "?" + separator, separator * 2 + "." + separator)):
            raise LaunchUnavailable()
        if len(path) >= 3 and "A" <= path[0] <= "Z" and path[1:3] == ":" + separator:
            start = 3
        elif path.startswith(separator * 2):
            server_end = path.find(separator, 2)
            if server_end <= 2:
                raise LaunchUnavailable()
            share_end = path.find(separator, server_end + 1)
            if share_end <= server_end + 1:
                raise LaunchUnavailable()
            _component(path[2:server_end], True)
            _component(path[server_end + 1 : share_end], True)
            start = share_end + 1
        else:
            raise LaunchUnavailable()
    else:
        raise LaunchUnavailable()

    # An anchor alone includes its final separator; a nonempty tail cannot.
    if start < len(path) and path.endswith(separator):
        raise LaunchUnavailable()
    depth = 1
    while start < len(path):
        end = path.find(separator, start)
        if end == -1:
            end = len(path)
        _component(path[start:end], windows)
        depth += 1
        start = end + 1
    return depth


def _text(path: str, flavor: str) -> PurePosixPath | PureWindowsPath:
    _canonical_depth(path, flavor)
    # Keep the compatibility return type lazy; OPEN needs only scalar depth.
    return PureWindowsPath(path) if flavor == "windows" else PurePosixPath(path)


def _relative(path: str, parent: str, separator: str) -> bool:
    return path == parent or (
        path.startswith(parent) and (parent.endswith(separator) or path.startswith(separator, len(parent)))
    )


def _windows_key(path: str) -> bytes:
    return path.lower().encode("utf-8")


def _windows_relative(path: bytes, parent: bytes) -> bool:
    return path == parent or (
        path.startswith(parent) and (parent.endswith(b"\\") or path.startswith(b"\\", len(parent)))
    )


@dataclass(frozen=True, slots=True, repr=False, init=False)
class Binding:
    _folder: bytes
    _root: bytes
    project: str
    source: Literal["server", "client"]
    flavor: Literal["windows", "posix"]

    def __init__(
        self,
        folder: str,
        root: str,
        project: str,
        source: Literal["server", "client"],
        flavor: Literal["windows", "posix"],
    ) -> None:
        try:
            if type(flavor) is not str:
                raise LaunchUnavailable()
            _text(folder, flavor)
            _text(root, flavor)
            relative = (
                _windows_relative(_windows_key(folder), _windows_key(root))
                if flavor == "windows"
                else _relative(folder, root, "/")
            )
            if not relative:
                raise LaunchUnavailable()
            if type(project) is not str or _PROJECT.fullmatch(project) is None:
                raise LaunchUnavailable()
            if type(source) is not str or source not in ("server", "client"):
                raise LaunchUnavailable()
            object.__setattr__(self, "_folder", folder.encode("utf-8"))
            object.__setattr__(self, "_root", root.encode("utf-8"))
        except UnicodeError:
            raise LaunchUnavailable() from None
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "flavor", flavor)

    @property
    def folder(self) -> str:
        return self._folder.decode("utf-8")

    @property
    def root(self) -> str:
        return self._root.decode("utf-8")

    def fields(self) -> tuple[bytes, ...]:
        return (
            self._folder,
            self._root,
            self.project.encode("ascii"),
            self.source.encode("ascii"),
            self.flavor.encode("ascii"),
        )

    def digest(self, auth: bytes, issuer: str, locator: bytes, launch_id: bytes) -> bytes:
        return hmac.digest(
            auth, packed((b"launch-binding-v1", issuer.encode("ascii"), locator, launch_id, *self.fields())), "sha256"
        )

    def contains(self, folder: str) -> bool:
        try:
            return _text(folder, self.flavor).is_relative_to(_text(self.root, self.flavor))
        except (LaunchUnavailable, UnicodeError):
            return False


@dataclass(frozen=True, slots=True, repr=False)
class BindingAddress:
    epoch: bytes
    issuer: str
    locator: bytes
    launch_id: bytes
    generation: int
    publication: bytes

    def fields(self) -> tuple[bytes, ...]:
        if not (
            exact_bytes(self.epoch, 16)
            and type(self.issuer) is str
            and ISSUER.fullmatch(self.issuer)
            and exact_bytes(self.locator, 32)
            and exact_bytes(self.launch_id, 16)
            and uint(self.generation)
            and exact_bytes(self.publication, 16)
        ):
            raise LaunchUnavailable()
        return (
            b"1",
            self.epoch,
            self.issuer.encode("ascii"),
            self.locator,
            self.launch_id,
            str(self.generation).encode("ascii"),
            self.publication,
        )


def binding_key(bus: Bus, epoch: bytes, locator: bytes) -> bytes:
    try:
        prefix = bus.prefix.encode("ascii")
    except UnicodeError:
        raise LaunchUnavailable() from None
    if not (exact_bytes(epoch, 16) and exact_bytes(locator, 32) and _PREFIX.fullmatch(prefix)):
        raise LaunchUnavailable()
    return bus.key(f"launch:{epoch.hex()}:{locator.hex()}").encode("ascii")


def _unpack(raw: bytes, count: int) -> tuple[bytes, ...]:
    position = 0
    result = []
    for _ in range(count):
        end = position + 4
        if end > len(raw):
            raise LaunchUnavailable()
        size = int.from_bytes(raw[position:end], "big")
        position = end + size
        if position > len(raw):
            raise LaunchUnavailable()
        result.append(raw[end:position])
    if position != len(raw):
        raise LaunchUnavailable()
    return tuple(result)


class BindingCodec:
    """A preloaded identity-root codec. Callers project clear keys before submitting work."""

    __slots__ = ("_sealer", "_verifier")

    def __init__(self, root: bytes) -> None:
        if not exact_bytes(root, 32):
            raise LaunchUnavailable()
        self._sealer = Sealer(root, b"launch-binding-v1")
        self._verifier = derived(root, DERIVE, b"launch-key-v1")

    @property
    def retained_bytes(self) -> int:
        sealer = self._sealer
        return (
            getsizeof(self)
            + getsizeof(self._verifier)
            + getsizeof(sealer)
            + getsizeof(vars(sealer))
            + getsizeof(sealer._keyed)
            + getsizeof(sealer._paired)
            + getsizeof(sealer._aeads)
            + sum(getsizeof(aead) for aead in sealer._aeads.values())
        )

    def verifier(self, credential: bytes) -> bytes:
        return hmac.digest(self._verifier, canonical_credential(credential), "sha256")

    def seal(self, key: bytes, address: BindingAddress, verifier: bytes, binding: Binding) -> bytes:
        if not exact_bytes(verifier, 32):
            raise LaunchUnavailable()
        aad = f"launch-binding-v1:{key.decode('ascii')}:{address.issuer}"
        blob = self._sealer.seal(packed((*address.fields(), verifier, *binding.fields())), aad)
        if len(blob) > BINDING_BYTES:
            raise LaunchUnavailable()
        return blob

    def open(self, key: bytes, address: BindingAddress, blob: bytes) -> tuple[bytes, Binding]:
        if type(blob) is not bytes or not 30 <= len(blob) <= BINDING_BYTES:
            raise LaunchUnavailable()
        aad = f"launch-binding-v1:{key.decode('ascii')}:{address.issuer}"
        try:
            values = _unpack(self._sealer.open(blob, aad), 13)
            if values[:7] != address.fields() or not exact_bytes(values[7], 32):
                raise LaunchUnavailable()
            folder, root, project, source, flavor = (part.decode("utf-8") for part in values[8:])
            if source not in ("server", "client") or flavor not in ("windows", "posix"):
                raise LaunchUnavailable()
            return values[7], Binding(folder, root, project, source, flavor)
        except (InvalidTag, ValueError):
            raise LaunchUnavailable() from None


_CREDENTIAL = re.compile(rb"latet_lk_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")


def canonical_credential(credential: bytes) -> bytes:
    if type(credential) is not bytes or _CREDENTIAL.fullmatch(credential) is None:
        raise LaunchUnavailable()
    return credential


class RemoteResult(Enum):
    NOT_SUBMITTED = "not-submitted"
    SET = "set"
    DELETE = "delete"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, repr=False)
class WireResult:
    remote: RemoteResult
    cleanup_pending: _BorrowedConnection | None = None


def _stock_connection(connection: _BorrowedConnection) -> TypeGuard[Connection]:
    return type(connection) in (Connection, SSLConnection)


def binding_once(bus: Bus, epoch: bytes, locator: bytes, sealed: bytes | None) -> WireResult:
    """SET one sealed binding or DEL one key. Never replay a mutation."""
    key = binding_key(bus, epoch, locator)
    if sealed is not None and (type(sealed) is not bytes or not 30 <= len(sealed) <= BINDING_BYTES):
        raise LaunchUnavailable()
    pool = bus.client.connection_pool
    if type(pool) is not BlockingConnectionPool or pool.connection_class not in (Connection, SSLConnection):
        return WireResult(RemoteResult.NOT_SUBMITTED)
    options = pool.connection_kwargs
    if (
        pool.cache is not None
        or options.get("redis_connect_func") is not None
        or options.get("command_packer") is not None
        or options.get("parser_class") not in (None, *_PARSERS)
    ):
        return WireResult(RemoteResult.NOT_SUBMITTED)
    args = (b"DEL", key) if sealed is None else (b"SET", key, sealed, b"PX", 60000)
    connection = None
    remote = RemoteResult.NOT_SUBMITTED
    pending = None
    try:
        connection = pool.get_connection()
        if (
            _stock_connection(connection)
            and type(connection._parser) in _PARSERS
            and type(connection._command_packer) is PythonRespSerializer
            and not connection._connect_callbacks
        ):
            command = connection.pack_command(*args)
            connection.check_health()
            remote = RemoteResult.UNKNOWN
            connection.send_packed_command(command, check_health=False)
            reply = connection.read_response(disable_decoding=True)
            if sealed is not None and type(reply) is bytes and reply == b"OK":
                remote = RemoteResult.SET
            elif sealed is None and type(reply) is int and reply in (0, 1):
                remote = RemoteResult.DELETE
        else:
            remote = RemoteResult.UNKNOWN
    except Exception:  # noqa: S110
        # The phase, not the exception type or its words, determines whether settlement is known.
        pass
    finally:
        if connection is not None:
            try:
                if remote not in (RemoteResult.SET, RemoteResult.DELETE):
                    connection.disconnect()
                # The exact blocking pool releases both return shapes from its own get_connection.
                cast(Callable[[_BorrowedConnection], None], pool.release)(connection)
            except Exception:
                pending = connection
    return WireResult(remote, pending)
