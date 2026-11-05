"""A word's prints: cheap keyed fingerprints of the words of an identity's vault (the server performance plan's S1),
which each instance holds in memory to find which words of a text are any value's or fake's (``latet.server.index``)
without asking PostgreSQL for each, where ``vault_words`` keeps each word as a keyed HMAC (``PgVaultStore._word``).

One keyed BLAKE2b digest of the word's UTF-8, of 32 bytes, under a key derived from the identity's root key
(``print_key``) and a personalisation of its own, cut into four signed 64-bit numbers (little-endian): the word as a
value's, as a fake's, as a value's edge and as a fake's edge (``SLOT``: the role of a row of ``vault_words`` names the
one it keeps), the four labels the HMAC words have. A row keeps the slot of its role, never the others, so that a
reader of the database sees, as with the HMAC words, which rows share a word of one role, and nothing more: no word
(nobody makes a print without the root key), and not that a value's word is a fake's, or a string's edge another's
anchor (each slot of one digest tells nothing of the others). Another identity's root key gives other prints of the
same word. Sixty-four bits leave a word of a text that is none of a million values' a print of one of them once in
trillions of words: a print says only that a word may be one of the vault's, and every entry it finds is checked by its
decrypted string before it masks or restores anything."""

from __future__ import annotations

import hashlib
import struct
from typing import Final

from latet.server.sealing import DERIVE, derived

# What derives the key of the prints from the identity's root key (``sealing.derived``), and BLAKE2b's personalisation
# of them, whose last digit is their version: prints of another version are other numbers.
PRINT_PURPOSE: Final = b"vault-prints"
PRINT_PERSON: Final = b"latet-prints-1"
# The print each role of a row of ``vault_words`` keeps of its word's four: the value's word, the fake's word, and the
# edges of either (a head and a tail of one string are kept under one label, as their HMAC words are).
SLOT: Final = {
    "original": 0,
    "surrogate": 1,
    "original-head": 2,
    "original-tail": 2,
    "surrogate-head": 3,
    "surrogate-tail": 3,
}
_SLICES: Final = struct.Struct("<4q")


def print_key(root_key: bytes) -> bytes:
    """The key of an identity's prints, derived from its root key for this purpose alone."""
    return derived(root_key, DERIVE, PRINT_PURPOSE)


def prints(key: bytes, word: str) -> tuple[int, int, int, int]:
    """``word``'s four prints under ``key`` (``print_key``): as a value's word, a fake's word, a value's edge and a
    fake's edge, each in ``[-2**63, 2**63)`` (a BIGINT of PostgreSQL's)."""
    digest = hashlib.blake2b(word.encode(), key=key, digest_size=32, person=PRINT_PERSON).digest()
    value, fake, value_edge, fake_edge = _SLICES.unpack(digest)
    return value, fake, value_edge, fake_edge
