"""A computer's connection to a Latet server: ``connection.yaml`` in the data folder (LATET_CONNECTION moves it), and
this computer's personal token and computer key, kept in one record with the connection they were issued for (the
server's address, the identity, the pinned certificate's fingerprint), in the system's keyring or in a private file.
While it is connected, every address Latet gives an agent is the identity's space on the server (``agent_base``).
Nothing here needs the `server` extra: a computer's install has none of it.

The token goes to the agents (Claude Code's plugin calls the space's bridge with it); the computer key never does: the
CLI alone reads it from the record, for the server's calls that ask for it (a sign-in link to the dashboard, the
person's identity sent: R16), and `latet disconnect` forgets it with the token. A record kept before computer keys has
none (``key_missing`` says what to do).

What is read back is checked: the connection file field by field (an address as ``server_url`` keeps it, the
identity's id, a fingerprint), so that a planted or hand-edited file names no http:// address off this computer, which
the agents would be given with their providers' keys, and no fingerprint that is a path; a token only from a record of
the connection it is asked for, never one issued by another server, which it would be sent to; the token's file only in
a folder that is the person's alone. A refusal names the file and the field, never what it holds.

With no connection file this computer is not connected, and nothing of a connection is admitted or read: local Latet
is not a connection (R1). Nothing of a connection is read from a folder others can change
(``connection_folder_problem``), and a writer (`latet connect`) admits its folders before it makes one: the
connection file decides where every agent and Latet's own calls go, and the data folder holds what the agents read with
no check of Latet's (the certificate files they are given, `latet claude`'s session settings with the token) and what
`latet agents revert` writes back. The certificate an agent is given is a copy Latet writes in the connection's own
folder from the pin it checked, never the store `latet llm trust` shares, and only a pin constrained to the server's
host: Claude Code and OpenCode (Bun) and Codex (rustls) take from a certificate they trust its key and its name
constraints, not its CA:FALSE (notes/spike-server-certificates.md).

This module imports nothing heavy (the certificate helpers are imported where they are used): every agent's start asks
it whether this computer is connected."""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import logging
import os
import re
import stat
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# The exception Typer's command line shows as an error, with its exit code: Typer 0.27.2 runs on a copy of Click of its
# own, and the Click package's exceptions would escape it as a traceback.
if TYPE_CHECKING:
    from click.exceptions import ClickException
else:
    try:
        from typer._click.exceptions import ClickException
    except ImportError as exc:
        if exc.name not in ("typer", "typer._click", "typer._click.exceptions"):
            raise
        from click.exceptions import ClickException

from latet.config import _yaml_error_at, data_dir, load_config, load_yaml, write_atomic
from latet.fileio import FileReadError, read_regular_bytes
from latet.integrations import base as records
from latet.logline import described
from latet.server.tokens import COMPUTER_PREFIX, KEY_PREFIX, TOKEN_PREFIX, route_key
from latet.storage.folders import make_private_folder, private_folder_problem, tighten_folder
from latet.storage.keys import _keyring_on

if TYPE_CHECKING:
    from latet.config import Config

log = logging.getLogger(__name__)

# The token's keyring entry has a service of its own: under `latet` (the root key's, the reviewer key's), keyring's
# Windows backend keeps one entry per service and saves the one a later entry displaces again, as the writer's
# persistence has it, which for Latet's other entries is the roaming profile's.
_SERVICE, _TOKEN, _TOKEN_FILE = "latet-server", "server-token", "server.token"
KEYRING = "the system's keyring"  # where a copy of the token is, as `remove_server_token` names it
USER_ID = re.compile(r"u_[a-z2-7]{10}")  # E0b's identity ids (identities.IDENTITY_ID, which needs the extra)
PERSONAL_TOKEN = re.compile(rf"{TOKEN_PREFIX}[A-Za-z0-9_-]{{43}}")  # E0b's tokens: the prefix and 32 random bytes
API_KEY = re.compile(rf"{KEY_PREFIX}[A-Za-z0-9_-]{{43}}")
COMPUTER_KEY = re.compile(rf"{COMPUTER_PREFIX}[A-Za-z0-9_-]{{43}}")  # E0c's computer keys (R16): the same alphabet
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
NAME_CHARS = 1_000  # characters of an identity's name kept at most
_RECORD_BYTES = 4096  # a record of the token and the computer key is some 460 bytes
_KEY_BYTES = 1024
_CONNECTION_BYTES = 64 * 1024
_AGAIN = "`latet disconnect`, then connect again"
_CONNECTION_DIR = "connection"  # the connection's own folder in the data folder
THEIRS_MOST = 1024 * 1024  # bytes of a person's own certificate file read at most
_PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.DOTALL)
_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "gemini": "Gemini CLI"}

# What `server_url` keeps: a host is a name of letters, digits and hyphens (IDNA 2008 A-labels, as httpx sends it),
# whose last label is no number (some resolvers read 2130706433 or 0x7f.1 as an IPv4 address), or an IP address.
_LABEL = re.compile(r"[a-z0-9-]{1,63}")
_NUMBER = re.compile(r"[0-9]+|0x[0-9a-f]*")
_PORT = re.compile(r"[0-9]{1,5}")
_DEFAULT_PORTS = {"https": 443, "http": 80}
_HERE = ("127.0.0.1", "[::1]")  # where http is taken: this computer, by its address (a name is a resolver's answer)
_HTTPS = "an https:// address, such as https://latet.acme.lan (http:// only for 127.0.0.1 or [::1], this computer)"
_ALONE = "an https:// address alone, such as https://latet.acme.lan: no path, user, query or fragment, no space"
_HOST = (
    "an https:// address whose host is a name of letters, digits and hyphens that does not end in a number, or an IP "
    "address"
)
_PORT_RANGE = "an https:// address whose port is a number from 1 to 65535"


def server_url(url: str) -> str:
    """A server's address as Latet keeps it, in the one form every client reads the same way: ``scheme://host[:port]``,
    https (http only for 127.0.0.1 and [::1], this computer, for tests), a host in lower case (a name as httpx sends it,
    IDNA 2008 A-labels, no final dot; an IPv4 address dotted; an IPv6 address compressed, in brackets, with no zone), no
    port when it is the scheme's, nothing else: no user (no ``@`` at all), no path but ``/``, no ``?`` or ``#`` (even
    empty), no space, control character or backslash. Raises ValueError otherwise, in words that repeat nothing of it.
    The spaces and line ends a paste adds around it are taken off (those alone: ``str.strip`` takes C1's NEL too)."""
    text = url.strip(" \t\r\n")
    if any(char in " \\@?#" or ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in text):
        raise ValueError(_ALONE)
    scheme, sep, rest = text.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS:
        raise ValueError(_HTTPS)
    authority, _, path = rest.partition("/")
    if path:
        raise ValueError(_ALONE)
    host, port = _authority(authority)
    if scheme == "http" and host not in _HERE:
        raise ValueError(_HTTPS)
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else "")


def _authority(authority: str) -> tuple[str, int | None]:
    """The host, as ``server_url`` keeps it, and the port of ``authority`` (``host[:port]``)."""
    if authority.startswith("["):
        inside, closed, after = authority[1:].partition("]")
        if not closed or "%" in inside:  # an IPv6 address's zone names an interface of one computer
            raise ValueError(_HOST)
        try:
            host = f"[{ipaddress.IPv6Address(inside).compressed}]"
        except ValueError:
            raise ValueError(_HOST) from None
        if after and not after.startswith(":"):
            raise ValueError(_HOST)
        return host, _port(after[1:]) if after else None
    name, colon, given = authority.rpartition(":") if ":" in authority else (authority, "", "")
    port = _port(given) if colon else None
    try:
        return str(ipaddress.IPv4Address(name)), port
    except ValueError:
        pass
    return _name(name), port


def _port(text: str) -> int:
    if not _PORT.fullmatch(text) or not 1 <= int(text) <= 65535:
        raise ValueError(_PORT_RANGE)
    return int(text)


def _name(name: str) -> str:
    """A host name as httpx sends it (A-labels), when it is one ``server_url`` keeps."""
    import httpx

    if ":" in name or "[" in name or "]" in name:  # httpx would read a port or an address in them, and drop it
        raise ValueError(_HOST)
    try:
        parsed = httpx.URL(f"https://{name}/")
        if not parsed.host:  # read back as IDNA 2008 has it: an A-label it cannot read (xn--zz) raises here
            raise ValueError(_HOST)
        host = parsed.raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, ValueError):  # httpx's own refusal, or IDNA's (a UnicodeError)
        raise ValueError(_HOST) from None
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels) or _NUMBER.fullmatch(labels[-1]):
        raise ValueError(_HOST)
    return host


def _kept_as_is(url: str) -> bool:
    """Whether ``url`` is an address as ``server_url`` keeps it."""
    try:
        return server_url(url) == url
    except ValueError:
        return False


def fingerprint_form(text: str) -> str:
    """A certificate's fingerprint as Latet writes it, ``sha256:`` and 64 lower-case hex digits, from that form or from
    ``AB:CD:…`` as `latet server certificate` and openssl print it. Raises ValueError otherwise."""
    hexa = text.strip().lower().removeprefix("sha256:").replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{64}", hexa):
        raise ValueError("sha256: and 64 hexadecimal digits, or AB:CD:... as `latet server certificate` prints it")
    return f"sha256:{hexa}"


class NotConnected(ClickException):
    """This computer's connection, or what a command needs to reach a server, is missing or cannot be used: said, with
    exit code 2, by any command that lets it out."""

    exit_code = 2


class Connection(BaseModel):
    """This computer's connection, as connection.yaml keeps it, each field checked when it is read."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(description="The server's address, as `server_url` keeps it.")
    user: str = Field(description="The identity's id on the server.")
    user_name: str = Field(max_length=NAME_CHARS, description="The identity's name, as the server gave it.")
    fingerprint: str | None = Field(
        default=None,
        description="The server's self-signed certificate, pinned with `latet connect --fingerprint`.",
    )

    @field_validator("url")
    @classmethod
    def _url(cls, url: str) -> str:
        if server_url(url) != url:
            raise ValueError("an address as Latet keeps it, such as https://latet.acme.lan")
        return url

    @field_validator("user")
    @classmethod
    def _user(cls, user: str) -> str:
        if not USER_ID.fullmatch(user):
            raise ValueError("an identity's id on a Latet server: u_ and ten letters or digits")
        return user

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint(cls, fingerprint: str | None) -> str | None:
        if fingerprint is not None and not FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("sha256: and 64 lower-case hexadecimal digits")
        return fingerprint


def _problems(exc: ValidationError) -> str:
    """What is wrong with a connection file, each by its field (another key as ``*``: a key may be anything, a token
    written there among them) and pydantic's words, which quote nothing (``hide_input_in_errors``)."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    said = []
    for error in errors[:10]:
        place = ".".join(str(part) if part in Connection.model_fields else "*" for part in error["loc"])
        said.append(f"{place or 'the file'}: {error['msg'].removeprefix('Value error, ')}")
    return "; ".join(said)


def connection_path() -> Path:
    """``connection.yaml`` in the data folder, which stays on this computer (config.yaml's folder roams with a Windows
    profile, and a connection is this computer's); LATET_CONNECTION moves it."""
    return Path(os.environ.get("LATET_CONNECTION") or (data_dir() / "connection.yaml"))


def connection_folder_problem() -> str | None:
    """Why the selected connection state cannot be used; inspect only, without creating a missing folder."""
    try:
        _admit_connection(_selected_connection())
    except NotConnected as exc:
        return exc.message
    return None


@dataclass(frozen=True)
class _SelectedConnection:
    data: Path
    path: Path


@dataclass(frozen=True)
class _PreparedConnection:
    selected: _SelectedConnection
    connection: Connection | None
    token: str | None = field(default=None, repr=False)
    key: str | None = field(default=None, repr=False)  # the computer key, read only for a call that asks for it


def _prepare_connection() -> _PreparedConnection:
    selected = _selected_connection()
    return _PreparedConnection(selected, _load_connection(selected))


def _prepare_token(prepared: _PreparedConnection) -> _PreparedConnection:
    if prepared.connection is None:
        return prepared
    token = load_server_token(prepared.selected.data, prepared.connection, _selected=prepared.selected)
    return replace(prepared, token=token)


def _prepare_key(prepared: _PreparedConnection) -> _PreparedConnection:
    """``prepared`` (its token read) with the computer key kept with that token, for the CLI's calls that ask for it:
    read only by them, never on an agent's start."""
    if prepared.connection is None or prepared.token is None:
        return prepared
    key = load_server_key(prepared.selected.data, prepared.connection, prepared.token, _selected=prepared.selected)
    return replace(prepared, key=key)


def _require_selection(prepared: _PreparedConnection) -> None:
    """Refuse before an older settings/record helper could select a different operation's state."""
    if _selected_connection() != prepared.selected:
        raise NotConnected(
            f"Latet's data folder changed while the command ran (LATET_DATA_DIR or LATET_CONNECTION, from "
            f"{prepared.selected.data}): run it again"
        )


def _selected_connection(data: Path | None = None) -> _SelectedConnection:
    """One operation's two selected roots, anchored without resolving links or collapsing parent traversal."""
    data = data if data is not None else data_dir()
    path = Path(os.environ.get("LATET_CONNECTION") or (data / "connection.yaml"))
    if not data.is_absolute() or not path.is_absolute():
        here = Path.cwd()
        data = data if data.is_absolute() else here / data
        path = path if path.is_absolute() else here / path
    return _SelectedConnection(data, path)


def _connection_unavailable(path: Path, exc: records.RecordsUnavailable | None = None) -> NotConnected:
    """A connection's state refused at ``path``: with the words of the check that failed (the folder or file, why, and
    the fix: ``records.RecordsUnavailable.said``), or the fixed ones when it has none."""
    if exc is not None and exc.said:
        return NotConnected(f"this computer's connection is not read from {path}: {exc.said}")
    return NotConnected(
        f"this computer's connection state at {path} is unsafe or unavailable: "
        "use a private data folder (LATET_DATA_DIR) and connection folder (LATET_CONNECTION), then connect again"
    )


def _admit_connection(selected: _SelectedConnection, *, create: bool = False) -> None:
    roots = tuple(dict.fromkeys((selected.data, selected.path.parent)))

    def inspect(folder: Path, *, create: bool = False) -> None:
        try:
            present = records._namespace(folder, create=create, connected=True)
        except records.RecordsUnavailable as exc:
            raise _connection_unavailable(folder, exc) from None
        if present and folder == selected.data:
            tighten_folder(folder)  # Latet's own data folder, as an older Latet made it (0755): never another's
        if present and (problem := private_folder_problem(folder)) is not None:
            raise NotConnected(f"this computer's connection is not read from {folder}: {problem}")

    # Inspect both existing chains before a writer creates anything beneath either root.
    for folder in roots:
        inspect(folder)
    if create:
        for folder in roots:
            inspect(folder, create=True)


def load_connection(*, unchecked: bool = False) -> Connection | None:
    """This computer's connection, or None. Raises NotConnected for a file that is not one, naming the file and the
    field, never what it holds; and for one kept in a folder others may change (``connection_folder_problem``), which
    every caller stops on. ``unchecked``: read past that check, for what forgets the connection (`latet disconnect`) or
    says why it cannot be used (`latet status`, `latet doctor`), never for what the connection is used for."""
    return _load_connection(_selected_connection(), unchecked=unchecked)


def connection_present(selected: _SelectedConnection | None = None) -> bool:
    """Whether a connection file is there (a link, or anything else, at its name counts): with none, this computer is
    not connected, and nothing of a connection is admitted or read (local Latet, as before a server existed). Only
    lstat is asked: nothing is opened, and nothing is created."""
    path = (selected or _selected_connection()).path
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # a name that cannot be inspected is not taken for none: the admission says why
    return True


def _load_connection(selected: _SelectedConnection, *, unchecked: bool = False) -> Connection | None:
    path = selected.path
    if not connection_present(selected):  # R1: local mode is not a connection; nothing to guard, nothing admitted
        return None
    try:
        if unchecked:  # Diagnostic/revocation escape hatch: it must not become an operational fallback.
            if not path.is_file():
                return None
            with path.open("rb") as file:
                raw = file.read(_CONNECTION_BYTES + 1)
        else:
            _admit_connection(selected)
            if records._metadata(path) is None:
                return None
            raw = records._checked_bytes(path, _CONNECTION_BYTES + 1, connected=True)
    except records.RecordsUnavailable as exc:
        raise _connection_unavailable(path, exc) from None
    except OSError as exc:
        raise NotConnected(f"{path} cannot be read ({exc.strerror or described(exc)}): {_AGAIN}") from None
    if len(raw) > _CONNECTION_BYTES:
        raise NotConnected(f"{path} is not a connection (it holds more than {_CONNECTION_BYTES} bytes): {_AGAIN}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise NotConnected(f"{path} is not UTF-8 text: {_AGAIN}") from None
    try:
        data = load_yaml(text)
    except yaml.YAMLError as exc:  # where, never what: PyYAML's words quote the line
        raise NotConnected(f"{path} is not valid YAML{_yaml_error_at(exc, text)}: {_AGAIN}") from None
    problems = ""
    try:
        return Connection.model_validate(data if data is not None else {})
    except ValidationError as exc:
        problems = _problems(exc)
    raise NotConnected(f"{path} does not hold a connection ({problems}): {_AGAIN}")  # raised once pydantic's is handled


def save_connection(connection: Connection) -> Path:
    """connection.yaml written, private: it holds the person's name."""
    return _save_connection(_selected_connection(), connection)


def _save_connection(selected: _SelectedConnection, connection: Connection) -> Path:
    _admit_connection(selected, create=True)
    path = selected.path
    text = yaml.safe_dump(connection.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
    write_atomic(path, text, private=True)
    return path


def forget_connection() -> None:
    connection_path().unlink(missing_ok=True)


# --- the token, kept with its connection ------------------------------------------------------------------------------


@dataclass(frozen=True)
class KeptToken:
    """A copy of this computer's token as it is kept: the connection it was issued for (its server's address, its
    identity, the fingerprint pinned for that server), the token, where the copy is (``KEYRING``, or the file), and the
    computer key kept with it (None in a record kept before computer keys)."""

    url: str
    user: str
    fingerprint: str | None
    token: str = field(repr=False)
    place: str
    key: str | None = field(default=None, repr=False)

    def is_for(self, connection: Connection) -> bool:
        return (self.url, self.user, self.fingerprint) == (connection.url, connection.user, connection.fingerprint)


def _record(connection: Connection, token: str, key: str | None = None) -> str:
    kept = {"url": connection.url, "user": connection.user, "fingerprint": connection.fingerprint, "token": token}
    if key is not None:
        kept["key"] = key
    return json.dumps(kept, separators=(",", ":"))


def _kept(text: str | None, place: str) -> KeptToken | None:
    """A record read back, when it is one: the address as ``server_url`` keeps it, an identity's id, a fingerprint or
    null, and a personal token's shape. Anything else (a bare token, another shape) is no record, and answered for no
    connection. Its computer key is read when it is one's shape; else the record has none (``key_missing``)."""
    if not text:
        return None
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(found, dict):
        return None
    url, user, fingerprint, token = (found.get(name) for name in ("url", "user", "fingerprint", "token"))
    if not (isinstance(url, str) and _kept_as_is(url) and isinstance(user, str) and USER_ID.fullmatch(user)):
        return None
    if fingerprint is not None and not (isinstance(fingerprint, str) and FINGERPRINT.fullmatch(fingerprint)):
        return None
    if not (isinstance(token, str) and PERSONAL_TOKEN.fullmatch(token)):
        return None
    key = found.get("key")
    if not (isinstance(key, str) and COMPUTER_KEY.fullmatch(key)):
        key = None
    return KeptToken(url, user, fingerprint, token, place, key)


def _keyring() -> Any:
    """The system's keyring, as this computer's token is kept there. On Windows, an entry of this computer alone:
    keyring keeps one for the person's roaming profile by default (CRED_PERSIST_ENTERPRISE), and another computer of
    theirs would come up connected with this one's token. A copy of the backend takes the setting, the one the rest of
    Latet writes through is left as it is. keyring is imported here, not with the module: it costs some 200 ms, which
    every command that gives its agents an address would pay, connected or not (as ``latet.storage.keys`` does)."""
    import keyring

    backend = keyring.get_keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        if isinstance(backend, WinVaultKeyring):
            local = copy.copy(backend)
            local.persist = "local machine"
            return local
    return backend


def _from_keyring() -> KeptToken | None:
    try:
        text = _keyring().get_password(_SERVICE, _TOKEN)
    except Exception as exc:  # no backend, a locked keychain, a session without D-Bus...
        log.info("keyring unavailable: %s", described(exc))
        return None
    return _kept(text, KEYRING)


def _gone_from_keyring() -> bool:
    """The keyring's copy deleted, if any: whether none is left there (True too where there is no keyring at all)."""
    from keyring.backends import fail
    from keyring.errors import PasswordDeleteError

    try:
        backend = _keyring()
    except Exception as exc:
        log.info("keyring unavailable: %s", described(exc))
        return True
    try:
        backend.delete_password(_SERVICE, _TOKEN)
    except PasswordDeleteError:
        pass  # none there
    except Exception as exc:
        log.info("keyring unavailable: %s", described(exc))
    try:
        return backend.get_password(_SERVICE, _TOKEN) is None
    except Exception as exc:
        log.info("keyring unavailable: %s", described(exc))
        return isinstance(backend, fail.Keyring)  # no keyring on this system: nothing was ever kept in one


def _copies(data: Path) -> Iterator[KeptToken]:
    """The keyring's copy (unless LATET_KEYRING is off), then the file's, each read only when the one before was not
    the copy looked for."""
    if _keyring_on() and (kept := _from_keyring()) is not None:
        yield kept
    if (kept := _kept(_read_token_file(data), str(data / _TOKEN_FILE))) is not None:
        yield kept


def kept_tokens(data: Path) -> list[KeptToken]:
    """Every copy of this computer's token kept with its connection: the keyring's (unless LATET_KEYRING is off), then
    the file's in ``data``. A copy that is not a record is left out. Raises NotConnected when the file is in a folder
    that is not the person's alone, or is not theirs alone itself."""
    selected = _selected_connection(data)
    _admit_connection(selected)
    return list(_copies(selected.data))


def token_copies(data: Path) -> tuple[list[KeptToken], str | None]:
    """Every copy of this computer's token that can be read, each with the connection it was issued for, and why a copy
    could not be read, for what revokes or keeps the token whole (`latet disconnect`, `latet connect`'s check): the
    keyring's copy whatever LATET_KEYRING says (one saved while it was on is this computer's token still, and
    ``remove_server_token`` deletes it whatever it says), then the file's. A file that is not read (in a folder that is
    not the person's alone, or not theirs alone itself) is said in place of raised: the keyring's copy is revoked
    whatever the file is."""
    found = [kept] if (kept := _from_keyring()) is not None else []
    try:
        if (kept := _kept(_read_token_file(data), str(data / _TOKEN_FILE))) is not None:
            found.append(kept)
    except NotConnected as exc:
        return found, exc.message
    return found, None


def load_server_token(
    data: Path, connection: Connection, *, _selected: _SelectedConnection | None = None
) -> str | None:
    """This computer's token for ``connection``: from a record kept for that connection alone (its address, its
    identity, its pinned fingerprint), the keyring's first, then the file's. None when none is: a token another server
    issued is never answered, as it would then be sent to this one."""
    selected = _selected if _selected is not None else _selected_connection(data)
    if _selected is not None and selected.data != data:
        raise _connection_unavailable(selected.path)
    _admit_connection(selected)
    for kept in _copies(selected.data):
        if kept.is_for(connection):
            return kept.token
    return None


def load_server_key(
    data: Path, connection: Connection, token: str, *, _selected: _SelectedConnection | None = None
) -> str | None:
    """The computer key kept in one record with ``token`` for ``connection`` (``load_server_token``'s), for the CLI's
    calls that ask for it alone; None when that record holds none (``key_missing``)."""
    selected = _selected if _selected is not None else _selected_connection(data)
    if _selected is not None and selected.data != data:
        raise _connection_unavailable(selected.path)
    _admit_connection(selected)
    for kept in _copies(selected.data):
        if kept.is_for(connection) and kept.token == token:
            return kept.key
    return None


def save_server_token(
    data: Path, connection: Connection, token: str, key: str | None = None
) -> Literal["keyring", "file"]:
    """``token``, and the computer ``key`` issued with it, kept in one record with ``connection``: in the keyring when
    it gives the record back (keyring's null backend, for one, takes a record and keeps nothing), else in
    ``<data>/server.token``, private, the keyring's entry then deleted. Raises NotConnected when ``data`` is not the
    person's alone, and ValueError for a text that is not a personal token, or a key that is not a computer key."""
    return _save_server_token(_selected_connection(data), connection, token, key)


def _save_server_token(
    selected: _SelectedConnection, connection: Connection, token: str, key: str | None = None
) -> Literal["keyring", "file"]:
    if not PERSONAL_TOKEN.fullmatch(token):
        raise ValueError("not a personal token of a Latet server")
    if key is not None and not COMPUTER_KEY.fullmatch(key):
        raise ValueError("not a computer key of a Latet server")
    _admit_connection(selected)
    data = selected.data
    record = _record(connection, token, key)
    if _keyring_on():
        _gone_from_keyring()  # first: keyring's Windows backend would save the record it replaces again, aside
        try:
            backend = _keyring()
            backend.set_password(_SERVICE, _TOKEN, record)
            back = backend.get_password(_SERVICE, _TOKEN)
        except Exception as exc:
            log.info("keyring unavailable: %s", described(exc))
        else:
            if back == record:
                for path in _token_files(data):  # an older copy would be read if the keyring ever failed
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                return "keyring"
            log.info("the keyring did not give this computer's token back: it is kept in a file")
        _gone_from_keyring()
    _admit_connection(selected, create=True)
    write_atomic(data / _TOKEN_FILE, record, private=True)
    return "file"


def _file_problem(data: Path) -> str | None:
    """Why ``data`` cannot hold this computer's token's file, in words that name the folder and the fix; None when it
    can. Made the person's alone when it is made here, whatever the umask (002 makes a folder group-writable), and on
    Windows with the access list Python gives a folder made so (SYSTEM, the Administrators, its owner)."""
    try:
        make_private_folder(data)
    except OSError as exc:
        from latet.server.client import printable

        return (
            f"this computer's token cannot be kept in a file: folder {printable(str(data))} "
            f"is unavailable ({described(exc)})"
        )
    if (problem := private_folder_problem(data)) is None:
        return None
    return (
        f"this computer's token cannot be kept in a file: {problem}; or let the system's keyring keep it "
        "(LATET_KEYRING not off)"
    )


def token_file_problem(data: Path) -> str | None:
    """Why this computer's token could not be kept, as far as it is known before a one-time code is spent on it: only
    the file can keep it (LATET_KEYRING off, or a keyring that keeps nothing: keyring's fail backend, where there is
    none, and its null backend) and ``data`` cannot hold it. None otherwise: a keyring that fails when the token comes
    leads to the file then, checked then."""
    if _keyring_on() and not _keeps_nothing():
        return None
    return _file_problem(data)


def _keeps_nothing() -> bool:
    from keyring.backends import fail, null

    try:
        return isinstance(_keyring(), fail.Keyring | null.Keyring)
    except Exception as exc:  # no backend that can be asked: none keeps the token
        log.info("keyring unavailable: %s", described(exc))
        return True


def remove_server_token(data: Path) -> list[str]:
    """Every copy of this computer's token forgotten, with the computer key in its record: the keyring's (also with
    LATET_KEYRING off: a copy saved while it was on would stay otherwise), the file, and what a write cut short left
    beside it. The places where a copy remains: none, or a keyring that could not be asked, a file that could not be
    removed."""
    remains = [] if _gone_from_keyring() else [KEYRING]
    for path in _token_files(data):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.info("a copy of this computer's token could not be removed: %s", described(exc))
            remains.append(str(path))
    return remains


def _token_files(data: Path) -> list[Path]:
    """The token's file and the temporary files a write cut short left beside it (``write_atomic``'s)."""
    try:
        left = sorted(data.glob(f".{_TOKEN_FILE}.*.tmp"))
    except OSError:
        left = []
    return [data / _TOKEN_FILE, *left]


def keep_connection(
    connection: Connection, token: str, key: str | None = None, *, cleanup: bool = True
) -> Literal["keyring", "file"]:
    """``connection``, its token and its computer key kept: the token's record first, then the connection file, the
    record removed again when the connection file cannot be written, so that nothing is kept of a connection half made.
    Where the record is."""
    selected = _selected_connection()
    _admit_connection(selected)  # Rejection precedes the cleanup region: no credential was changed yet.
    try:
        where = _save_server_token(selected, connection, token, key)
        _save_connection(selected, connection)
    except BaseException:
        if cleanup:
            try:
                remove_server_token(selected.data)
            except BaseException as exc:
                log.info("a failed connection's token cleanup could not finish: %s", described(exc))
        raise
    return where


def _read_token_file(data: Path) -> str | None:
    """The token's file, read only from a folder that is the person's alone (and on POSIX only a file that is theirs
    alone): its text, or None when there is none, or it is too long or not UTF-8 to be a record."""
    path = data / _TOKEN_FILE
    if not os.path.lexists(path):
        return None
    if (problem := private_folder_problem(data)) is not None:
        raise NotConnected(f"this computer's token is not read from {path}: {problem}")
    try:
        raw = _read_private(path, _RECORD_BYTES + 1)
    except OSError as exc:
        raise NotConnected(f"{path} cannot be read ({exc.strerror or described(exc)}): {_AGAIN}") from None
    if len(raw) > _RECORD_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_private(path: Path, most: int) -> bytes:
    """At most ``most`` bytes of ``path``. On POSIX only a regular file (a link is not followed, a pipe does not hold
    the read), owned by the person, with no permission for group or others; raises NotConnected otherwise."""
    if sys.platform == "win32":
        try:
            return records._checked_bytes(path, most, connected=True)
        except records.RecordsUnavailable as exc:
            raise _connection_unavailable(path, exc) from None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as file:
        found = os.fstat(file.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise NotConnected(f"{path} is not a plain file: remove it, then connect again")
        if found.st_uid != os.getuid():
            raise NotConnected(f"{path} belongs to another user of this computer: remove it, then connect again")
        if found.st_mode & 0o077:
            raise NotConnected(f"other users of this computer can read {path}: `chmod 600 {path}`")
        return file.read(most)


# --- what a command reaches ---------------------------------------------------------------------------------------


def _server_pem(data: Path, fingerprint: str, *, strict: bool = False) -> str | None:
    """A desktop pin's checked bytes, from its actual private child; never reopen it after validation."""
    from latet.engine.reviewer import certificate as certificates

    if not FINGERPRINT.fullmatch(fingerprint):
        return None
    folder = data / "certificates"
    path = folder / f"{fingerprint.removeprefix('sha256:')}.pem"
    try:
        if not records._namespace(folder, connected=True):
            return None
        tighten_folder(folder)  # Latet's own pin store, as an older Latet made it (0755): R2
        records._trusted_folder(folder, connected=True)
        if records._metadata(path) is None:
            return None
        raw = records._checked_bytes(path, THEIRS_MOST + 1, connected=True)
        if len(raw) > THEIRS_MOST:
            return None
        pem = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")  # Match the former read_text semantics.
        return pem if certificates.fingerprint_of(pem) == fingerprint else None
    except records.RecordsUnavailable as exc:
        if strict:
            raise _connection_unavailable(folder, exc) from None
        return None
    except (OSError, ValueError):
        return None


def _prepare_output(folder: Path, refusal: str, *, create: bool = False) -> None:
    """Inspect before input reads; only a later writer creates and revalidates the private output."""
    try:
        present = records._namespace(folder, create=create, connected=True)
    except records.RecordsUnavailable as exc:
        raise NotConnected(f"{refusal} in {folder}: {exc.said or 'use a private folder (LATET_DATA_DIR)'}") from None
    if present and (problem := private_folder_problem(folder)) is not None:
        raise NotConnected(f"{refusal} in {folder}: {problem}")


def server_certificate_file(connection: Connection) -> Path | None:
    """The server's certificate this computer pinned, in the store it was pinned in: only while it is there as it was
    pinned (its content hashed against the fingerprint) and is a server's own, never an authority. An agent is given a
    copy of it (``agent_certificate``), never this file."""
    from latet.engine.reviewer import certificate as certificates

    if connection.fingerprint is None:
        return None
    data = data_dir()
    pem = _server_pem(data, connection.fingerprint)
    if pem is None or certificates.is_authority(pem):
        return None
    return data / "certificates" / f"{connection.fingerprint.removeprefix('sha256:')}.pem"


def connection_dir() -> Path:
    """The connection's own folder in the data folder: the certificate files Latet gives the agents (never the store
    that `latet llm trust` shares), and what `latet disconnect` keeps while it runs. Removed with the connection."""
    return data_dir() / _CONNECTION_DIR


def pin_for_agents(connection: Connection) -> tuple[str | None, str | None]:
    """The certificate this computer pinned for its server, when an agent may be given it, and why not when not: None
    and None when nothing is pinned (a certificate of the company's authority, or a public one); ``missing`` when the
    pin is not there as it was pinned, or is an authority; ``unconstrained`` when it does not carry a critical name
    constraint that permits the server's host alone (``latet server certificate --make``'s): every agent takes from a
    certificate it trusts its key and its name constraints, not its CA:FALSE, so that without the constraint it would
    trust whatever the server's key signs, for any host (T3.8, and the spike's run for Task 5)."""
    return _pin_for_agents(data_dir(), connection)


def _pin_for_agents(data: Path, connection: Connection) -> tuple[str | None, str | None]:
    from latet.engine.reviewer import certificate as certificates

    if connection.fingerprint is None:
        return None, None
    pem = _server_pem(data, connection.fingerprint)
    if pem is None or certificates.is_authority(pem):
        return None, "missing"
    from urllib.parse import urlsplit

    if not constrained_to(pem, urlsplit(connection.url).hostname or ""):
        return None, "unconstrained"
    return pem, None


def constrained_to(pem: str, host: str) -> bool:
    """Whether ``pem`` carries a critical name constraint whose permitted names are ``host`` alone, in
    ``make_certificate``'s exact form: for a name, that name with ``0.0.0.0/32`` and ``::/128`` as the only addresses;
    for an address, that address with ``invalid`` as the only name. Anything else is not: another name, an address
    range, a kind of name not read here, and a kind of name with no permitted subtree at all, which RFC 5280 (as
    rustls and BoringSSL apply it) leaves unconstrained."""
    from cryptography import x509

    try:
        found = x509.load_pem_x509_certificate(pem.encode()).extensions.get_extension_for_class(x509.NameConstraints)
    except Exception:  # no constraint, or a certificate that cannot be read: not constrained, as far as an agent goes
        return False
    permitted = found.value.permitted_subtrees or []
    if not found.critical or any(not isinstance(name, x509.DNSName | x509.IPAddress) for name in permitted):
        return False
    names = {name.value.lower() for name in permitted if isinstance(name, x509.DNSName)}
    networks = {name.value for name in permitted if isinstance(name, x509.IPAddress)}
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return names == {host.lower()} and networks == {
            ipaddress.ip_network("0.0.0.0/32"),
            ipaddress.ip_network("::/128"),
        }
    return networks == {ipaddress.ip_network(f"{address}/{address.max_prefixlen}")} and names == {"invalid"}


def agent_certificate(
    connection: Connection, agent: str, theirs: Iterable[str | None]
) -> tuple[Path | None, list[str]]:
    """The file ``agent`` is given for the server's certificate (``pin_for_agents``), written again now, exclusively,
    in the connection's own folder, one per agent: the person's own certificates first (``theirs``: the values of their
    own variable, as their shell and their agent's settings hold them), then the pin; and what was said of the person's
    files. None when there is no pin to give an agent. A person's certificate is never dropped: a company's inspection
    proxy goes on being trusted beside the server."""
    return _agent_certificate(data_dir(), connection, agent, theirs)


def _agent_certificate(
    data: Path, connection: Connection, agent: str, theirs: Iterable[str | None]
) -> tuple[Path | None, list[str]]:
    folder = data / _CONNECTION_DIR
    refusal = f"the certificate {_NAMES.get(agent, agent)} reads is not written"
    _prepare_output(folder, refusal)
    pem, _ = _pin_for_agents(data, connection)
    if pem is None:
        return None, []
    said: list[str] = []
    mine: dict[str, str] = {}
    for value in theirs:
        for found in _their_certificates(data, value, connection, agent, said):
            mine.setdefault(found, found)
    _prepare_output(folder, refusal, create=True)
    path = folder / f"{agent}.pem"
    write_atomic(path, "".join(mine) + (pem if pem.endswith("\n") else pem + "\n"), private=True)
    return path, said


def is_latets_own(value: str) -> bool:
    """Whether a file named by an agent's certificate variable is one Latet wrote for a connection: never read as the
    person's own, so that no bundle holds an older one."""
    try:
        place = os.path.normcase(os.path.abspath(value))
    except (OSError, ValueError):
        return False
    folder = os.path.normcase(os.path.abspath(connection_dir()))
    return place == folder or place.startswith(folder.rstrip("\\/") + os.sep)


def _their_certificates(
    data: Path, value: str | None, connection: Connection, agent: str, said: list[str]
) -> list[str]:
    """The certificates of the person's own file ``value``, as PEM: read only when it is a plain file of
    ``THEIRS_MOST`` bytes at most, each certificate parsed, anything else in it left out and said; never a file Latet
    wrote for a connection, nor a certificate Latet pinned (this server's, or an earlier one's)."""
    if not value or not value.strip() or is_latets_own(value):
        return []
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from latet.engine.reviewer import certificate as certificates

    name = _NAMES.get(agent, agent)
    try:
        raw = _read_theirs(Path(value))
    except _NotTheirs as exc:
        said.append(f"{value} is not given to {name}: {exc}")
        return []
    found: list[str] = []
    read, other = 0, False
    for block in _PEM_BLOCK.finditer(raw):
        try:
            if block.group(1) != b"CERTIFICATE":
                raise ValueError("not a certificate")
            pem = x509.load_pem_x509_certificate(block.group()).public_bytes(serialization.Encoding.PEM).decode()
        except ValueError:
            other = True
            continue
        read += 1
        fingerprint = certificates.fingerprint_of(pem)
        kept = _server_pem(data, fingerprint, strict=True)
        if fingerprint == connection.fingerprint or (kept is not None and not certificates.is_authority(kept)):
            continue  # a pin of Latet's: this server's is added after it, an earlier one's never kept
        found.append(pem)
    rest = _PEM_BLOCK.sub(b"", raw)
    other = other or any(line.strip() and not line.strip().startswith(b"#") for line in rest.splitlines())
    if not read:
        said.append(f"{value} is not given to {name}: it holds no certificate in PEM form")
    elif other:
        said.append(f"{value}: what in it is not a certificate is not given to {name}")
    return found


class _NotTheirs(Exception):
    """Why a person's certificate file is not read."""


def _read_theirs(path: Path) -> bytes:
    """A person's certificate file, read only when it is a plain file of ``THEIRS_MOST`` bytes at most: a pipe, a
    device or a large file is never read (a pipe would hold the read; nonblocking, it is opened and refused)."""
    try:
        return read_regular_bytes(path, limit=THEIRS_MOST, follow=True)
    except FileReadError as exc:
        why = {"nonregular": "it is not a plain file", "too_large": "it is larger than 1 MiB"}
        raise _NotTheirs(why.get(exc.reason, "it cannot be read")) from None


def agent_base(config: Config | None = None) -> str:
    """The address Latet gives agents: its own on this computer, or the identity's space on the server this computer
    is connected to (``<url>/u/<route key>``), made of the connection's address and the token alone."""
    return _agent_base(_prepare_token(_prepare_connection()), config)


def _connect_again(connection: Connection) -> str:
    """What makes a connection again, a new code from an administrator in hand (R7)."""
    again = f"latet connect {connection.url}"
    if connection.fingerprint:
        again += f" --fingerprint {connection.fingerprint}"
    return f"`latet disconnect`, then `{again}` with a new code from an administrator"


def token_refused(connection: Connection) -> str:
    """What `latet status`, `latet doctor`, Latet's own calls and an agent's start say when the server refuses this
    computer's token (401): why it may, and what to do (R7)."""
    return (
        "the server no longer accepts this computer's token (revoked, or your identity disabled): "
        f"{_connect_again(connection)}"
    )


def token_missing(connection: Connection) -> str:
    """The same advice as ``token_refused``, for a token this computer no longer keeps."""
    return f"this computer is connected to {connection.url}, but its token is missing: {_connect_again(connection)}"


def key_missing(connection: Connection) -> str:
    """What `latet status`, `latet doctor`, `latet dashboard` and `latet identity send` say of a connection whose record
    holds no computer key (made before computer keys, R16): the agents work, but the dashboard and the identity need
    it, and connecting again gives it."""
    return (
        f"this computer's connection to {connection.url} has no computer key, which your dashboard and your identity "
        f"need (it was made by an earlier Latet): {_connect_again(connection)}"
    )


def _agent_base(prepared: _PreparedConnection, config: Config | None = None) -> str:
    connection, token = prepared.connection, prepared.token
    if connection is None:
        return f"http://127.0.0.1:{(config or load_config()).port}"
    if token is None:
        raise NotConnected(token_missing(connection))
    return f"{connection.url}/u/{route_key(token)}"


def server_target(url: str | None = None, fingerprint: str | None = None) -> tuple[str, str | None]:
    """The server a command asks, and the fingerprint of its certificate when one is pinned: ``url``, else
    LATET_SERVER_URL, else this computer's connection; ``fingerprint``, else LATET_SERVER_FINGERPRINT, else the
    connection's when it is that server. Raises NotConnected when nothing names a server, or what names it is wrong:
    naming where it came from, never repeating it (an address may hold a password)."""
    connection = load_connection()
    if url:
        chosen, source = url, "--server"
    elif os.environ.get("LATET_SERVER_URL"):
        chosen, source = os.environ["LATET_SERVER_URL"], "LATET_SERVER_URL"
    elif connection is not None:
        chosen, source = connection.url, "this computer's connection"
    else:
        raise NotConnected("which server? --server https://latet.acme.lan, or LATET_SERVER_URL")
    try:
        address = server_url(chosen)
    except ValueError as exc:
        raise NotConnected(f"{source}: {exc}") from None
    given, named = fingerprint, "--fingerprint"
    if not given:
        given, named = os.environ.get("LATET_SERVER_FINGERPRINT"), "LATET_SERVER_FINGERPRINT"
    try:
        pinned = fingerprint_form(given) if given else None
    except ValueError as exc:
        raise NotConnected(f"{named}: {exc}") from None
    if pinned is None and connection is not None and connection.url == address:
        pinned = connection.fingerprint
    return address, pinned


def read_key(path: Path) -> str:
    """An API key from the file an administrator kept it in (`latet server keys add`): the key alone on its line, read
    no further than a key's length. Raises NotConnected, naming the file as it was given, never what it holds."""
    try:
        raw = read_regular_bytes(path, limit=_KEY_BYTES, follow=True)
    except FileReadError:
        raise NotConnected(f"{path} holds no API key that can be read as a bounded regular file") from None
    try:
        key = raw.decode("utf-8-sig").strip() if len(raw) <= _KEY_BYTES else ""
    except UnicodeDecodeError:
        key = ""
    if not API_KEY.fullmatch(key):
        raise NotConnected(
            f"{path} holds no API key ({KEY_PREFIX} and 43 letters, digits, - or _, alone on its line): `latet server "
            "keys add` prints one"
        )
    return key
