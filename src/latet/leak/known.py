"""Whether Latet's own vault and history on this computer know a secret, for ``latet leak``: read-only, and never a key
made.

The vault (``latet.db``) keys a value's entry by a keyed hash of its type and of the value as the masked text wrote it,
escapes and all (``Vault.entity_id``: ``Masker._apply`` keeps ``text[span]``, and what it reads only draws its fake),
beside the time the entry was kept (``created``, in clear). So a value is looked up by the ids it would have, made with
the root key that ``load_root_key`` finds (``keys.find_root_key``, which makes none): under each type the vault holds,
since the type of a value's first sighting is its type for good, whatever finds it later; as the session writes it; and
as its escapes read (a JSON string's escaped slash or letter, a URL's `%40`, each the character it stands for), since
the vault finds a value it knows in a text's escaped reading too (``escapes.read_escaped``). The earliest of the entries
found is when Latet began to mask it. No value is opened: the ids and the times alone are read, and the vault's first
sealed value is tried once, to see that the key found is the vault's.

The history (``exchanges.db``) keeps each request's texts once, sealed (``blocks``), with the entry of each value masked
in them (their ``replacements``' ``entity``); and, sealed too (``refs``), each request's blocks and, in the full
history, its conversation's own key: ``cc:`` and Claude Code's ``x-claude-code-session-id``; ``session-id:``,
``session_id:``, ``thread-id:`` or ``x-session-id:`` and the session id of Codex or Hermes Agent; ``pck:`` and Codex's
prompt cache key, its thread. Those name the agent's session as its saved files do, and each request's ``ts`` (when it
reached Latet) the first time the history shows a value masked in that session. The key of a conversation whose
agent sends none of those is a digest of its opening (``conv:``), which names no session; a metadata history keeps no
blocks; an older Latet's row keeps its key in clear, read as it is. The row's own ``session`` column holds the key only
as a handle keyed with the root key, which names no session by itself. The earliest date Latet has on this computer
is the older of its vault's oldest entry and its history's oldest request (``LocalKnown.latet_since``).

Both databases are read from a private copy, deleted after (``sources.connect_read_only``): nothing is written beside
them. On a computer connected to a Latet server the vault and the history are the server's: there is no answer here.
Where there is none, ``answer`` says why (``NoAnswer``), for the command to say it; on a computer with no vault and no
server, Latet masked nothing, and its answers say so (``NothingHere``)."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard
from cryptography.exceptions import InvalidTag

from latet.config import data_dir
from latet.engine.escapes import uncached
from latet.engine.vault import Vault
from latet.leak.scan import SINCE_SLACK, Known
from latet.leak.sources import connect_read_only
from latet.storage.db import opens_with, sealer
from latet.storage.exchanges import history_opened, history_sealer, history_written, read_refs, ref_parts
from latet.storage.keys import find_root_key

log = logging.getLogger(__name__)

# The conversation keys that name an agent's own session id (``mediate._session_key``, ``ws``, ``bridge.api``), by the
# prefix the id follows. Any other (``conv:``, a digest of the opening; ``unscoped``) names none.
_SESSION_PREFIXES = ("cc:", "session-id:", "session_id:", "thread-id:", "x-session-id:", "pck:")
_NO_SESSION = "cc:unknown"  # the plugin bridge's, for a call that named no session
# What a sealed record gives that names nothing: another key's, a damaged one, one of a later Latet's in a form unknown
# here (a dict where a list was): never a stop of the scan.
_UNOPENED = (InvalidTag, zstandard.ZstdError, ValueError, TypeError, IndexError, KeyError)


def _agent_session(key: str) -> str | None:
    """The agent's own session id a conversation's key names (``cc:<id>``, ``session-id:<id>``...), or None."""
    if key == _NO_SESSION:
        return None
    for prefix in _SESSION_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix) :] or None
    return None


def _entities(replacements: Any) -> frozenset[str]:
    """The vault entries ``replacements`` (a block's, as the recorder keeps them) name."""
    if not isinstance(replacements, list):
        return frozenset()
    return frozenset(r["entity"] for r in replacements if isinstance(r, dict) and isinstance(r.get("entity"), str))


def _conversation(session: Any, summary: Any, sensitive: dict[str, Any] | None) -> str | None:
    """A request's conversation key: the full history's sealed one, or an older Latet's, which kept it in clear in the
    row's ``session`` (the row's summary written before history groups)."""
    if sensitive is not None and isinstance(sensitive.get("session"), str):
        return sensitive["session"]
    try:
        written = history_written(json.loads(summary))
    except (TypeError, ValueError):
        return None
    return session if not written and isinstance(session, str) else None


def _when(at: float) -> datetime:
    return datetime.fromtimestamp(at, UTC)


def _moment(raw: Any) -> float | None:
    """A time a store keeps (seconds since 1970), or None where it is none a date can be made of (a damaged row)."""
    try:
        at = float(raw)
        _when(at)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return at


def _history_sessions(path: Path, root_key: bytes) -> tuple[dict[str, dict[str, float]], float | None]:
    """Each vault entry the history names in a request's blocks, with the agents' session ids of those requests and the
    time of the first of them in each (its ``ts``, when it reached Latet); and the time of the oldest request it keeps,
    whatever it names."""
    aead = history_sealer(root_key)
    found: dict[str, dict[str, float]] = {}
    oldest: float | None = None
    with connect_read_only(path) as db:
        named: dict[str, frozenset[str]] = {}  # each block's entries, for the blocks that name any
        for block_id, data in db.execute("SELECT id, data FROM blocks"):
            try:
                kept = _entities(history_opened(aead, bytes(data), str(block_id))[1])
            except _UNOPENED:
                continue
            if kept:
                named[str(block_id)] = kept
        rows = db.execute("SELECT id, ts, session, summary, refs FROM exchanges")
        for exchange_id, ts, session, summary, refs in rows:
            at = _moment(ts)
            if at is None:
                continue
            oldest = at if oldest is None else min(oldest, at)
            if refs is None:  # counts and flags alone (a metadata history)
                continue
            try:
                blocks, sensitive = read_refs(history_opened(aead, bytes(refs), f"{exchange_id}:refs"))
                entities: set[str] = set()
                for ref in blocks:
                    block_id, _, _, _, _, source = ref_parts(ref)
                    entities |= named.get(block_id, frozenset())
                    if isinstance(source, list) and len(source) == 2:  # where its tool's output came from, masked
                        entities |= _entities(source[1])
            except _UNOPENED:
                continue
            key = _conversation(session, summary, sensitive) if entities else None
            agent = _agent_session(key) if key is not None else None
            if agent is None:
                continue
            for entity in entities:
                sessions = found.setdefault(entity, {})
                sessions[agent] = min(at, sessions.get(agent, at))
    return found, oldest


class LocalKnown:
    """This computer's vault, as the ids of its entries and when each was kept, and its history, read once it is first
    asked about: no value of either is held."""

    def __init__(self, root_key: bytes, created: dict[str, float], types: frozenset[str], history: Path | None) -> None:
        self._ids_of = Vault(root_key).entity_id  # a computer's vault: its first generation, as ``make_runtime``'s
        self._root_key = root_key
        self._created = created
        self._types = types
        self._history = history
        self._read: tuple[dict[str, dict[str, float]], float | None] | None = None

    def _ids(self, value: str) -> list[str]:
        """The ids of the vault's entries that hold ``value``: under any of its types, as written or as it reads."""
        forms = {value}
        reading = uncached(value)
        if reading is not None and reading.text:
            forms.add(reading.text)
        ids = {self._ids_of(etype, form) for etype in self._types for form in forms}
        return [entity_id for entity_id in ids if entity_id in self._created]

    def _history_read(self) -> tuple[dict[str, dict[str, float]], float | None]:
        """The history as ``_history_sessions`` reads it, once; nothing where there is none or it cannot be read."""
        if self._read is None:
            self._read = {}, None
            if self._history is not None:
                try:
                    self._read = _history_sessions(self._history, self._root_key)
                except (sqlite3.Error, OSError) as exc:
                    log.warning("Latet's history could not be read (%s): it confirms no session", type(exc).__name__)
        return self._read

    def since(self, etype: str, value: str) -> datetime | None:
        """When the vault first kept ``value``, under whatever type (``etype`` is the scan's, maybe not the vault's)."""
        kept = [self._created[entity_id] for entity_id in self._ids(value)]
        return _when(min(kept)) if kept else None

    def masked_in(self, etype: str, value: str) -> dict[str, datetime]:
        """The agents' session ids of the requests whose blocks the history shows ``value``'s entries masked in, each
        with the time of the first of them."""
        ids = self._ids(value)
        if not ids:
            return {}
        index = self._history_read()[0]
        first: dict[str, float] = {}
        for entity_id in ids:
            for session, at in index.get(entity_id, {}).items():
                first[session] = min(at, first.get(session, at))
        return {session: _when(at) for session, at in first.items()}

    def latet_since(self) -> datetime | None:
        """The earliest date Latet has on this computer: its vault's oldest entry or its history's oldest request,
        whichever is older; None where it has neither."""
        times = [at for at in (min(self._created.values(), default=None), self._history_read()[1]) if at is not None]
        return _when(min(times)) if times else None

    def history_horizon(self) -> datetime | None:
        """The history's oldest request still kept, how far back it sees (it keeps 14 days by default); None where
        there is no history, or it cannot be read."""
        oldest = self._history_read()[1]
        return _when(oldest) if oldest is not None else None


def _vault(path: Path, root_key: bytes) -> tuple[dict[str, float], frozenset[str]] | None:
    """The vault's entry ids with when each was kept, and its types; None where ``root_key`` does not open it."""
    with connect_read_only(path) as db:
        first = db.execute("SELECT original FROM entities LIMIT 1").fetchone()
        if first is not None and not opens_with(sealer(root_key), bytes(first[0])):
            return None
        rows = db.execute("SELECT id, created FROM entities")
        created = {str(entity_id): at for entity_id, raw in rows if (at := _moment(raw)) is not None}
        types = frozenset(str(etype) for (etype,) in db.execute("SELECT DISTINCT etype FROM entities"))
    return created, types


# Why Latet's vault and history on this computer give no answer (``NoAnswer.why``).
CONNECTED = "connected"  # to a Latet server, whose vault and history are not here
NO_VAULT = "no vault"  # and no server: Latet masked nothing here
NO_KEY = "no key"  # found here that opens the vault (none, or another install's)
UNREADABLE_KEY = "unreadable key"
UNREADABLE_VAULT = "unreadable vault"  # it could not be read or copied


@dataclass(frozen=True, slots=True)
class NoAnswer:
    """Why there is no answer here, and, where a read failed, what stopped it (an OS error's or SQLite's own words,
    never a piece of what was read)."""

    why: str
    detail: str = ""


class NothingHere:
    """The answers of a computer with no Latet vault and no Latet server: Latet masked nothing here. The vault knows no
    value, the history names no session, and Latet's date is now, past ``SINCE_SLACK``: later than every session read,
    so that a secret of a type the settings redact is in clear in each, as every other secret is."""

    def since(self, etype: str, value: str) -> datetime | None:
        return None

    def masked_in(self, etype: str, value: str) -> dict[str, datetime]:
        return {}

    def latet_since(self) -> datetime | None:
        return datetime.now(UTC) + SINCE_SLACK

    def history_horizon(self) -> datetime | None:
        return None


def _reason(exc: Exception) -> str:
    """What stopped a read, never a piece of what was read: an OS error's or SQLite's own words, else the error's
    class."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    if isinstance(exc, sqlite3.Error) and str(exc):
        return str(exc)
    return type(exc).__name__


def answer() -> tuple[Known | None, NoAnswer | None]:
    """What this computer's vault and history know, with no ``NoAnswer``; or, where they cannot tell, why: a computer
    connected to a Latet server (its vault is the server's), no root key that opens the vault (none is made), a key or
    a vault that could not be read. A computer with no vault and no server has the answers of ``NothingHere``, with
    ``NO_VAULT`` as its reason: Latet masked nothing there."""
    from latet import wrappers
    from latet.server.connection import NotConnected

    try:
        if wrappers.connected() is not None:
            return None, NoAnswer(CONNECTED)
    except NotConnected:  # a connection that cannot be used is no less a connection
        return None, NoAnswer(CONNECTED)
    data = data_dir()
    vault = data / "latet.db"
    if not vault.is_file():
        return NothingHere(), NoAnswer(NO_VAULT)
    try:
        root_key = find_root_key(data)
    except (OSError, ValueError) as exc:  # a key file that cannot be read, a key that is not base64
        return None, NoAnswer(UNREADABLE_KEY, _reason(exc) if isinstance(exc, OSError) else "it is not in base64")
    if not root_key:
        return None, NoAnswer(NO_KEY)
    try:
        read = _vault(vault, root_key)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return None, NoAnswer(UNREADABLE_VAULT, _reason(exc))
    if read is None:
        return None, NoAnswer(NO_KEY)
    history = data / "exchanges.db"
    return LocalKnown(root_key, *read, history if history.is_file() else None), None


def local_known() -> Known | None:
    """What this computer's vault and history know, or None where they cannot tell (``answer``'s reasons, a computer
    with no vault among them)."""
    found, unknown = answer()
    return found if unknown is None else None
