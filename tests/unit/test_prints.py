"""A word's prints (``latet.server.prints``): one keyed BLAKE2b digest of the word cut into four 64-bit numbers, the word
as a value's, as a fake's, as a value's edge and as a fake's edge, under a key derived from the identity's root key, so
that a reader of PostgreSQL learns no word from them and another identity's prints of the same word are others."""

import hashlib
import struct

from latet.server.prints import PRINT_PERSON, PRINT_PURPOSE, SLOT, print_key, prints
from latet.server.sealing import DERIVE, derived

ROOT = b"\x07" * 32
OTHER = b"\x08" * 32


def test_a_words_prints_are_four_and_keyed():
    key = print_key(ROOT)
    assert key == derived(ROOT, DERIVE, PRINT_PURPOSE) and len(key) == 32
    found = prints(key, "bianchi")
    assert found == prints(key, "bianchi") == prints(print_key(ROOT), "bianchi")  # the same word, the same prints
    assert len(found) == 4 and len(set(found)) == 4
    assert all(isinstance(p, int) and -(2**63) <= p < 2**63 for p in found)
    # One digest of the word's UTF-8 under the key, cut into four little-endian signed slices.
    digest = hashlib.blake2b(b"bianchi", key=key, digest_size=32, person=PRINT_PERSON).digest()
    assert found == struct.unpack("<4q", digest)
    # Another identity's root key gives other prints of the same word, none of them its own.
    theirs = prints(print_key(OTHER), "bianchi")
    assert not set(theirs) & set(found)
    # Not the word's digest without a key: nobody without the root key makes them.
    bare = struct.unpack("<4q", hashlib.blake2b(b"bianchi", digest_size=32, person=PRINT_PERSON).digest())
    assert not set(bare) & set(found)
    # Another word, other prints; a word of another script is a word too.
    assert not set(prints(key, "rossi")) & set(found) and len(set(prints(key, "петров"))) == 4
    assert prints(key, "Bianchi") != found  # a word is printed as the store makes it (folded before), never folded here


def test_a_word_and_its_edge_differ():
    """A string's word and its edge (a word of six letters is asked as both) are two slices of one digest, as a value's
    and a fake's are: none is another's, and the roles name their slot."""
    key = print_key(ROOT)
    word = "nordes"  # six letters: a text's word that is asked as a word and as an edge
    value, fake, value_edge, fake_edge = prints(key, word)
    assert len({value, fake, value_edge, fake_edge}) == 4
    assert SLOT == {
        "original": 0,
        "surrogate": 1,
        "original-head": 2,
        "original-tail": 2,
        "surrogate-head": 3,
        "surrogate-tail": 3,
    }
    found = prints(key, word)
    values = [found[SLOT[role]] for role in ("original", "original-head", "original-tail")]
    fakes = [found[SLOT[role]] for role in ("surrogate", "surrogate-head", "surrogate-tail")]
    assert values == [value, value_edge, value_edge] and fakes == [fake, fake_edge, fake_edge]
    assert "held" not in SLOT  # a secret held glued has no word, and no print
