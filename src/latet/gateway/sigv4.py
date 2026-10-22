"""AWS Signature Version 4, for Bedrock: the signature Claude Code would make for Amazon's address,
made by Latet for the request it forwards (Claude Code's own would be for 127.0.0.1)."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"


@dataclass(frozen=True, slots=True)
class AwsCredentials:
    """Credentials of one AWS identity: never written anywhere, never shown."""

    access_key: str
    secret_key: str = field(repr=False)
    token: str | None = field(default=None, repr=False)  # temporary credentials' session token
    expires: float | None = None  # when temporary credentials end (a time.time() value)


def _segments(path: str) -> str:
    """The path without dot segments nor empty ones, as AWS reads it."""
    out: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if out:
                out.pop()
        elif part and part != ".":
            out.append(part)
    return ("/" if path.startswith("/") else "") + "/".join(out) + ("/" if path.endswith("/") and out else "")


def canonical_uri(wire_path: str) -> str:
    """The path as AWS signs it for a service other than S3: as it is sent, encoded once more (an ARN's
    `%2F` signs as `%252F`)."""
    return quote(_segments(wire_path), safe="/~") or "/"


def canonical_query(query: str) -> str:
    """Each name and value encoded as RFC 3986 asks, sorted."""
    pairs = []
    for item in query.split("&") if query else []:
        name, _, value = item.partition("=")
        pairs.append((quote(unquote(name), safe="-_.~"), quote(unquote(value), safe="-_.~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(
    method: str,
    url: str,
    body: bytes,
    credentials: AwsCredentials,
    region: str,
    service: str = "bedrock",
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """The headers that sign this request: its host, its time and its body, nothing the client set.
    The URL's path is the one sent, percent-escapes kept."""
    when = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date, day = when.strftime("%Y%m%dT%H%M%SZ"), when.strftime("%Y%m%d")
    parts = urlsplit(url)
    headers = [("host", parts.netloc), ("x-amz-date", amz_date)]
    if credentials.token:
        headers.append(("x-amz-security-token", credentials.token))
    signed = ";".join(name for name, _ in headers)
    canonical = "\n".join(
        [
            method.upper(),
            canonical_uri(parts.path),
            canonical_query(parts.query),
            "".join(f"{name}:{value.strip()}\n" for name, value in headers),
            signed,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + credentials.secret_key).encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"{ALGORITHM} Credential={credentials.access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return [*headers[1:], ("authorization", authorization)]
