"""Local SQLite store. Originals are encrypted at rest (AES-GCM, key derived from the root key). A value's rule is kept
in clear, naming the field the value sat in by its kind alone (``modules.kept``): a field's name can be a value."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from latet.engine import modules
from latet.engine.types import EntityClass
from latet.engine.vault import Entity, EntryGone, FakeTaken
from latet.storage.folders import make_private_folder

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    etype TEXT NOT NULL,
    eclass INTEGER NOT NULL,
    original BLOB NOT NULL,
    surrogate TEXT NOT NULL UNIQUE,
    counter INTEGER NOT NULL,
    created REAL NOT NULL,
    rule TEXT
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
_ENTITY = "SELECT id, etype, eclass, original, surrogate, counter, created, rule"


class Store:
    def __init__(self, path: Path, root_key: bytes) -> None:
        make_private_folder(path.parent)
        self.path = path
        self._aead = AESGCM(hmac.new(root_key, b"storage", hashlib.sha256).digest())
        self._image_hmac = hmac.new(root_key, b"image-decisions", hashlib.sha256).digest()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        self.unreadable = 0  # values sealed with another root key: skipped (latet doctor says so)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(entities)")}
        if "rule" not in columns:  # a vault from before the rules were kept
            self._db.execute("ALTER TABLE entities ADD COLUMN rule TEXT")
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
        return Entity(row[0], row[1], EntityClass(row[2]), original, row[4], row[5], row[6], modules.kept(row[7]))

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

    def save_entity(self, e: Entity) -> Entity | None:
        """Keep ``e``: None when it is kept as given; the entity of the same value when another process kept it first
        (the vault uses that one). Raises FakeTaken when ``e``'s fake is another value's."""
        rule = modules.kept(e.rule)
        with self._lock:
            done = self._db.execute(
                "INSERT OR IGNORE INTO entities (id, etype, eclass, original, surrogate, counter, created, rule)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (e.id, e.etype, int(e.eclass), self.seal(e.original), e.surrogate, e.counter, e.created, rule),
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
        with self._lock:
            self._db.execute("DELETE FROM entities WHERE id = ?", (entity_id,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
