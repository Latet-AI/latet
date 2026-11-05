"""Protected authenticated plugin binding, with a bounded writer quota for each identity (root's ruling I-3 on P2-6a's
review): an identity's writers lock its own quota row alone, and one identity that fills its quota never refuses
another identity's binding."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field

import psycopg

from latet.server.db import Database

_CLOCK = "floor(extract(epoch FROM clock_timestamp()) * 1000000)::bigint"
_CAP = 256  # live bindings of one identity: a binding it has is always refreshed, a new one waits for a free slot
_IDLE_US = 30 * 60 * 1_000_000
_MAX_BIGINT = (1 << 63) - 1
_ISSUER = re.compile(r"u_[a-z2-7]{10}")
_PROJECT = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True, slots=True, repr=False)
class PluginBinding:
    session_digest: bytes
    project: bytes
    binding_digest: bytes
    required: bool
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class PluginBindings:
    db: Database
    issuer: str
    root_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if _ISSUER.fullmatch(self.issuer) is None or len(self.root_key) != 32:
            raise ValueError("invalid plugin binding owner")

    def session_digest(self, session: str) -> bytes | None:
        if type(session) is not str or not session.startswith("cc:") or not 3 < len(session) <= 1024:
            return None
        try:
            encoded = session.encode("utf-8")
        except UnicodeError:
            return None
        if len(encoded) > 1024:
            return None
        return hmac.digest(self.root_key, b"plugin-session-v1\0" + encoded, "sha256")

    def binding_digest(self, project: str, root: str) -> bytes | None:
        if (
            type(project) is not str
            or _PROJECT.fullmatch(project) is None
            or type(root) is not str
            or not 0 < len(root) <= 4096
        ):
            return None
        try:
            encoded = root.encode("utf-8")
        except UnicodeError:
            return None
        if len(encoded) > 4096:
            return None
        return hmac.digest(self.root_key, b"plugin-binding-v1\0" + bytes.fromhex(project) + encoded, "sha256")

    def refresh(self, session: str, project: str, root: str, *, required: bool) -> bool:
        """Authenticated bridge only. A quota or protected-store failure grants no authority."""
        key = self.session_digest(session)
        binding = self.binding_digest(project, root)
        if key is None or binding is None:
            return False
        project_id = bytes.fromhex(project)
        try:
            with self.db.transaction() as conn:
                identity = conn.execute(
                    "SELECT NOT disabled FROM identities WHERE id=%s FOR SHARE", (self.issuer,)
                ).fetchone()
                if identity != (True,):
                    return False
                # The identity's own quota row, made on its first binding: its writers alone wait for its lock.
                conn.execute(
                    "INSERT INTO legacy_plugin_identity_quota (issuer, entries, generation) VALUES (%s, 0, 0) "
                    "ON CONFLICT (issuer) DO NOTHING",
                    (self.issuer,),
                )
                quota = conn.execute(
                    "SELECT entries, generation FROM legacy_plugin_identity_quota WHERE issuer=%s FOR UPDATE",
                    (self.issuer,),
                ).fetchone()
                if quota is None:
                    return False
                clock = conn.execute("SELECT " + _CLOCK).fetchone()
                if clock is None or not 0 <= clock[0] <= _MAX_BIGINT - _IDLE_US:
                    return False
                now = clock[0]
                entries, sequence = quota
                # The identity's expired rows still cost it a slot until deleted here, a few at each write.
                removed = conn.execute(
                    "WITH expired AS (SELECT issuer, session_digest FROM legacy_plugin_binding "
                    "WHERE issuer=%s AND expires_us <= %s ORDER BY expires_us, session_digest LIMIT 32), "
                    "deleted AS (DELETE FROM legacy_plugin_binding b USING expired e "
                    "WHERE b.issuer=e.issuer AND b.session_digest=e.session_digest RETURNING 1) "
                    "SELECT count(*) FROM deleted",
                    (self.issuer, now),
                ).fetchone()
                if removed is None or not 0 <= removed[0] <= entries:
                    raise psycopg.DataError("plugin quota invariant")
                entries -= removed[0]
                previous = conn.execute(
                    "SELECT project_id, binding_digest, required, generation, expires_us "
                    "FROM legacy_plugin_binding WHERE issuer=%s AND session_digest=%s",
                    (self.issuer, key),
                ).fetchone()
                live = previous is not None and previous[4] > now
                if live and previous is not None:
                    required = required or previous[2]
                unchanged = live and previous is not None and previous[:3] == (project_id, binding, required)
                fits = previous is not None or entries < _CAP
                written = fits and (unchanged or sequence < _MAX_BIGINT)
                generation = None
                if written:
                    if unchanged and previous is not None:
                        generation = previous[3]
                    else:
                        sequence += 1
                        generation = sequence
                    if previous is None:
                        entries += 1
                conn.execute(
                    "UPDATE legacy_plugin_identity_quota SET entries=%s, generation=%s WHERE issuer=%s",
                    (entries, sequence, self.issuer),
                )
                if generation is None:
                    return False
                conn.execute(
                    "INSERT INTO legacy_plugin_binding "
                    "(issuer, session_digest, project_id, binding_digest, required, generation, expires_us) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (issuer, session_digest) DO UPDATE SET "
                    "project_id=excluded.project_id, binding_digest=excluded.binding_digest, "
                    "required=excluded.required, generation=excluded.generation, expires_us=excluded.expires_us",
                    (self.issuer, key, project_id, binding, required, generation, now + _IDLE_US),
                )
                return True
        except psycopg.Error:
            return False

    def _read(self, key: bytes) -> PluginBinding | None:
        try:
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT b.project_id, b.binding_digest, b.required, b.generation "
                    "FROM legacy_plugin_binding b JOIN identities i ON i.id=b.issuer "
                    "WHERE b.issuer=%s AND b.session_digest=%s AND NOT i.disabled AND b.expires_us > " + _CLOCK,
                    (self.issuer, key),
                ).fetchone()
        except psycopg.Error:
            return None
        return PluginBinding(key, *row) if row is not None else None

    def read(self, session: str) -> PluginBinding | None:
        key = self.session_digest(session)
        return self._read(key) if key is not None else None

    def current(self, binding: PluginBinding) -> bool:
        return not binding.required and self._read(binding.session_digest) == binding
