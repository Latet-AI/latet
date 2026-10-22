"""Header policy: what is forwarded, what is dropped, and what is never logged."""

from __future__ import annotations

from collections.abc import Iterable

# RFC 9110 §7.6.1 hop-by-hop headers plus the ones a proxy must recompute itself.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Never forwarded upstream: recomputed by the HTTP client or meaningful only locally.
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length"}
# Latet's own headers (`x-latet-project` names a folder of this computer): they stay here.
DROP_PREFIXES = ("x-latet-",)

# Handshake headers of a WebSocket upgrade, negotiated separately on each leg.
WS_HANDSHAKE_DROP = REQUEST_DROP | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-accept",
    "sec-websocket-protocol",
}

RESPONSE_DROP = HOP_BY_HOP | {"content-length"}

# Values of these headers are credentials or device attestations: logged only as a length.
_SECRET_EXACT = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "chatgpt-account-id",
        "x-oai-attestation",
        "openai-organization",
        "openai-project",
    }
)
_SECRET_MARKERS = ("token", "secret", "auth", "key", "cookie", "attestation", "account")


def is_secret_header(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SECRET_EXACT or any(marker in lowered for marker in _SECRET_MARKERS)


def forwardable(
    headers: Iterable[tuple[str, str]], drop: frozenset[str], prefixes: tuple[str, ...] = ()
) -> list[tuple[str, str]]:
    """Keep every header except the dropped ones (by name, or by the start of it), preserving order
    and duplicates."""
    return [(k, v) for k, v in headers if k.lower() not in drop and not k.lower().startswith(prefixes)]


def redacted(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers as they may be written to a log: secret values replaced by their length."""
    return [(k, f"<redacted len={len(v)}>" if is_secret_header(k) else v) for k, v in headers]
