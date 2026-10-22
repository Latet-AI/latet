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
        self._response = bytearray()
        self._response_encoding: str | None = None
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
        self._response_encoding = next((v for k, v in headers if k.lower() == "content-encoding"), None)
        self._write({"t": "response", "status": status, "headers": redacted(headers)})

    def response_chunk(self, chunk: bytes) -> None:
        if self._response_encoding:
            self._response.extend(chunk)  # compressed: decode once at the end
        else:
            self._write({"t": "chunk", "data": _text(chunk)})

    def response_end(self, error: str | None = None) -> None:
        if self._response_encoding and self._response:
            try:
                body = codec.decode(bytes(self._response), self._response_encoding)
                self._write({"t": "body", "data": _text(body)})
            except Exception as exc:
                self._write({"t": "body-decode-error", "error": repr(exc), "bytes": len(self._response)})
        self._write({"t": "end", "error": error})

    def ws(self, direction: str, data: str | bytes) -> None:
        payload: str | dict[str, Any] = data if isinstance(data, str) else _text(data)
        self._write({"t": "ws", "dir": direction, "data": payload})
