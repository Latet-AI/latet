"""Local raw capture of exchanges, for debugging and protocol fixtures.

Off by default. When enabled it writes every exchange to JSONL files in a local directory,
with credential headers reduced to their length. Captures contain conversation content:
they never leave the machine and must never be committed.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from pathlib import Path
from typing import Any

from latet.engine.response_guard import safe_text
from latet.gateway.headers import redacted
from latet.logline import redacted as redacted_line
from latet.protocols import codec

_counter = itertools.count(1)


def _text(body: bytes) -> str | dict[str, Any]:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary": len(body)}


class Capture:
    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    def exchange(self, kind: str, upstream: str, method: str, url: str) -> Exchange | None:
        if self.directory is None:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{next(_counter):05d}-{upstream}-{kind}"
        return Exchange(self.directory / f"{name}.jsonl", kind, upstream, method, url)


class Exchange:
    def __init__(self, path: Path, kind: str, upstream: str, method: str, url: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        # Its address without the credentials a query can carry (Google's API key in ``key=``).
        self._write({"t": "open", "kind": kind, "upstream": upstream, "method": method, "url": redacted_line(url)})

    def _write(self, record: dict[str, Any]) -> None:
        record["ms"] = round((time.perf_counter() - self._t0) * 1000, 1)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def request(self, headers: list[tuple[str, str]], body: bytes, content_encoding: str | None) -> None:
        try:
            decoded = codec.decode(body, content_encoding)
        except Exception as exc:  # capture must never break forwarding
            decoded = b""
            self._write({"t": "request-decode-error", "error": repr(exc)})
        self._write(
            {
                "t": "request",
                "headers": redacted(headers),
                "encoding": content_encoding,
                "wire_bytes": len(body),
                "body": _text(decoded),
            }
        )

    def response_start(self, status: int, headers: list[tuple[str, str]]) -> None:
        # Handshake/unread callers also pass through this bounded metadata guard.
        kept: list[tuple[str, str]] = []
        charge = 0
        for name, value in headers:
            charge += 256 + 4 * (len(name) + len(value))
            if charge > 65536:
                break
            if safe_text(name) and safe_text(value):
                kept.append((name, value))
        self._write({"t": "response", "status": status, "headers": redacted(kept)})

    def response_chunk(self, chunk: bytes) -> None:
        """Called only by the screened owner's publication worker."""
        self._write({"t": "chunk", "data": _text(chunk)})

    def response_text(self, text: str) -> None:
        self._write({"t": "chunk", "data": text})

    def response_body(self, body: bytes) -> None:
        self._write({"t": "body", "data": _text(body)})

    def response_omitted(self, category: str) -> None:
        self._write({"t": "omitted", "reason": category})

    def response_end(self, error: str | None = None) -> None:
        # Failure strings from transports are never copied into raw capture.
        self._write({"t": "end", "error": "response-error" if error else None})

    def ws(self, direction: str, data: str | bytes) -> None:
        payload: str | dict[str, Any] = data if isinstance(data, str) else _text(data)
        self._write({"t": "ws", "dir": direction, "data": payload})
