"""Bounded transport checks shared by the local bridge and server launch gate."""

from collections.abc import Mapping
from itertools import islice
from typing import Any

from latet.gateway.launch_keys import _CONTENT_KEY


def valid_launch_scope(scope: Mapping[str, Any], *, hold: bool = False) -> bool:
    headers = scope.get("headers", ())
    if len(headers) > 32 or sum(len(name) + len(value) for name, value in headers) > 4096:
        return False
    raw_path = scope.get("raw_path")
    if raw_path is None:
        path = scope.get("path", "")
        if len(path) > 256 or not path.isascii():
            return False
    elif len(raw_path) > 256:
        return False
    query = scope.get("query_string", b"")
    if not hold:
        return not query
    return len(query) <= 64 and not any(
        name.lower() == b"transfer-encoding"
        or (name.lower() == b"content-length" and value != b"0")
        or _CONTENT_KEY.search(value) is not None
        for name, value in headers
    )


def flat_launch_json(raw: bytearray) -> bool:
    """Reject nested containers and more than eight pairs before JSON allocation."""
    body = raw.strip(b" \t\r\n")
    if not (body.startswith(b"{") and body.endswith(b"}")):
        return False
    quoted = escaped = False
    commas = 0
    for char in islice(body, 1, len(body) - 1):
        if quoted:
            if escaped:
                escaped = False
            elif char == ord("\\"):
                escaped = True
            elif char == ord('"'):
                quoted = False
        elif char == ord('"'):
            quoted = True
        elif char in b"{}[]":
            return False
        elif char == ord(","):
            commas += 1
            if commas >= 8:
                return False
    return not quoted
