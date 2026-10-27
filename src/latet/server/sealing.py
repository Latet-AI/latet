"""What an identity keeps on a server, sealed with keys derived from its root key: AES-GCM as on a computer, with
additional data that binds each sealed value to its place, and a frame that names the version of the key that sealed
it (so that a key can be rotated later: the rotation itself is not in these plans); and keyed hashes, which find a
value again without giving it away.

Each seal's nonce is random, 96 bits, and nothing here counts seals. An identity's Valkey records use one AES-GCM key,
never rotated. A program sealing at every request passes NIST's 2^32 seals per key within months, although the chance
of a nonce collision stays near one in a million at a thousand seals a second for ten years. Per-record subkeys come
with the next FORMAT, the old one still read, before an identity is expected to pass 2^32 seals. A dashboard session is
sealed when it opens and only read at each use (``live.ValkeySignIns.subject_of``); every new purpose derives its own
key (``derived``)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Sequence
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = 1  # the first byte of every sealed value
VERSIONS = 255  # how many versions of one key the second byte can name
_NONCE = 12
# What a key is for goes first into what derives it, and each use starts with a byte of its own: whatever a purpose
# is called, a key that seals is never the key that signs or the key that hashes.
SEAL, KEYED, DERIVE = b"seal:", b"keyed:", b"derive:"


def derived(key: bytes, use: bytes, purpose: bytes) -> bytes:
    """The key that ``key`` gives for one use (``SEAL``, ``KEYED`` or ``DERIVE``) and one purpose: an HMAC-SHA256 of
    the two."""
    return hmac.new(key, use + purpose, hashlib.sha256).digest()


def frame(version: int, nonce: bytes, ciphertext: bytes) -> bytes:
    """``format | key version | nonce | ciphertext``."""
    if not 1 <= version <= VERSIONS:
        raise ValueError("a key version is 1 to 255")
    return bytes([FORMAT, version]) + nonce + ciphertext


def version_of(blob: bytes) -> int | None:
    """The version of the key that a sealed value names; None for what is not a sealed value."""
    if len(blob) < 2 + _NONCE + 16 or blob[0] != FORMAT:
        return None
    return blob[1]


def unframe(blob: bytes) -> tuple[int, bytes, bytes]:
    """The key version, the nonce and the ciphertext of a sealed value. Raises ValueError for what is not one."""
    version = version_of(blob)
    if version is None:
        raise ValueError("not a sealed value")
    return version, blob[2 : 2 + _NONCE], blob[2 + _NONCE :]


def _bound(version: int, aad: str) -> bytes:
    """What a value is sealed with: the frame's two bytes, then its place. So a value moved elsewhere does not open,
    nor one whose frame was changed to name another format or another version, whatever key that version holds."""
    return bytes([FORMAT, version]) + aad.encode()


class Sealer:
    """Seals with the last of ``keys`` (version ``len(keys)``) and opens with the version a blob names; ``purpose``
    keeps the keys of one use apart from another's (storage, settings, valkey, sign-ins)."""

    def __init__(self, keys: bytes | Sequence[bytes], purpose: bytes) -> None:
        versions = [keys] if isinstance(keys, bytes) else list(keys)
        if not versions or any(len(key) != 32 for key in versions):  # an empty key would seal for whoever asks
            raise ValueError("a sealer needs a key of 32 bytes")
        if len(versions) > VERSIONS:
            raise ValueError("a sealer holds 255 versions of its key at most: a sealed value names one in a byte")
        if len(set(versions)) != len(versions):
            raise ValueError("a sealer's key is given twice: each version is a key of its own")
        self._aeads = {n: AESGCM(derived(key, SEAL, purpose)) for n, key in enumerate(versions, 1)}
        self.version = len(versions)
        self._keyed = derived(versions[-1], KEYED, purpose)
        self._paired = derived(self._keyed, DERIVE, b"keyed-pair")  # ``keyed_pair``'s

    def seal(self, data: bytes, aad: str) -> bytes:
        nonce = os.urandom(_NONCE)
        sealed = self._aeads[self.version].encrypt(nonce, data, _bound(self.version, aad))
        return frame(self.version, nonce, sealed)

    def open(self, blob: bytes, aad: str) -> bytes:
        version, nonce, ciphertext = unframe(blob)
        aead = self._aeads.get(version)
        if aead is None:
            raise ValueError(f"sealed with key version {version}, which is not held")
        return aead.decrypt(nonce, ciphertext, _bound(version, aad))

    def seal_json(self, value: Any, aad: str) -> bytes:
        return self.seal(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(), aad)

    def open_json(self, blob: bytes, aad: str) -> Any:
        return json.loads(self.open(blob, aad))

    def keyed(self, text: str) -> bytes:
        """A keyed hash of ``text``: the same text gives the same bytes, which give nothing of it away. Made in one call
        (``hmac.digest``): the words of a long text are hashed by the hundred thousand."""
        return hmac.digest(self._keyed, text.encode(), "sha256")

    def keyed_pair(self, text: str) -> tuple[bytes, bytes]:
        """Two keyed hashes of ``text`` from one call, the halves of an HMAC-SHA512 under a key of their own (derived
        from ``keyed``'s): a key's vault keeps a word under the first and an edge under the second (anchors version 9,
        review 10b's d), so that a text's word of six letters, asked as both, is hashed once. Neither half tells the
        other, nor any ``keyed`` hash."""
        both = hmac.digest(self._paired, text.encode(), "sha512")
        return both[:32], both[32:]
