"""Local SQLite store. Originals are encrypted at rest (AES-GCM, key derived from the root key). A value's rule is kept
in clear, naming the field the value sat in by its kind alone (``modules.kept``): a field's name can be a value; and so
is how it was found (``decoded``: 1 where the text's escapes read as what they stand for, 0 as it is written, NULL for
an entry kept before Latet 0.9.10 recorded it).

Every statement names the columns it reads and writes, as 0.9.9's do: a vault with a column more, of a later Latet's,
stays one an earlier Latet opens, and its new entries say nothing of how they were found (NULL)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from latet.engine import modules
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, EntryGone, FakeTaken
from latet.storage.folders import make_private_folder

if TYPE_CHECKING:
    from latet.storage.upgrade_copies import Follow

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA secure_delete=ON;
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    etype TEXT NOT NULL,
    eclass INTEGER NOT NULL,
    original BLOB NOT NULL,
    surrogate TEXT NOT NULL UNIQUE,
    counter INTEGER NOT NULL,
    created REAL NOT NULL,
    rule TEXT,
    decoded INTEGER
);
CREATE TABLE IF NOT EXISTS reviews (
    key BLOB PRIMARY KEY,          -- keyed hash of the text the local reviewer read
    data BLOB NOT NULL,            -- sealed JSON: its findings (values in clear) and whether it blocked
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_ts ON reviews(ts);
CREATE TABLE IF NOT EXISTS image_decisions (
    key BLOB PRIMARY KEY,          -- keyed hash of the image
    data BLOB NOT NULL,            -- sealed JSON: the rectangles you drew on it, or that you withheld it
    ts REAL NOT NULL
);
"""
_REVIEW_DAYS = 90  # a text not resent for this long is reviewed again if it ever comes back
_ENTITY = "SELECT id, etype, eclass, original, surrogate, counter, created, rule, decoded"
FORGET = "DELETE FROM entities WHERE id = ?"  # what Forget does to a vault: this one, and its upgrade copies'
FORGET_ANSWER = "DELETE FROM reviews WHERE key = ?"  # and to the local reviewer's answers that hold a value forgotten
_SETTLE_WAIT_MS = 250  # how long writing the journal into the file waits for another program's read (``settle``)
_BUSY_MS = 5000  # the stores' own wait on another program (sqlite3.connect's five seconds)


def settle(db: sqlite3.Connection, vacuum: bool = False) -> bool:
    """What a deletion overwrote where it was (``secure_delete``) written into the database's file and its journal
    emptied (``wal_checkpoint(TRUNCATE)``), a VACUUM first for Clear history: no page of either keeps what was deleted
    for SQLite to reuse some day. It waits ``_SETTLE_WAIT_MS`` at most for another program reading the file (masking
    waits on the vault meanwhile), and never fails what was deleted, done already: False, said in the log in one line,
    when it could not, and the next deletion, or the store's close, writes the journal into the file."""
    if vacuum:
        try:
            db.execute("VACUUM")
        except sqlite3.Error as error:
            log.warning("the history's file was not made anew after it was cleared (%s)", type(error).__name__)
    try:
        db.execute(f"PRAGMA busy_timeout = {_SETTLE_WAIT_MS}")
        try:
            row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            db.execute(f"PRAGMA busy_timeout = {_BUSY_MS}")
    except sqlite3.Error as error:
        log.warning(
            "the journal was not written into the file after a deletion (%s): the next deletion, or the stop, does it",
            type(error).__name__,
        )
        return False
    if row is not None and row[0]:
        log.warning(
            "another program was reading the file: the journal is written into it at the next deletion, or the stop"
        )
        return False
    return True


def _opened(aead: AESGCM, sealed: bytes) -> str | None:
    """A text sealed in a vault, opened with ``aead``; None when another root key sealed it."""
    try:
        return aead.decrypt(bytes(sealed[:12]), bytes(sealed[12:]), None).decode()
    except Exception:  # sealed with another root key
        return None


def opened_values(db: sqlite3.Connection, aead: AESGCM, entity_ids: Iterable[str]) -> list[str]:
    """The values of ``entity_ids`` in the vault ``db``, opened with ``aead`` (``sealer``); one that does not open is
    left out."""
    ids = list(entity_ids)
    values = []
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" * len(chunk))
        rows = db.execute(f"SELECT original FROM entities WHERE id IN ({marks})", chunk).fetchall()  # noqa: S608
        values += [value for (sealed,) in rows if (value := _opened(aead, sealed)) is not None]
    return values


def answers_holding(db: sqlite3.Connection, aead: AESGCM, values: Iterable[str]) -> list[bytes]:
    """The keys of the local reviewer's remembered answers (``reviews``: sealed JSON whose findings, and those it could
    not place, are values in clear) that found one of ``values``: each answer opened and read once, whatever how many
    values, its findings looked up among them. One that does not open with ``aead``, or is no answer, stays."""
    wanted = {value for value in values if value}
    if not wanted:
        return []
    found = []
    for key, data in db.execute("SELECT key, data FROM reviews").fetchall():
        try:
            answer = json.loads(_opened(aead, data) or "null")
        except ValueError:
            continue
        if not isinstance(answer, dict):
            continue
        held = {
            str(entry[0])
            for group in ("findings", "unlocated")
            for entry in answer.get(group) or ()
            if isinstance(entry, list) and entry
        }
        if held & wanted:
            found.append(bytes(key))
    return found


def sealer(root_key: bytes) -> AESGCM:
    """What a vault's values are sealed with, made from the root key (``upgrade_copies.Follow`` checks a copy's)."""
    return AESGCM(hmac.new(root_key, b"storage", hashlib.sha256).digest())


def opens_with(aead: AESGCM, sealed: bytes) -> bool:
    """Whether a value sealed in a vault opens with ``aead`` (``sealer``): not when another root key sealed it."""
    try:
        aead.decrypt(sealed[:12], sealed[12:], None)
    except Exception:  # sealed with another root key, or no sealed value at all
        return False
    return True


class Store:
    def __init__(self, path: Path, root_key: bytes) -> None:
        make_private_folder(path.parent)
        self.path = path
        self._aead = sealer(root_key)
        self._image_hmac = hmac.new(root_key, b"image-decisions", hashlib.sha256).digest()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.unreadable = 0  # values sealed with another root key: skipped (latet doctor says so)
        # What forgetting a value does to the upgrade copies' vaults too (``upgrade_copies.Follow``): this computer's
        # vault alone has one, given by ``state.local.local_backends``.
        self.copies: Follow | None = None
        try:
            self._prepare()
        except BaseException:  # not a vault (a file damaged, or of something else): its file is not held open
            self._db.close()
            raise

    def _prepare(self) -> None:
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(entities)")}
        if "rule" not in columns:  # a vault from before the rules were kept
            self._db.execute("ALTER TABLE entities ADD COLUMN rule TEXT")
        if "decoded" not in columns:  # a vault from before how a value was found was kept (0.9.9 and earlier)
            self._db.execute("ALTER TABLE entities ADD COLUMN decoded INTEGER")
        # Rules an earlier Latet kept with a field's name in them, which can be a value: by the field's kind from now
        # on (``modules.kept``).
        named = "SELECT id, rule FROM entities WHERE rule LIKE 'strict:%' OR rule LIKE 'field:%' OR rule LIKE 'field-%'"
        for entity_id, rule in self._db.execute(named).fetchall():
            if modules.kept(rule) != rule:
                self._db.execute("UPDATE entities SET rule = ? WHERE id = ?", (modules.kept(rule), entity_id))

    def seal(self, text: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aead.encrypt(nonce, text.encode(), None)

    def open(self, blob: bytes) -> str:
        return self._aead.decrypt(blob[:12], blob[12:], None).decode()

    def _entity(self, row: Any) -> Entity | None:
        """A row as an entity; None when it was sealed with another root key (counted in ``unreadable``)."""
        try:
            original = self.open(row[3])
        except Exception:  # written with another root key: unusable
            log.warning("skipping vault entry %s: cannot decrypt", row[0])
            self.unreadable += 1
            return None
        decoded = None if row[8] is None else bool(row[8])
        return Entity(
            row[0], row[1], EntityClass(row[2]), original, row[4], row[5], row[6], modules.kept(row[7]), decoded
        )

    def load_entities(self) -> list[Entity]:
        with self._lock:
            rows = self._db.execute(f"{_ENTITY} FROM entities").fetchall()
        self.unreadable = 0
        return [entity for row in rows if (entity := self._entity(row)) is not None]

    def load_entity(self, entity_id: str) -> Entity | None:
        """One value by its id (another process may have kept it); None when there is none or it cannot be opened."""
        with self._lock:
            row = self._db.execute(f"{_ENTITY} FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return self._entity(row) if row is not None else None

    def holds(self, entity_id: str) -> bool:
        """Whether an entry of that id is there, whether it opens or not."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone() is not None

    def opens(self) -> bool:
        """Whether its values open with this root key, as its first one does (an empty vault does)."""
        with self._lock:
            row = self._db.execute("SELECT original FROM entities LIMIT 1").fetchone()
        return row is None or opens_with(self._aead, row[0])

    def save_entity(self, e: Entity) -> Entity | None:
        """Keep ``e``: None when it is kept as given; the entity of the same value when another process kept it first
        (the vault uses that one). Raises FakeTaken when ``e``'s fake is another value's."""
        rule = modules.kept(e.rule)
        decoded = None if e.decoded is None else int(e.decoded)
        row = (e.id, e.etype, int(e.eclass), self.seal(e.original), e.surrogate, e.counter, e.created, rule, decoded)
        with self._lock:
            done = self._db.execute(
                "INSERT OR IGNORE INTO entities"
                " (id, etype, eclass, original, surrogate, counter, created, rule, decoded)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            ).rowcount
        if done:
            return None
        kept = self.load_entity(e.id)
        if kept is None:
            raise FakeTaken(f"the fake made for a {e.etype} is another value's")
        return kept

    def save_rule(self, e: Entity) -> None:
        """The rule of a value recorded before rules were kept."""
        with self._lock:
            self._db.execute("UPDATE entities SET rule = ? WHERE id = ? AND rule IS NULL", (modules.kept(e.rule), e.id))

    def replace_fake(self, old: Entity, new: Entity) -> Entity | None:
        """Keep ``new``'s fake in place of ``old``'s, in their value's entry: None when kept so; the entry as another
        process changed it first otherwise. Raises FakeTaken when the fake is another value's, EntryGone when the entry
        is gone."""
        with self._lock:
            try:
                done = self._db.execute(
                    "UPDATE entities SET surrogate = ?, counter = ? WHERE id = ? AND surrogate = ?",
                    (new.surrogate, new.counter, old.id, old.surrogate),
                ).rowcount
            except sqlite3.IntegrityError:  # the surrogate is unique: another value's
                raise FakeTaken(f"the fake made for a {new.etype} is another value's") from None
        if done:
            return None
        now = self.load_entity(old.id)
        if now is None:
            raise EntryGone(f"the entry of a {old.etype} was forgotten meanwhile")
        return now

    def load_review(self, key: bytes) -> dict[str, Any] | None:
        """What the local reviewer found in a text it read before (see engine.reviewer)."""
        with self._lock:
            row = self._db.execute("SELECT data FROM reviews WHERE key = ?", (key,)).fetchone()
            if row is not None:
                self._db.execute("UPDATE reviews SET ts = ? WHERE key = ?", (time.time(), key))
        if row is None:
            return None
        try:
            data = json.loads(self.open(row[0]))
        except Exception:  # written with another root key: unusable
            return None
        return data if isinstance(data, dict) else None

    def _image_key(self, key: str) -> bytes:
        return hmac.new(self._image_hmac, key.encode(), hashlib.sha256).digest()

    def load_image_decision(self, key: str) -> dict[str, Any] | None:
        """What you decided on an image in the Live Reviewer (``key``: the image's own key)."""
        with self._lock:
            row = self._db.execute("SELECT data FROM image_decisions WHERE key = ?", (self._image_key(key),)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(self.open(row[0]))
        except Exception:  # sealed with another root key
            return None

    def save_image_decision(self, key: str, decision: dict[str, Any]) -> None:
        sealed = self.seal(json.dumps(decision))
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO image_decisions (key, data, ts) VALUES (?, ?, ?)",
                (self._image_key(key), sealed, time.time()),
            )

    def save_review(self, key: bytes, answer: dict[str, Any]) -> None:
        sealed = self.seal(json.dumps(answer, ensure_ascii=False))
        now = time.time()
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO reviews (key, data, ts) VALUES (?, ?, ?)", (key, sealed, now))
            self._db.execute("DELETE FROM reviews WHERE ts < ?", (now - _REVIEW_DAYS * 86400,))

    def forget_reviews(self) -> None:
        """Every answer the local reviewer remembered: what it read is read again."""
        with self._lock:
            self._db.execute("DELETE FROM reviews")

    def forget_entity(self, entity_id: str) -> None:
        """Forget a value: here, and in every copy `latet upgrade` took of this vault (``copies``)."""
        self.forget_entities([entity_id])

    def forget_entities(self, entity_ids: Iterable[str]) -> None:
        """Forget values at once (the names today's filters reject, at the start), and the local reviewer's answers
        that hold one: here, then in every copy `latet upgrade` took of this vault, each opened once for all of them
        (``copies``)."""
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return
        with self._lock:
            # The local reviewer's answers that hold one of the values go with them (read again if their text comes
            # back): opened before the values go.
            answers = answers_holding(self._db, self._aead, opened_values(self._db, self._aead, ids))
            # The values and their answers in one transaction: never a value gone while an answer still holds it.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany(FORGET, [(entity_id,) for entity_id in ids])
                self._db.executemany(FORGET_ANSWER, [(key,) for key in answers])
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            settle(self._db)  # what was deleted, overwritten, into the file; never fails the Forget
        if self.copies is not None:
            self.copies.forget_entities(ids)

    def close(self) -> None:
        with self._lock:
            self._db.close()
