"""Latet's credentials and the secret segment of an identity's HTTPS address."""

from __future__ import annotations

from collections.abc import Iterator

import re2

from latet.engine.detectors.base import DetectContext, group
from latet.engine.types import EntityClass, Span

TOKEN_TYPES = {
    "pat": "LATET_PERSONAL_TOKEN",
    "key": "LATET_API_KEY",
    "adm": "LATET_ADMIN_TOKEN",
    "ck": "LATET_COMPUTER_KEY",
}
LATET_TYPES = frozenset({*TOKEN_TYPES.values(), "LATET_ROUTE_KEY"})
_TOKEN = re2.compile(r"latet_(pat|key|adm|ck)_[A-Za-z0-9_-]{43}")
_ADDRESS = re2.compile(
    r"(?i:https)://(?:[A-Za-z0-9][A-Za-z0-9.\-]*|\[[0-9A-Fa-f:.]+\])"
    r"(?::[0-9]{1,5})?/u/([A-Za-z0-9_-]{16,128})"
)
_TOKEN_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_ADDRESS_BEFORE = _TOKEN_CHARACTERS | {".", "+", "/"}
_ADDRESS_END = frozenset("/?# \t\r\n\"'`<>)]},;.")
_PROSE_END = frozenset(" \t\r\n\"'`<>)]},;.")


class LatetCredentialDetector:
    """Latet personal tokens, computer keys, API keys and administration tokens, and route keys in HTTPS identity
    addresses."""

    id = "latet-credentials"
    mandatory = True

    def detect(self, text: str, ctx: DetectContext) -> Iterator[Span]:
        if "latet_" in text:
            for match in _TOKEN.finditer(text):
                start, end = match.span()
                if start and text[start - 1] in _TOKEN_CHARACTERS:
                    continue
                if end < len(text) and text[end] in _TOKEN_CHARACTERS:
                    continue
                kind = group(match, 1)
                yield Span(start, end, TOKEN_TYPES[kind], EntityClass.SECRET, f"latet-credentials:{kind}")
        if "/u/" in text:
            for match in _ADDRESS.finditer(text):
                if match.start() and text[match.start() - 1] in _ADDRESS_BEFORE:
                    continue
                start, end = match.span(1)
                if end < len(text) and text[end] not in _ADDRESS_END:
                    continue
                if text[end : end + 1] == "." and end + 1 < len(text) and text[end + 1] not in _PROSE_END:
                    continue
                yield Span(start, end, "LATET_ROUTE_KEY", EntityClass.SECRET, "latet-credentials:route")
