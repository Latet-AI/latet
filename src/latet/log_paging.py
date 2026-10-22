"""Small administration-log cursor protocol, usable without the server extra.

This token is a seek position for an already authorized reader, not authentication or a retained snapshot.
Empty action/actor filters have the same meaning as absent filters, as on the existing administration API.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass

LOG_PAGE_BYTES = 16 * 1024 * 1024
LOG_MOST = 10_000
ID_MOST = 9223372036854775807
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,1024}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def finite_time(text: str | None) -> float | None:
    if text is None:
        return None
    if not text.isascii() or not 1 <= len(text) <= 64:
        raise ValueError("invalid log query")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("invalid log query")
    return value


def page_limit(text: str | None, default: int = 500) -> int:
    if text is None:
        return default
    if not text.isascii() or not text.isdecimal() or not 1 <= len(text) <= 5:
        raise ValueError("invalid log query")
    value = int(text)
    if not 1 <= value <= LOG_MOST:
        raise ValueError("invalid log query")
    return value


@dataclass(frozen=True)
class LogFilters:
    since: float | None = None
    until: float | None = None
    action: str | None = None
    actor: str | None = None

    def digest(self) -> str:
        normalized = {
            "since": None if self.since is None else self.since.hex(),
            "until": None if self.until is None else self.until.hex(),
            "action": self.action or None,
            "actor": self.actor or None,
        }
        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


def _integer(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or not 1 <= len(text) <= 19:
        raise ValueError("invalid log cursor")
    value = int(text)
    if not 1 <= value <= ID_MOST:
        raise ValueError("invalid log cursor")
    return value


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid log cursor")
        result[key] = value
    return result


def _encode(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class LogCursor:
    ts: float
    id: int
    ceiling_id: int
    filters: str

    def encode(self) -> str:
        return _encode(
            {"v": 1, "ts": self.ts.hex(), "id": self.id, "ceiling_id": self.ceiling_id, "filters": self.filters}
        )

    @classmethod
    def decode(cls, token: str, filters: LogFilters) -> LogCursor:
        try:
            if not _TOKEN.fullmatch(token):
                raise ValueError
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if len(raw) > 768:
                raise ValueError
            # The protocol is flat: reject nesting before asking the JSON decoder to traverse it.
            text = raw.decode("ascii")
            if any(char in text for char in "[]") or text.count("{") != 1 or text.count("}") != 1:
                raise ValueError
            payload = json.loads(text, object_pairs_hook=_object, parse_int=_integer)
            if not isinstance(payload, dict) or set(payload) != {"v", "ts", "id", "ceiling_id", "filters"}:
                raise ValueError
            version, stamp, row, ceiling, digest = (payload[key] for key in ("v", "ts", "id", "ceiling_id", "filters"))
            if type(version) is not int or version != 1 or type(row) is not int or type(ceiling) is not int:
                raise ValueError
            if not 1 <= row <= ceiling <= ID_MOST or not isinstance(stamp, str) or len(stamp) > 64:
                raise ValueError
            value = float.fromhex(stamp)
            if not math.isfinite(value) or value.hex() != stamp:
                raise ValueError
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or digest != filters.digest():
                raise ValueError
            cursor = cls(value, row, ceiling, digest)
            if cursor.encode() != token:
                raise ValueError
            return cursor
        except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError, OverflowError):
            raise ValueError("invalid log cursor") from None
