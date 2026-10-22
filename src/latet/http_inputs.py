"""Small HTTP input primitives shared by the computer and server, without server extras."""

import hmac

from starlette.requests import Request


def same_etag(given: str, expected: str) -> bool:
    """Compare caller text with a generated etag as bytes, including lone caller surrogates."""
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode())


async def bounded_body(request: Request, limit: int) -> bytearray | None:
    """Read at most ``limit`` bytes into one buffer; refuse an oversized chunk before retaining it."""
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            return None
    except ValueError:
        pass  # Count actual bytes even when the declared length cannot be used.
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(body):
            return None
        body += chunk
    return body
