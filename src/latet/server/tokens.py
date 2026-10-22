"""A Latet server's secrets and what is derived from them. A secret is a prefix and 43 random characters (a prefix
secret scanners can be taught): ``latet_pat_`` a person's token for one computer, ``latet_ck_`` that computer's key (its
second secret, which its CLI alone keeps: R16), ``latet_key_`` an API key, ``latet_adm_`` an administration token. The
server never keeps them: it keeps a keyed hash of each (``keyed_hash``, an
HMAC under a key derived from the master key, which whoever writes the database without the master key cannot make for
a secret of his own choosing); ``secret_hash``, the plain SHA-256, is for what is random and kept sealed already. An
identity's address is ``/u/<route key>``, an HMAC of its secret: a new secret gives a new address, and the address
does not give the secret away."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

TOKEN_PREFIX = "latet_pat_"  # noqa: S105 - a prefix, not a secret
KEY_PREFIX = "latet_key_"
COMPUTER_PREFIX = "latet_ck_"
ADMIN_PREFIX = "latet_adm_"
ROUTE_LENGTH = 26
# What the administration says, first, of a token asked for that would end after the token that issues it (403): the
# administration CLI tells it apart from the other refusals by these words.
OUTLIVES_ISSUER = "an administration token issues tokens that end no later than itself"
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_KEYED = b"latet-credential" + bytes(1)  # what a keyed hash's bytes start with: the format they are in


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def check_key(key: bytes) -> bytes:
    """``key`` itself when it is 32 bytes. Raises ValueError otherwise, in words that hold no key: an empty key would
    let anyone make a hash that holds."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("a keyed hash needs a key of 32 bytes")
    return key


def keyed_hash(key: bytes, label: str, *parts: str) -> str:
    """HMAC-SHA256, in hex, under ``key`` (32 bytes, ``MasterKey.derive(b"credentials")`` on a server) of ``label``
    and ``parts``: what a server keeps of a credential in place of its plain SHA-256, and the tag that binds a row to
    what it says. ``label`` says what the hash is of (a token, a code, a route key, a row's tag), so that one is never
    taken for another. The label and each part are preceded by their length, so that no two different lists make the
    same bytes. A text that cannot be encoded (a lone surrogate) is encoded all the same: it matches nothing, and
    raises nothing."""
    message = bytearray(_KEYED)
    for text in (label, *parts):
        data = text.encode("utf-8", "surrogatepass")
        message += len(data).to_bytes(4, "big") + data
    return hmac.new(check_key(key), bytes(message), hashlib.sha256).hexdigest()


def route_key(secret: str) -> str:
    digest = hmac.new(secret.encode(), b"latet-route", hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:ROUTE_LENGTH].lower()


def is_route_key(text: str) -> bool:
    return len(text) == ROUTE_LENGTH and set(text) <= set(_ALPHABET)


def new_id(prefix: str) -> str:
    """An id: ``prefix`` and 10 lowercase base32 characters (50 random bits)."""
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(10))
