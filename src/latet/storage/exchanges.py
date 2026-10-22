"""The dashboard's record of what left the machine: exchanges, the text blocks they carried
(stored once, however many turns resend them), the images as they were sent, and the response as
the model wrote it.

Only masked text is kept here, sealed with AES-GCM: originals are looked up in the vault when the
dashboard shows a block, so a value forgotten from the vault disappears from the history too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from latet.engine.modules import kept, module_of
from latet.storage.folders import make_private_folder

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,           -- keyed hash of the masked text
    kind TEXT NOT NULL,
    data BLOB NOT NULL,            -- sealed JSON [masked text, replacements]
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_seen ON blocks(last_seen);
CREATE TABLE IF NOT EXISTS exchanges (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    agent TEXT NOT NULL,
    flags TEXT NOT NULL,           -- " blocked leak uninspected:image " (padded, for LIKE)
    summary TEXT NOT NULL,         -- JSON ExchangeSummary: counts and metadata, no content
    refs BLOB,                     -- sealed JSON [[block id, path, from_model, new(, tool(, source))], ...]
    request_headers BLOB,          -- sealed JSON, credentials already redacted
    response BLOB,                 -- sealed JSON {headers, text, text_spans, tool_calls}
    leaks TEXT NOT NULL DEFAULT '[]',
    body_bytes INTEGER NOT NULL DEFAULT 0,
    project TEXT                   -- id of the session's project, when known
);
CREATE INDEX IF NOT EXISTS exchanges_ts ON exchanges(ts);
CREATE INDEX IF NOT EXISTS exchanges_session ON exchanges(session, ts);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    project TEXT,
    block TEXT NOT NULL,           -- id of the block as it left (see blocks)
    kind TEXT NOT NULL,
    inline INTEGER NOT NULL,       -- 1: before the request left; 0: in the background
    summary TEXT NOT NULL          -- JSON: categories, what was masked, failed, block; never a value
);
CREATE INDEX IF NOT EXISTS reviews_ts ON reviews(ts);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,           -- keyed hash of the image as it was sent
    data BLOB NOT NULL,            -- sealed JSON {media_type, data (base64)}
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS images_seen ON images(last_seen);
"""
MAX_IMAGE = 10 * 1024 * 1024  # larger images are listed, not kept
_CHUNK = 500  # SQLite host parameters per statement
_SUMMARIES_AT_ONCE = 1_000  # summaries read into objects at a time by ``summaries_parts``: what one part holds


@dataclass(slots=True)
class BlockRecord:
    id: str
    path: str
    kind: str
    from_model: bool
    new: bool  # first time this block left in this session
    masked: str | None = None  # None: already stored
    replacements: list[dict[str, Any]] = field(default_factory=list)
    tool: str | None = None  # the tool whose output a tool result is, when the request said
    # The file or the web address that output came from, when its call said, as the request carried it (masked), a web
    # address by its scheme and host alone (``recorder.kept_source``); and where values are masked in it, as for
    # ``replacements``: it is shown with them from the vault, so a value forgotten there is gone from it too.
    source: str | None = None
    source_found: list[dict[str, Any]] = field(default_factory=list)


def block_ref(block: BlockRecord) -> list[Any]:
    """A block as an exchange refers to it: id, path, from_model, new, and its tool and source when known (older
    references have four or five items). The source is its text, or its text and where values are masked in it
    (``[text, replacements]``)."""
    ref: list[Any] = [block.id, block.path, int(block.from_model), int(block.new)]
    if block.source:
        return [*ref, block.tool, [block.source, block.source_found] if block.source_found else block.source]
    return [*ref, block.tool] if block.tool else ref


def ref_parts(ref: Sequence[Any]) -> tuple[str, str, bool, bool, str | None, Any]:
    """A reference as ``block_ref`` wrote it: id, path, from_model, new, tool, source (None when not kept; its text, or
    its text and where values are masked in it, which ``recorder.restored_source`` reads)."""
    tool = ref[4] if len(ref) > 4 else None
    return ref[0], ref[1], bool(ref[2]), bool(ref[3]), tool, (ref[5] if len(ref) > 5 else None)


@dataclass(slots=True)
class ImageRecord:
    """An image of a request as it was sent: masked, clean (nothing to mask), or withheld (not sent)."""

    path: str
    state: str
    id: str | None = None  # None: not kept (withheld, or too large)
    media_type: str | None = None
    data: str | None = None  # base64, when not stored yet
    block: str | None = None  # the block of the text read in it


@dataclass(slots=True)
class ExchangeRecord:
    summary: dict[str, Any]
    blocks: list[BlockRecord] = field(default_factory=list)
    images: list[ImageRecord] = field(default_factory=list)
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    response: dict[str, Any] | None = None
    leaks: list[dict[str, Any]] = field(default_factory=list)
    body_bytes: int = 0


def new_exchange_id() -> str:
    return f"ex_{int(time.time() * 1000):x}{os.urandom(3).hex()}"


class ExchangeStore:
    def __init__(self, path: Path, root_key: bytes, retention_days: int = 14) -> None:
        make_private_folder(path.parent)
        self._aead = AESGCM(hmac.new(root_key, b"exchanges", hashlib.sha256).digest())
        self._id_key = hmac.new(root_key, b"exchange-ids", hashlib.sha256).digest()
        # zstandard's compressors and decompressors are not for two threads at once, and the recorder's thread and the
        # dashboard's workers use one store together: the compressor is used under the lock alone (``record``), and
        # each opening makes a decompressor of its own (``image`` opens outside the lock), which costs microseconds.
        self._zc = zstandard.ZstdCompressor(level=6)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(exchanges)")}
        if "project" not in columns:
            self._db.execute("ALTER TABLE exchanges ADD COLUMN project TEXT")  # a history from before projects
        if "images" not in columns:
            self._db.execute("ALTER TABLE exchanges ADD COLUMN images BLOB")  # sealed JSON [image, ...]
        self._db.execute("CREATE INDEX IF NOT EXISTS exchanges_project ON exchanges(project, ts)")
        self._cut_field_names()
        # Pruned by its owner once the retention is known (`make_runtime`): opening it prunes nothing.
        self.retention_days = retention_days

    def _cut_field_names(self) -> None:
        """What stopped a request is kept in clear (``leaks``): a rule an earlier Latet kept there with the name of the
        field a value sat in, which can be a value, names it by its kind from now on (``modules.kept``)."""
        named = (
            'SELECT id, leaks FROM exchanges WHERE leaks LIKE \'%"rule": "strict:%\' OR leaks LIKE \'%"rule": "field%\''
        )
        for exchange_id, raw in self._db.execute(named).fetchall():
            try:
                leaks = json.loads(raw)
            except ValueError:
                continue
            cut = [
                leak | {"rule": kept(leak["rule"])} if isinstance(leak, dict) and leak.get("rule") else leak
                for leak in leaks
            ]
            if cut != leaks:
                self._db.execute("UPDATE exchanges SET leaks = ? WHERE id = ?", (json.dumps(cut), exchange_id))

    # --- sealing ----------------------------------------------------------------------------------

    def _seal(self, value: Any, aad: str) -> bytes:
        nonce = os.urandom(12)
        data = self._zc.compress(json.dumps(value, ensure_ascii=False).encode())
        return nonce + self._aead.encrypt(nonce, data, aad.encode())

    def _open(self, sealed: bytes | None, aad: str) -> Any:
        if sealed is None:
            return None
        opened = self._aead.decrypt(sealed[:12], sealed[12:], aad.encode())
        return json.loads(zstandard.ZstdDecompressor().decompress(opened))

    def block_id(self, masked: str) -> str:
        return hmac.new(self._id_key, masked.encode(), hashlib.sha256).hexdigest()[:32]

    def image_id(self, data: str) -> str:
        return "im_" + hmac.new(self._id_key, b"image\0" + data.encode(), hashlib.sha256).hexdigest()[:32]

    # --- writes -----------------------------------------------------------------------------------

    def known_blocks(self, ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(ids))
        found: set[str] = set()
        with self._lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                found.update(r[0] for r in self._db.execute(f"SELECT id FROM blocks WHERE id IN ({marks})", part))  # noqa: S608
        return found

    def last_used(self) -> dict[str, float]:
        """When each route (`anthropic`, a destination's name...) last carried an exchange."""
        query = "SELECT json_extract(summary, '$.upstream'), MAX(ts) FROM exchanges GROUP BY 1"
        with self._lock:
            return {str(name): float(ts) for name, ts in self._db.execute(query) if name}

    def block_types(self, ids: Iterable[str]) -> dict[str, list[tuple[str, str, str, int]]]:
        """Values masked in stored blocks, as (type, class, module, count)."""
        ids = list(dict.fromkeys(ids))
        out: dict[str, list[tuple[str, str, str, int]]] = {}
        with self._lock:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                for block_id, data in self._db.execute(f"SELECT id, data FROM blocks WHERE id IN ({marks})", part):  # noqa: S608
                    replacements = self._open(data, block_id)[1]
                    counts = Counter(
                        (r["type"], r.get("class", "?"), r.get("module") or module_of(r.get("rule"), r["type"]))
                        for r in replacements
                    )
                    out[block_id] = sorted((t, c, m, n) for (t, c, m), n in counts.items())
        return out

    def record(self, rec: ExchangeRecord) -> str:
        summary = rec.summary
        exchange_id = summary.setdefault("id", new_exchange_id())
        now = time.time()
        refs = [block_ref(b) for b in rec.blocks]
        images = [{"id": i.id, "path": i.path, "state": i.state, "block": i.block} for i in rec.images]
        flags = " " + " ".join(summary.get("flags", [])) + " "
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for b in rec.blocks:
                    if b.masked is not None:
                        self._db.execute(
                            "INSERT OR IGNORE INTO blocks VALUES (?, ?, ?, ?)",
                            (b.id, b.kind, self._seal([b.masked, b.replacements], b.id), now),
                        )
                for image in rec.images:
                    if image.id is None:
                        continue
                    if image.data is not None:
                        sealed = self._seal({"media_type": image.media_type, "data": image.data}, image.id)
                        self._db.execute("INSERT OR IGNORE INTO images VALUES (?, ?, ?)", (image.id, sealed, now))
                    self._db.execute("UPDATE images SET last_seen = ? WHERE id = ?", (now, image.id))
                ids = list(dict.fromkeys(b.id for b in rec.blocks))
                for i in range(0, len(ids), _CHUNK):
                    part = ids[i : i + _CHUNK]
                    marks = ",".join("?" * len(part))
                    self._db.execute(f"UPDATE blocks SET last_seen = ? WHERE id IN ({marks})", [now, *part])  # noqa: S608
                self._db.execute(
                    "INSERT OR REPLACE INTO exchanges (id, ts, session, agent, flags, summary, refs, request_headers,"
                    " response, leaks, body_bytes, project, images) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        exchange_id,
                        summary["ts"],
                        summary["session"],
                        summary.get("agent", "other"),
                        flags,
                        json.dumps(summary),
                        self._seal(refs, f"{exchange_id}:refs") if refs else None,
                        self._seal(rec.request_headers, f"{exchange_id}:headers") if rec.request_headers else None,
                        self._seal(rec.response, f"{exchange_id}:response") if rec.response is not None else None,
                        json.dumps(rec.leaks),
                        rec.body_bytes,
                        (summary.get("project") or {}).get("id"),
                        self._seal(images, f"{exchange_id}:images") if images else None,
                    ),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return exchange_id

    def record_review(self, summary: dict[str, Any]) -> str:
        """What the local reviewer said of one block: categories and where in the block, never values
        (the block holds them, sealed; a masked value is in the vault)."""
        review_id = f"rv_{int(time.time() * 1000):x}{os.urandom(3).hex()}"
        with self._lock:
            self._db.execute(
                "INSERT INTO reviews (id, ts, session, project, block, kind, inline, summary)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    summary["ts"],
                    summary["session"],
                    summary.get("project"),
                    summary["block"],
                    summary["kind"],
                    int(bool(summary.get("inline"))),
                    json.dumps(summary),
                ),
            )
        return review_id

    def reviews(self, limit: int = 100, session: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT id, summary FROM reviews"
        args: list[Any] = []
        if session:
            query += " WHERE session = ?"
            args.append(session)
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [{"id": rid, **json.loads(data)} for rid, data in rows]

    def prune(self, now: float | None = None) -> int:
        """Drop exchanges older than the retention period, and blocks no newer exchange carried."""
        cutoff = (now or time.time()) - self.retention_days * 86400
        with self._lock:
            removed = self._db.execute("DELETE FROM exchanges WHERE ts < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM blocks WHERE last_seen < ?", (cutoff,))
            self._db.execute("DELETE FROM reviews WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM images WHERE last_seen < ?", (cutoff,))
        return removed

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM exchanges")
            self._db.execute("DELETE FROM blocks")
            self._db.execute("DELETE FROM reviews")
            self._db.execute("DELETE FROM images")
            self._db.execute("VACUUM")

    # --- reads ------------------------------------------------------------------------------------

    def list(
        self,
        limit: int = 100,
        before: str | None = None,
        session: str | None = None,
        agent: str | None = None,
        flag: str | None = None,
        project: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        where: list[str] = []
        args: list[Any] = []
        with self._lock:
            if before:
                row = self._db.execute("SELECT ts FROM exchanges WHERE id = ?", (before,)).fetchone()
                if row:
                    where.append("(ts < ? OR (ts = ? AND id < ?))")
                    args += [row[0], row[0], before]
            if session:
                where.append("session = ?")
                args.append(session)
            if agent:
                where.append("agent = ?")
                args.append(agent)
            if flag:
                where.append("(flags LIKE ? OR flags LIKE ?)")
                args += [f"% {flag} %", f"% {flag}:%"]
            if project:
                where.append("project = ?")
                args.append(project)
            query = "SELECT summary FROM exchanges"
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " ORDER BY ts DESC, id DESC LIMIT ?"
            items = [json.loads(r[0]) for r in self._db.execute(query, [*args, limit + 1])]
        next_id = items[limit - 1]["id"] if len(items) > limit else None
        return items[:limit], next_id

    def in_order(self, session: str) -> list[str]:
        """The ids of one conversation's exchanges, in the order they were recorded. An agent sends a conversation's
        requests one after the other, so this is the order they were sent: their start times can be equal (a tick
        of Windows' clock is 15.6 ms), and an id's random tail sorts them at random then."""
        with self._lock:
            rows = self._db.execute("SELECT id FROM exchanges WHERE session = ? ORDER BY rowid", (session,))
            return [row[0] for row in rows]

    def detail(self, exchange_id: str) -> dict[str, Any] | None:
        """The stored exchange: masked blocks and response, with replacement offsets in masked text."""
        with self._lock:
            row = self._db.execute(
                "SELECT summary, refs, request_headers, response, leaks, body_bytes, images"
                " FROM exchanges WHERE id = ?",
                (exchange_id,),
            ).fetchone()
            if row is None:
                return None
            summary, refs, headers, response, leaks, body_bytes, images = row
            images = self._open(images, f"{exchange_id}:images") or []
            kept = self._known_images([i["id"] for i in images if i.get("id")])
            for image in images:
                image["kept"] = image.get("id") in kept
            refs = self._open(refs, f"{exchange_id}:refs") or []
            ids = list(dict.fromkeys(r[0] for r in refs))
            stored: dict[str, tuple[str, list[dict[str, Any]], str]] = {}
            for i in range(0, len(ids), _CHUNK):
                part = ids[i : i + _CHUNK]
                marks = ",".join("?" * len(part))
                for block_id, kind, data in self._db.execute(
                    f"SELECT id, kind, data FROM blocks WHERE id IN ({marks})",  # noqa: S608
                    part,
                ):
                    masked, replacements = self._open(data, block_id)
                    stored[block_id] = (masked, replacements, kind)
            slots = []
            for ref in refs:
                block_id, path, from_model, new, tool, source = ref_parts(ref)
                found = stored.get(block_id)
                if found is None:
                    continue  # pruned
                masked, replacements, kind = found
                slots.append(
                    {
                        "block": block_id,
                        "path": path,
                        "kind": kind,
                        "from_model": from_model,
                        "new": new,
                        "masked": masked,
                        "replacements": replacements,
                        "tool": tool,
                        "source": source,
                    }
                )
            return {
                "summary": json.loads(summary),
                "request": {
                    "headers": self._open(headers, f"{exchange_id}:headers") or [],
                    "slots": slots,
                    "images": images,
                    "body_bytes": body_bytes,
                },
                "response": self._open(response, f"{exchange_id}:response"),
                "leaks": json.loads(leaks),
            }

    def _known_images(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0] for r in self._db.execute(f"SELECT id FROM images WHERE id IN ({marks})", ids)}  # noqa: S608

    def image(self, image_id: str) -> tuple[str, bytes] | None:
        """An image as it was sent: its media type and bytes."""
        import base64

        with self._lock:
            row = self._db.execute("SELECT data FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            return None
        found = self._open(row[0], image_id)
        return str(found["media_type"]), base64.b64decode(found["data"])

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT session, MAX(agent), MIN(ts), MAX(ts), COUNT(*),"
                " COALESCE(SUM(json_extract(summary, '$.new_masked')), 0), SUM(flags LIKE '% blocked %')"
                " FROM exchanges GROUP BY session ORDER BY MAX(ts) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"key": k, "agent": a, "first_ts": f, "last_ts": la, "exchanges": n, "masked": m, "blocked": b}
            for k, a, f, la, n, m, b in rows
        ]

    def summaries_since(self, ts: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT summary FROM exchanges WHERE ts >= ? ORDER BY ts", (ts,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def summaries_parts(self, ts: float, most: int) -> Iterator[list[dict[str, Any]]]:
        """The summaries from ``ts`` on, the latest ``most`` at most, the newest first: their rows read at once under
        the lock (no statement is left open across the parts, which the recorder's writes and ``clear`` would meet),
        each part read into objects only when it is asked for."""
        with self._lock:
            query = "SELECT summary FROM exchanges WHERE ts >= ? ORDER BY ts DESC LIMIT ?"
            rows = self._db.execute(query, (ts, max(0, most))).fetchall()
        for start in range(0, len(rows), _SUMMARIES_AT_ONCE):
            yield [json.loads(row[0]) for row in rows[start : start + _SUMMARIES_AT_ONCE]]

    def close(self) -> None:
        with self._lock:
            self._db.close()
