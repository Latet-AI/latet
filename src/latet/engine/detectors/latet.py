"""Latet's credentials and the secret segment of an identity's HTTPS address."""

from __future__ import annotations

import re
from collections.abc import Iterator

import re2

from latet.engine.detectors.base import DetectContext, group
from latet.engine.types import EntityClass, Span

TOKEN_TYPES = {
    "pat": "LATET_PERSONAL_TOKEN",
    "key": "LATET_API_KEY",
    "adm": "LATET_ADMIN_TOKEN",
    "ck": "LATET_COMPUTER_KEY",
    "scim": "LATET_SCIM_TOKEN",  # an Enterprise server's SCIM bearer token; the Community engine masks it all the same
    "brg": "LATET_BRIDGE_TOKEN",  # what the Claude Code plugin presents to the daemon on a computer
    "code": "LATET_ONE_TIME_CODE",  # what an administrator gives a person to connect a computer
}
LATET_TYPES = frozenset({*TOKEN_TYPES.values(), "LATET_ROUTE_KEY"})
# A secret is 43 URL-safe characters after its prefix (``secrets.token_urlsafe(32)``), a one-time code 32 (24 bytes).
_TOKEN = re2.compile(r"latet_(?:(pat|key|adm|ck|scim|brg)_[A-Za-z0-9_-]{43}|(code)_[A-Za-z0-9_-]{32})")
_ADDRESS = re2.compile(
    r"(?i:https)://(?:[A-Za-z0-9][A-Za-z0-9.\-]*|\[[0-9A-Fa-f:.]+\])"
    r"(?::[0-9]{1,5})?/u/([A-Za-z0-9_-]{16,128})"
)
_TOKEN_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_ADDRESS_BEFORE = _TOKEN_CHARACTERS | {".", "+", "/"}
_ADDRESS_END = frozenset("/?# \t\r\n\"'`<>)]},;.")
_PROSE_END = frozenset(" \t\r\n\"'`<>)]},;.")
# What a guard refuses pasted where a name, a label or a token of another kind goes (a cut secret too): the beginnings
# of the secrets with a prefix of their own, and a one-time code only by its whole shape, as `latet_code_review` is
# someone's word.
_SECRET_STARTS = tuple(f"latet_{kind}_" for kind in TOKEN_TYPES if kind != "code")
_A_CODE = re.compile(r"latet_code_[A-Za-z0-9_-]{32}(?![A-Za-z0-9_-])")


def starts_a_secret(text: str) -> bool:
    """Whether ``text`` begins with a secret of Latet's: the prefix of a token, a key or an administration, SCIM or
    bridge token, or a whole one-time code."""
    return text.startswith(_SECRET_STARTS) or _A_CODE.match(text) is not None


def holds_a_secret_start(text: str) -> bool:
    """Whether ``text`` holds the prefix of a token, a key or an administration, SCIM or bridge token (as lower case
    as the prefixes are), or a whole one-time code."""
    return any(start in text for start in _SECRET_STARTS) or _A_CODE.search(text) is not None


class LatetCredentialDetector:
    """Latet personal tokens, computer keys, API keys, administration tokens, SCIM tokens, bridge tokens and one-time
    codes, and route keys in HTTPS identity addresses."""

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
                kind = group(match, 1) or group(match, 2)
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
