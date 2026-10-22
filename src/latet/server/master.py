"""A Latet server's master key: it seals each identity's root key and every secret kept in PostgreSQL, and the keys of
the bus and the sign-ins are derived from it. It comes from LATET_MASTER_KEY (base64 of 32 bytes) or the file of
``master_key_file`` (LATET_MASTER_KEY_FILE; one key per line, oldest first, the last one current: a value sealed names
the version that sealed it, so the key can be rotated later), never from both: an instance refuses to start without
it. Losing it makes every vault unreadable (the fakes then change), as losing a computer's root.key does: it is kept
apart from the database's backups. The database keeps what the key seals: each identity's root key (`root_key`), and a
check value that stops a start with a key other than the one the database was first started with
(`check_master_key`)."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag

from latet.fileio import FileReadError, read_regular_bytes
from latet.server.sealing import DERIVE, VERSIONS, Sealer, derived, version_of

if TYPE_CHECKING:
    from latet.server.db import Database
    from latet.server.settings import ServerSettings

_OWN = b"master"  # the purpose of what the master key seals itself: no sealer of it is handed out for this one
_CHECK = b"the master key check"  # what the database's check value says: nothing, but that it opens
MASTER_FILE_BYTES = 64 * 1024


class MasterKeyError(RuntimeError):
    """No master key, or not one: the server says how to give it."""


class MasterKey:
    """The master key's versions, oldest first: the last seals, and a sealed value opens with the one it names."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys or any(len(key) != 32 for key in keys):
            raise MasterKeyError("the master key is 32 bytes, in base64 (`latet server init` makes one)")
        if len(keys) > VERSIONS:
            raise MasterKeyError("the master key has more than 255 versions: a sealed value names one in a byte")
        if len(set(keys)) != len(keys):
            raise MasterKeyError("the master key holds one key twice: each line of its file is a version of its own")
        self._keys = list(keys)
        self._sealer = Sealer(self._keys, _OWN)
        self.version = len(self._keys)

    @classmethod
    def load(cls, settings: ServerSettings) -> MasterKey:
        """The key of LATET_MASTER_KEY, or the keys of ``master_key_file``. Raises MasterKeyError, which holds its
        words and nothing else: none is raised while the error of a read or of a decoding is handled, since the first
        holds the file's name (the key itself, when it was given where its file's name is wanted) and the second the
        file's bytes."""
        variable = os.environ.get("LATET_MASTER_KEY", "").strip()
        path = settings.master_key_file
        if variable and path is not None:  # which of the two is the key is not guessed
            raise MasterKeyError(
                "the master key is given twice, by LATET_MASTER_KEY and by LATET_MASTER_KEY_FILE (master_key_file): "
                "give it once"
            )
        text, problem = variable, ""
        if path is not None:
            try:
                text = read_regular_bytes(path, limit=MASTER_FILE_BYTES, follow=True).decode("utf-8-sig")
            except FileReadError as exc:
                problem = f"master_key_file names a file that {exc}"
            except UnicodeDecodeError:  # the key's bytes as they are, say
                problem = "master_key_file names a file that cannot be read: it is not UTF-8 text"
        if problem:
            raise MasterKeyError(problem)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines and path is not None:
            raise MasterKeyError("the master key's file holds no key (LATET_MASTER_KEY_FILE)")
        if not lines:
            raise MasterKeyError("the server needs its master key: LATET_MASTER_KEY_FILE (or LATET_MASTER_KEY)")
        if variable and len(lines) > 1:
            raise MasterKeyError(
                "LATET_MASTER_KEY holds one key: its versions, one a line, live in the file of LATET_MASTER_KEY_FILE"
            )
        keys: list[bytes] = []
        try:
            keys = [base64.b64decode(line, validate=True) for line in lines]
        except (binascii.Error, ValueError):
            keys = []
        if not keys:
            raise MasterKeyError("the master key is 32 bytes, in base64")
        return cls(keys)

    @staticmethod
    def generate() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    def is_version(self, text: str) -> bool:
        """Whether ``text`` is one of the key's versions as base64 writes it, with or without its padding, in either
        alphabet: a secret of another setting given as this one (the metrics token, which travels in clear when it is
        scraped over http). Each version compared in constant time."""
        for alphabet in (b"+/", b"-_"):
            try:
                found = base64.b64decode(text + "=" * (-len(text) % 4), altchars=alphabet, validate=True)
            except (binascii.Error, ValueError):
                continue
            if any(hmac.compare_digest(found, key) for key in self._keys):
                return True
        return False

    def seal(self, data: bytes, aad: str) -> bytes:
        """``data`` sealed for its place, ``aad``, with the current key: sealed elsewhere, it does not open."""
        return self._sealer.seal(data, aad)

    def open(self, blob: bytes, aad: str) -> bytes:
        version = version_of(blob)
        if version is None:
            raise MasterKeyError("not a sealed value")
        if not 1 <= version <= self.version:
            raise MasterKeyError(f"sealed with master key version {version}, which this server does not have")
        return self._sealer.open(blob, aad)

    def derive(self, purpose: bytes) -> bytes:
        """A key for ``purpose`` (the bus's messages, say), from the current master key."""
        return derived(self._keys[-1], DERIVE, purpose)

    def sealer(self, purpose: bytes) -> Sealer:
        """A sealer for ``purpose`` (the sign-ins, say) with every version of the master key, the current one
        sealing."""
        if purpose == _OWN:
            raise ValueError("that purpose is the master key's own: what it seals itself, it alone opens")
        return Sealer(self._keys, purpose)


def root_key(db: Database, master: MasterKey, identity_id: str, *, clock: Callable[[], float] = time.time) -> bytes:
    """An identity's root key, made at its first use and kept sealed with the master key: two instances making it at
    once keep the first."""
    aad = f"root-key:{identity_id}"
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO root_keys (identity_id, sealed, created) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (identity_id, master.seal(secrets.token_bytes(32), aad), clock()),
        )
        row = conn.execute("SELECT sealed FROM root_keys WHERE identity_id = %s", (identity_id,)).fetchone()
    assert row is not None
    return master.open(row[0], aad)


_NOT_THE_FIRST = (
    "the master key (LATET_MASTER_KEY_FILE, or LATET_MASTER_KEY) is not the one this database was first started with: "
    "it opens nothing of what is sealed here"
)


def _opened(master: MasterKey, sealed: bytes, aad: str) -> bytes | None:
    """What a sealed value says, or None when the master key opens nothing of it (a key that is not the one it was
    sealed with, a value that is none)."""
    with suppress(InvalidTag, MasterKeyError, ValueError):
        return master.open(sealed, aad)
    return None


def check_master_key(db: Database, master: MasterKey, *, clock: Callable[[], float] = time.time) -> None:
    """Stop a start with a master key other than the one the database was first started with: the first start seals a
    fixed text with the key and keeps it (two instances starting at once keep the first), every start opens it. A
    first start of a database that already holds an identity's root key needs that root key to open with the key too,
    or a check value lost with a partial restore would let a wrong key in. Raises MasterKeyError, in words that name the
    setting and never a key, when the key opens nothing of what it is asked to; DatabaseError, with the type of the
    driver's error, when PostgreSQL cannot be asked. The schema must be there (`migrate`)."""
    import psycopg  # the drivers come with the extra: the rest of this module (loading a key, sealing) needs none

    from latet.server.db import DatabaseError

    aad, kind = "master-check", ""
    row = None
    try:
        with db.connection() as conn:
            row = conn.execute("SELECT sealed FROM master_check WHERE id = 1").fetchone()
            if row is None:  # the first start, or a check value that was lost
                kept = conn.execute(
                    "SELECT identity_id, sealed FROM root_keys ORDER BY created, identity_id LIMIT 1"
                ).fetchone()
                if kept is not None and _opened(master, kept[1], f"root-key:{kept[0]}") is None:
                    raise MasterKeyError(_NOT_THE_FIRST)
                conn.execute(
                    "INSERT INTO master_check (id, sealed, created) VALUES (1, %s, %s) ON CONFLICT DO NOTHING",
                    (master.seal(_CHECK, aad), clock()),
                )
                row = conn.execute("SELECT sealed FROM master_check WHERE id = 1").fetchone()
    except psycopg.Error as exc:
        kind = type(exc).__name__
    if kind:
        raise DatabaseError(f"the master key's check value could not be read or made ({kind})")
    assert row is not None
    if _opened(master, row[0], aad) != _CHECK:
        raise MasterKeyError(_NOT_THE_FIRST)
